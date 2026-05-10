#!/usr/bin/env python3
"""Rename existing uploads on filmy.prehrajto@post.cz to match the new naming
convention (CZ_DUB → "Title (year) CZ Dabing"; CZ_NATIVE+lang=sk → SK; else CZ).

Walks `state/uploaded.json` and the freshly exported backlog, finds entries
whose `display_name` no longer matches what `export_backlog.display_name()`
would produce now, and POSTs to prehraj.to's rename endpoint:

    POST https://prehraj.to/profil/nahrana-videa
        ?uploadedVideoListing-videoId={video_id}
        &do=uploadedVideoListing-changeVideoName
    body: uploadedVideoListing-name={new_name}

State is rewritten with the new display_name + a `renamed_at` timestamp on
each touched entry, so the script is idempotent.

Usage:
    PREHRAJTO_EMAIL=... PREHRAJTO_PASSWORD=... python3 src/rename_uploaded.py
"""

from __future__ import annotations

import datetime
import gzip
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prehrajto_upload import login, ACCEPT_LANG, SEC_CH_UA  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = REPO_ROOT / "state" / "uploaded.json"
BACKLOG = REPO_ROOT / "backlog" / "prehrajto-films.jsonl.gz"


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_backlog_index() -> dict[int, str]:
    """cr_film_id → expected display_name (per current export rules)."""
    index: dict[int, str] = {}
    with gzip.open(BACKLOG, "rt", encoding="utf-8") as fh:
        for line in fh:
            f = json.loads(line)
            index[f["cr_film_id"]] = f["display_name"]
    return index


def rename(session, video_id: int, new_name: str) -> None:
    url = (f"https://prehraj.to/profil/nahrana-videa"
           f"?uploadedVideoListing-videoId={video_id}"
           f"&do=uploadedVideoListing-changeVideoName")
    r = session.post(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Language": ACCEPT_LANG,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://prehraj.to",
            "Referer": "https://prehraj.to/profil/nahrana-videa",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        },
        data={"uploadedVideoListing-name": new_name},
        timeout=30,
    )
    r.raise_for_status()


def main() -> int:
    email = os.environ.get("PREHRAJTO_EMAIL")
    password = os.environ.get("PREHRAJTO_PASSWORD")
    if not email or not password:
        print("ERROR: PREHRAJTO_EMAIL / PREHRAJTO_PASSWORD required", file=sys.stderr)
        return 2

    state = json.loads(STATE.read_text())
    expected = load_backlog_index()

    pending = []
    for entry in state.get("uploads", []):
        cur = entry["display_name"]
        new = expected.get(entry["cr_film_id"])
        if new and new != cur:
            pending.append((entry, cur, new))

    if not pending:
        print("Nothing to rename — all uploads already match current naming.")
        return 0

    print(f"Renaming {len(pending)} uploads:")
    for _, cur, new in pending:
        print(f"  {cur!r}  →  {new!r}")

    session = login(email, password)

    for entry, cur, new in pending:
        vid = entry["prehrajto_video_id"]
        print(f"  POST rename video_id={vid} → {new!r}")
        rename(session, vid, new)
        entry["display_name"] = new
        entry["renamed_at"] = now_iso()

    state["last_updated"] = now_iso()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    print(f"Done — state/uploaded.json updated, {len(pending)} entries renamed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
