#!/usr/bin/env python3
"""Update uploaded email-account videos with generated descriptions."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from prehrajto_upload import login  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ACCESS_PATH = Path("/home/jirka/Dokumenty/přístupy/prehrajto.md")
EMAIL = "filmy.prehrajto@email.cz"
LOG_PATH = ROOT / "state/email-description-updates.jsonl"
GENERATED_PATH = ROOT / "state/email-generated-descriptions-v3.jsonl"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_password() -> str:
    text = ACCESS_PATH.read_text(encoding="utf-8")
    pos = text.find(EMAIL)
    if pos < 0:
        raise RuntimeError("email account not found in access file")
    block = text[pos : pos + 1200]
    match = re.search(r"(?im)^\s*#?\s*(?:heslo|password)\s*[:=-]\s*(\S+)\s*$", block)
    if not match:
        raise RuntimeError("password not found near email account")
    return match.group(1).strip()


def load_generated() -> dict[int, dict]:
    generated: dict[int, dict] = {}
    for line in GENERATED_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") == "ok" and row.get("new_description"):
            generated[int(row["cr_film_id"])] = row
    return generated


def load_updated_ids() -> set[int]:
    updated: set[int] = set()
    if not LOG_PATH.exists():
        return updated
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("ok") is True or row.get("status") == "ok":
            updated.add(int(row["cr_film_id"]))
    return updated


def load_pending(limit: int) -> list[dict]:
    generated = load_generated()
    updated = load_updated_ids()
    pending: list[dict] = []
    for shard in [0, 1]:
        state_path = ROOT / f"state/email-uploaded-shard-{shard}.json"
        if not state_path.exists():
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for upload in state.get("uploads", []):
            cr_film_id = int(upload["cr_film_id"])
            if upload.get("preexisting_on_account"):
                continue
            if cr_film_id in updated or cr_film_id not in generated:
                continue
            video_id = upload.get("prehrajto_video_id") or upload.get("video_id")
            if not video_id:
                continue
            pending.append(
                {
                    "cr_film_id": cr_film_id,
                    "video_id": str(video_id),
                    "name": upload.get("display_name")
                    or generated[cr_film_id].get("display_name")
                    or upload.get("title"),
                    "description": generated[cr_film_id]["new_description"],
                }
            )
    pending.sort(key=lambda item: item["cr_film_id"])
    return pending[:limit]


def update_one(session, item: dict) -> dict:
    params = {
        "uploadedVideoListing-videoId": item["video_id"],
        "do": "uploadedVideoListing-changeVideoNameAndVideoDescription",
        "uploadedVideoListing-name": item["name"],
        "uploadedVideoListing-desc": item["description"],
    }
    url = "https://prehraj.to/profil/nahrana-videa?" + urlencode(params)
    try:
        response = session.get(
            url,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": "https://prehraj.to/profil/nahrana-videa",
            },
            timeout=30,
        )
        success = response.status_code == 200
        info = f"http={response.status_code} len={len(response.text)}"
    except Exception as exc:  # noqa: BLE001
        success = False
        info = f"{type(exc).__name__}: {str(exc)[:160]}"
    return {
        "cr_film_id": item["cr_film_id"],
        "video_id": item["video_id"],
        "name": item["name"],
        "desc_len": len(item["description"]),
        "ok": success,
        "info": info,
        "updated_at": now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    pending = load_pending(args.limit)
    print(f"description_update_pending={len(pending)}", flush=True)
    if not pending:
        return 0

    session = login(EMAIL, load_password())
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        for item in pending:
            row = update_one(session, item)
            ok += int(row["ok"])
            fail += int(not row["ok"])
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            print(
                f"{'ok' if row['ok'] else 'fail'} "
                f"cr_film_id={row['cr_film_id']} video_id={row['video_id']}",
                flush=True,
            )
    print(f"description_update_done ok={ok} fail={fail}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
