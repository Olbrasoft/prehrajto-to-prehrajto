#!/usr/bin/env python3
"""One-shot migration: split state/uploaded.json by cr_film_id % NUM_SHARDS.

Run once before enabling parallel shards in the workflow. The sharded
sync_batch.py only reads from state/uploaded-shard-{id}.json — if existing
uploads aren't migrated, parallel runners will re-upload films already on
filmy.prehrajto@post.cz and create duplicates.

Usage:
    python src/split_state_by_shard.py 2     # split into 2 shards
    python src/split_state_by_shard.py 4     # 4 shards, etc.

Reads state/uploaded.json, writes state/uploaded-shard-{0..N-1}.json,
leaves the original in place (untouched) as historical reference. The
sharded reader ignores it once NUM_SHARDS>1.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "state" / "uploaded.json"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: split_state_by_shard.py <num_shards>", file=sys.stderr)
        return 2
    n = int(sys.argv[1])
    if n < 2:
        print("num_shards must be >= 2", file=sys.stderr)
        return 2

    if not SRC.exists():
        print(f"ERROR: {SRC} missing", file=sys.stderr)
        return 2
    data = json.loads(SRC.read_text())

    buckets: dict[int, dict] = defaultdict(
        lambda: {"schema_version": data.get("schema_version", 1),
                 "uploads": [], "failed_attempts": []}
    )

    for u in data.get("uploads", []):
        k = u["cr_film_id"] % n
        buckets[k]["uploads"].append(u)

    # failed_attempts is keyed by cr_film_id too — split it so each shard
    # only sees its own burned upload_ids. (A cross-shard miss is harmless,
    # just a wasted resolve attempt; this keeps the shard files small.)
    for a in data.get("failed_attempts", []):
        k = a["cr_film_id"] % n
        buckets[k]["failed_attempts"].append(a)

    for k in range(n):
        bucket = buckets[k]
        bucket["last_updated"] = data.get("last_updated")
        out = REPO_ROOT / "state" / f"uploaded-shard-{k}.json"
        out.write_text(json.dumps(bucket, ensure_ascii=False, indent=2) + "\n")
        print(f"shard {k}: {len(bucket['uploads'])} uploads, "
              f"{len(bucket['failed_attempts'])} failed attempts → {out.name}")

    total = sum(len(buckets[k]["uploads"]) for k in range(n))
    print(f"total uploads written: {total} (source had {len(data.get('uploads', []))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
