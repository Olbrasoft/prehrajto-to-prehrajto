#!/usr/bin/env python3
"""Export Phase 1 backlog of films to re-upload from prehraj.to to our account.

Reads from CR DB (`films` + `film_prehrajto_uploads`) and emits JSONL where
each line is one film with all CZ_DUB/CZ_NATIVE candidate uploads. The actual
"best upload" pick happens later in the pipeline, after probing the player
page for real stream resolution (resolution_hint in DB is too sparse — only
~9% rows have a value).

Usage:
    DATABASE_URL=postgres://<user>:<password>@<host>/<db> \
        python -m src.export_backlog --out backlog/prehrajto-films.jsonl
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
from collections import defaultdict
from typing import Any

import psycopg2
import psycopg2.extras

PHASE1_LANG_CLASSES = ("CZ_DUB", "CZ_NATIVE")


def display_name(title: str, year: int | None, lang_class: str, film_lang: str | None) -> str:
    """Naming convention on filmy.prehrajto@post.cz uploads.

    - CZ_DUB upload  → "Title (year) CZ Dabing"   (always — film was dubbed to Czech)
    - CZ_NATIVE upload, film_lang == 'sk' → "Title (year) SK"  (Slovak-origin)
    - CZ_NATIVE upload, anything else     → "Title (year) CZ"  (Czech-origin or unknown)

    `film_lang` comes from films.lang (TMDB original_language). It's only
    populated for ~7.5 % of CR films, so the fallback is "CZ" — matches
    the user's request that Czech-origin defaults; explicit 'sk' is the
    only signal we trust for SK suffix.
    """
    base = f"{title} ({year})" if year else title
    if lang_class == "CZ_DUB":
        return f"{base} CZ Dabing"
    if (film_lang or "").lower() == "sk":
        return f"{base} SK"
    return f"{base} CZ"


def fetch_rows(conn) -> list[dict[str, Any]]:
    sql = """
        SELECT
            f.id              AS cr_film_id,
            f.slug            AS cr_slug,
            f.title,
            f.original_title,
            f.year,
            f.lang            AS film_lang,
            f.tmdb_id,
            f.description,
            f.tmdb_poster_path,
            f.imdb_id,
            f.imdb_rating,
            u.upload_id,
            u.url,
            u.title           AS upload_title,
            u.lang_class,
            u.resolution_hint,
            u.duration_sec,
            u.view_count,
            u.discovered_at,
            u.last_seen_at
        FROM films f
        JOIN film_prehrajto_uploads u ON u.film_id = f.id
        WHERE u.is_alive
          AND u.lang_class = ANY(%s)
        ORDER BY f.id, u.lang_class, u.discovered_at;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (list(PHASE1_LANG_CLASSES),))
        return cur.fetchall()


def group_by_film(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_film: dict[int, dict[str, Any]] = {}
    for r in rows:
        fid = r["cr_film_id"]
        if fid not in by_film:
            by_film[fid] = {
                "cr_film_id": fid,
                "cr_slug": r["cr_slug"],
                "title": r["title"],
                "original_title": r["original_title"],
                "year": r["year"],
                "film_lang": r["film_lang"],
                "tmdb_id": r["tmdb_id"],
                "imdb_id": r["imdb_id"],
                "imdb_rating": r["imdb_rating"],
                "tmdb_poster_path": r["tmdb_poster_path"],
                "description": r["description"],
                "preferred_lang_class": r["lang_class"],  # may be promoted below
                "candidates": [],
            }
        else:
            # CZ_DUB wins over CZ_NATIVE — if any candidate is explicitly
            # marked as a Czech dub, the film is almost certainly a foreign
            # title with a Czech dub track. CZ_NATIVE here just means "no
            # dub marker in the upload title" which is noisy for big foreign
            # films like Temný rytíř (1 CZ_NATIVE upload, 14 CZ_DUB).
            if r["lang_class"] == "CZ_DUB" and by_film[fid]["preferred_lang_class"] == "CZ_NATIVE":
                by_film[fid]["preferred_lang_class"] = "CZ_DUB"

        by_film[fid]["candidates"].append({
            "upload_id": r["upload_id"],
            "url": r["url"],
            "upload_title": r["upload_title"],
            "lang_class": r["lang_class"],
            "resolution_hint": r["resolution_hint"],
            "duration_sec": r["duration_sec"],
            "view_count": r["view_count"],
            "discovered_at": r["discovered_at"].isoformat() if r["discovered_at"] else None,
            "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
        })

    # Sort candidates: preferred lang first, then 1080p hints first, then most viewed.
    # Compute display_name now that preferred_lang_class is finalised across all rows.
    res_score = {"1080p": 3, "720p": 2, "bluray": 2, "bdrip": 2, "web-dl": 2, "webrip": 2, "hdrip": 1}
    for film in by_film.values():
        pref = film["preferred_lang_class"]
        film["display_name"] = display_name(film["title"], film["year"], pref, film["film_lang"])
        film["candidates"].sort(
            key=lambda c: (
                0 if c["lang_class"] == pref else 1,
                -res_score.get((c["resolution_hint"] or "").lower(), 0),
                -(c["view_count"] or 0),
            )
        )
    return sorted(by_film.values(), key=lambda f: f["cr_film_id"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="backlog/prehrajto-films.jsonl")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--limit", type=int, help="Cap output to N films (debug).")
    args = parser.parse_args()

    if not args.db_url:
        print("ERROR: --db-url or DATABASE_URL required", file=sys.stderr)
        return 2

    conn = psycopg2.connect(args.db_url)
    try:
        rows = fetch_rows(conn)
    finally:
        conn.close()

    films = group_by_film(rows)
    if args.limit:
        films = films[: args.limit]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    opener = gzip.open if args.out.endswith(".gz") else open
    with opener(args.out, "wt", encoding="utf-8") as fh:
        for film in films:
            fh.write(json.dumps(film, ensure_ascii=False) + "\n")

    lang_counts: dict[str, int] = defaultdict(int)
    for f in films:
        lang_counts[f["preferred_lang_class"]] += 1

    print(f"Wrote {len(films)} films to {args.out}")
    for lc, n in sorted(lang_counts.items()):
        print(f"  {lc}: {n}")
    print(f"  total candidate uploads: {sum(len(f['candidates']) for f in films)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
