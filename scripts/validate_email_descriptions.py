#!/usr/bin/env python3
"""Validate generated email-account descriptions."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "state/email-generated-descriptions-v3.jsonl"


def main() -> int:
    rows = []
    for line in PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))

    ok_rows = [row for row in rows if row.get("status") == "ok" and row.get("new_description")]
    fail_rows = [row for row in rows if row.get("status") == "fail"]
    ids = [int(row["cr_film_id"]) for row in ok_rows]
    texts = [row["new_description"].strip() for row in ok_rows]
    duplicate_ids = [item for item, count in Counter(ids).items() if count > 1]
    duplicate_texts = [item for item, count in Counter(texts).items() if count > 1]
    too_short = [row["cr_film_id"] for row in ok_rows if len(row["new_description"]) < 150]
    too_long = [row["cr_film_id"] for row in ok_rows if len(row["new_description"]) > 700]

    print(
        "description_validation "
        f"rows={len(rows)} ok={len(ok_rows)} fail={len(fail_rows)} "
        f"duplicate_ids={len(duplicate_ids)} duplicate_texts={len(duplicate_texts)} "
        f"too_short={len(too_short)} too_long={len(too_long)}"
    )

    if duplicate_ids or duplicate_texts or too_short or too_long:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
