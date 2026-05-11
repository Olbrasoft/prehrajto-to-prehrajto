#!/usr/bin/env python3
"""Batch orchestrator: re-upload N films from prehraj.to to our account.

For each picked film, walk its candidate uploads in priority order and try
resolve → download → upload until one succeeds. State and the human log get
persisted **after every attempt** so a runner timeout/crash never loses
progress.

Run:
    PREHRAJTO_EMAIL=… PREHRAJTO_PASSWORD=… python3 src/sync_batch.py [--count 5]

A single failed candidate (404, throttled, oversize, transient upload
error) does not stop the batch — it gets logged into `failed_attempts`
and we move on to the next candidate or the next film.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pick_next_film import (  # noqa: E402
    BACKLOG, STATE, load_backlog, load_state, pick_next,
)
from resolve_stream import resolve as resolve_stream, pick_best, ResolveError  # noqa: E402
from download import download_to, head_size, DownloadError, MAX_FILE_SIZE  # noqa: E402
from prehrajto_upload import login, upload_video  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "state" / "sync.log"
TMP_DIR = Path("/tmp")


def log(msg: str) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_state(state: dict) -> None:
    state["last_updated"] = now_iso()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def safe_filename(name: str) -> str:
    # Keep it simple — strip filesystem-hostile chars but allow Unicode.
    return re.sub(r'[\\/:"<>|*?]', "_", name)[:200]


def record_failure(
    state: dict,
    cr_film_id: int,
    upload_id: str,
    reason: str,
    permanent: bool = False,
    timing: dict | None = None,
) -> None:
    entry = {
        "cr_film_id": cr_film_id,
        "upload_id": upload_id,
        "reason": reason,
        "permanent": permanent,
        "failed_at": now_iso(),
    }
    if timing:
        entry["timing"] = timing
    state.setdefault("failed_attempts", []).append(entry)
    save_state(state)


def try_candidate(
    film: dict,
    candidate: dict,
    session,
    state: dict,
) -> bool:
    """Try one upload_id end-to-end. Returns True on full success."""
    upload_id = candidate["upload_id"]
    cr_film_id = film["cr_film_id"]
    name = film["display_name"]
    description = film.get("description") or ""
    log(f"  candidate upload_id={upload_id} url={candidate['url']}")

    # 1. Resolve player stream URL (1080p preferred).
    t = time.monotonic()
    try:
        resolved = resolve_stream(candidate["url"])
        best = pick_best(resolved.videos)
    except ResolveError as e:
        log(f"  resolve FAILED: {e} (permanent={e.permanent})")
        record_failure(state, cr_film_id, upload_id, f"resolve_failed: {e}",
                       permanent=e.permanent)
        return False
    except Exception as e:  # last-line defense; never let a single film abort the batch
        log(f"  resolve CRASHED ({type(e).__name__}: {e})")
        record_failure(state, cr_film_id, upload_id,
                       f"resolve_crashed: {type(e).__name__}: {e}", permanent=False)
        return False
    resolve_sec = round(time.monotonic() - t, 1)
    log(f"  resolved in {resolve_sec}s → {best.label} ({best.url[:80]}…)")

    # 2. Pre-download size check — premiumcdn returns Content-Length on HEAD.
    expected = head_size(best.url)
    if expected is not None and expected > MAX_FILE_SIZE:
        log(f"  oversize: {expected} B > {MAX_FILE_SIZE} B, skip")
        record_failure(state, cr_film_id, upload_id,
                       f"oversize: {expected} B", permanent=True)
        return False

    # 3. Download.
    tmp_path = TMP_DIR / f"{safe_filename(name)}.mp4"
    t = time.monotonic()
    try:
        size = download_to(best.url, tmp_path)
    except DownloadError as e:
        dl_sec = round(time.monotonic() - t, 1)
        log(f"  download FAILED after {dl_sec}s: {e}")
        record_failure(state, cr_film_id, upload_id,
                       f"download_failed: {e}", permanent=False,
                       timing={"resolve_sec": resolve_sec, "download_sec": dl_sec})
        if tmp_path.exists():
            tmp_path.unlink()
        return False
    dl_sec = round(time.monotonic() - t, 1)
    mbps = round(size / 1_000_000 / max(dl_sec, 0.001), 1)
    log(f"  downloaded {size / 1_000_000:,.1f} MB in {dl_sec}s ({mbps} MB/s)")

    # 4. Upload.
    t = time.monotonic()
    try:
        video_id = upload_video(session, tmp_path,
                                display_name=name, description=description)
    except Exception as e:
        up_sec = round(time.monotonic() - t, 1)
        log(f"  upload FAILED after {up_sec}s: {e}")
        record_failure(state, cr_film_id, upload_id,
                       f"upload_failed: {e}", permanent=False,
                       timing={"resolve_sec": resolve_sec, "download_sec": dl_sec, "upload_sec": up_sec})
        return False
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    up_sec = round(time.monotonic() - t, 1)
    log(f"  upload done video_id={video_id} in {up_sec}s")

    state.setdefault("uploads", []).append({
        "cr_film_id": cr_film_id,
        "cr_slug": film.get("cr_slug"),
        "title": film["title"],
        "year": film.get("year"),
        "display_name": name,
        "preferred_lang_class": film["preferred_lang_class"],
        "src_upload_id": upload_id,
        "src_lang_class": candidate["lang_class"],
        "src_resolution": best.label,
        "prehrajto_video_id": video_id,
        "size_bytes": size,
        "uploaded_at": now_iso(),
        "timing": {
            "resolve_sec": resolve_sec,
            "download_sec": dl_sec,
            "upload_sec": up_sec,
        },
    })
    save_state(state)
    return True


def process_film(film: dict, session, state: dict) -> bool:
    cr_film_id = film["cr_film_id"]
    name = film["display_name"]
    log(f"film cr_film_id={cr_film_id} name={name!r} "
        f"candidates={len(film['candidates'])}")
    t = time.monotonic()
    for cand in film["candidates"]:
        if try_candidate(film, cand, session, state):
            log(f"film cr_film_id={cr_film_id} OK in {round(time.monotonic() - t, 1)}s")
            return True
    log(f"film cr_film_id={cr_film_id} all candidates exhausted")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    # Empty string passed by GA workflows when scheduled (no inputs available)
    # would normally crash argparse; coerce to default.
    def _count(v: str) -> int:
        return 50 if not v.strip() else int(v)
    ap.add_argument("--count", type=_count, default=50, help="Films per batch (default 50)")
    args = ap.parse_args()

    email = os.environ.get("PREHRAJTO_EMAIL")
    password = os.environ.get("PREHRAJTO_PASSWORD")
    if not email or not password:
        log("ERROR: PREHRAJTO_EMAIL / PREHRAJTO_PASSWORD required")
        return 2

    backlog = load_backlog()
    state = load_state()
    log(f"batch-start count={args.count} backlog={len(backlog)} "
        f"uploads={len(state.get('uploads', []))} "
        f"failed_attempts={len(state.get('failed_attempts', []))}")

    log("login")
    session = login(email, password)
    log("login done")

    ok = bad = 0
    attempted: set[int] = set()
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 3
    t_batch = time.monotonic()
    for i in range(args.count):
        log(f"iteration {i + 1}/{args.count}")
        # In-memory exclude prevents re-picking the same film when
        # every candidate failed transiently (e.g. proxy 502 wrapping
        # prehraj.to's 404 for dead uploads). Across-batch retries
        # still work — `attempted` is fresh each run.
        film = pick_next(state, backlog, attempted)
        if film is None:
            log("backlog exhausted")
            break
        attempted.add(film["cr_film_id"])
        if process_film(film, session, state):
            ok += 1
            consecutive_failures = 0
        else:
            bad += 1
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log(f"bail-out: {consecutive_failures} consecutive films exhausted "
                    f"(proxy likely down) — stopping batch early")
                break

    dur = round(time.monotonic() - t_batch, 1)
    avg = round(dur / max(ok, 1), 1) if ok else 0
    remaining = len(backlog) - len(state.get("uploads", []))
    eta_h = round(remaining * avg / 3600, 1) if avg else None
    log(f"batch-end ok={ok} failed={bad} dur={dur}s avg/film={avg}s "
        f"remaining={remaining} eta_hours={eta_h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
