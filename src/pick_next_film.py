#!/usr/bin/env python3
"""Pick the next film from the Phase 1 backlog that hasn't been uploaded yet.

Backlog shape (one JSONL line per film):
    {cr_film_id, display_name, description, candidates: [{upload_id, url, ...}, ...]}

State shape:
    {schema_version: 1, uploads: [{cr_film_id, prehrajto_url, when, ...}],
     failed_attempts: [{cr_film_id, upload_id, reason, when}]}

Sharding:
    Set SYNC_NUM_SHARDS=N and SYNC_SHARD_ID=K (0..N-1) to run multiple
    workflows in parallel. Each shard owns films where cr_film_id % N == K
    and reads/writes only `state/uploaded-shard-K.json`. Default
    (NUM_SHARDS=1) keeps the legacy single-file path `state/uploaded.json`.

The orchestrator calls `pick_next(state, rows)` to get the first film whose
`cr_film_id` is NOT in `state.uploads` and which still has at least one
candidate that hasn't been definitively burned in `failed_attempts`.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKLOG = REPO_ROOT / "backlog" / "prehrajto-films.jsonl.gz"

NUM_SHARDS = int(os.environ.get("SYNC_NUM_SHARDS", "1"))
SHARD_ID = int(os.environ.get("SYNC_SHARD_ID", "0"))


def state_path(shard_id: int | None = None) -> Path:
    """Return the state JSON path for the current (or specified) shard.

    NUM_SHARDS=1 → legacy single file `state/uploaded.json` (preserves
    history before sharding was introduced). NUM_SHARDS>1 → per-shard
    files so parallel runners never conflict on state writes.
    """
    sid = shard_id if shard_id is not None else SHARD_ID
    if NUM_SHARDS <= 1:
        return REPO_ROOT / "state" / "uploaded.json"
    return REPO_ROOT / "state" / f"uploaded-shard-{sid}.json"


# Backward-compatible alias still imported by other modules.
STATE = state_path()


def load_backlog(path: Path = BACKLOG) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_state(path: Path | None = None) -> dict:
    p = path or state_path()
    if not p.exists():
        return {"schema_version": 1, "uploads": [], "failed_attempts": []}
    return json.loads(p.read_text())


def uploaded_film_ids(state: dict) -> set[int]:
    return {u["cr_film_id"] for u in state.get("uploads", [])}


def burned_upload_ids(state: dict) -> set[str]:
    """upload_ids whose last attempt was a permanent failure (404, dead, geoblock)."""
    return {a["upload_id"] for a in state.get("failed_attempts", []) if a.get("permanent")}


def pick_next(
    state: dict,
    backlog_rows: list[dict],
    extra_exclude: set[int] | None = None,
) -> dict | None:
    """Return the next film to attempt, or None when nothing remains.

    Filters by `cr_film_id % NUM_SHARDS == SHARD_ID` so two parallel
    runners never race on the same film. With NUM_SHARDS=1 the filter
    is a no-op (every film belongs to shard 0), matching the original
    single-runner behavior.

    `extra_exclude` is an in-memory set the orchestrator uses to skip
    cr_film_ids it has already attempted in the current batch — including
    ones that failed every candidate transiently. Without it, transient
    failures (permanent=False) would let pick_next return the same film
    again and the batch would infinite-loop on a 404-wrapped-as-502
    upload until the 5 h job timeout. Across-batch retries still work
    because extra_exclude is rebuilt per-batch.
    """
    done = uploaded_film_ids(state)
    burned = burned_upload_ids(state)
    extras = extra_exclude or set()
    for film in backlog_rows:
        if NUM_SHARDS > 1 and film["cr_film_id"] % NUM_SHARDS != SHARD_ID:
            continue
        if film["cr_film_id"] in done or film["cr_film_id"] in extras:
            continue
        live = [c for c in film["candidates"] if c["upload_id"] not in burned]
        if not live:
            continue
        # Return a shallow copy with the live-only candidate list so the
        # orchestrator iterates them in priority order.
        return {**film, "candidates": live}
    return None


def main() -> int:
    if not BACKLOG.is_file():
        print(f"ERROR: backlog missing: {BACKLOG}", file=sys.stderr)
        return 2

    state = load_state()
    rows = load_backlog()
    shard_info = f" (shard {SHARD_ID}/{NUM_SHARDS})" if NUM_SHARDS > 1 else ""
    print(f"[pick]{shard_info} state: {len(state.get('uploads', []))} done, "
          f"{len(state.get('failed_attempts', []))} failed attempts")
    print(f"[pick] backlog: {len(rows)} films")

    pick = pick_next(state, rows)
    if pick is None:
        print("[pick] backlog exhausted")
        return 1

    print(f"[pick] cr_film_id={pick['cr_film_id']}")
    print(f"[pick] display_name={pick['display_name']!r}")
    print(f"[pick] preferred_lang={pick['preferred_lang_class']}, "
          f"candidates={len(pick['candidates'])}")
    desc = pick.get("description") or ""
    print(f"[pick] description ({len(desc)} chars): {desc[:120]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
