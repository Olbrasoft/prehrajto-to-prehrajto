#!/usr/bin/env python3
"""Generate rewritten email-account descriptions with Gemma."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "state/email-generated-descriptions-v3.jsonl"
MODEL = "gemma-4-31b-it"
MAX_WORKERS = 4


def load_keys() -> list[str]:
    keys: list[str] = []
    for path in [
        Path("/home/jirka/Dokumenty/přístupy/gemini.txt"),
        Path("/home/jirka/Dokumenty/přístupy/api-keys.md"),
    ]:
        if not path.exists():
            continue
        for key in re.findall(r"AIza[0-9A-Za-z_-]{20,}", path.read_text(errors="ignore")):
            if key not in keys:
                keys.append(key)
    preferred = [keys[i] for i in [0, 5, 7, 8] if i < len(keys)]
    return preferred or keys[:4]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_existing_ids() -> set[int]:
    existing: set[int] = set()
    if not OUT.exists():
        return existing
    for line in OUT.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") == "ok" and row.get("new_description"):
            existing.add(int(row["cr_film_id"]))
    return existing


def load_burned_upload_ids() -> set[str]:
    burned: set[str] = set()
    for shard in [0, 1]:
        path = ROOT / f"state/email-uploaded-shard-{shard}.json"
        if not path.exists():
            continue
        state = json.loads(path.read_text(encoding="utf-8"))
        burned.update(
            str(a["upload_id"])
            for a in state.get("failed_attempts", [])
            if a.get("permanent")
        )
    return burned


def select_rows(limit: int) -> list[dict]:
    existing = load_existing_ids()
    burned = load_burned_upload_ids()
    rows: list[dict] = []
    with gzip.open(ROOT / "backlog/prehrajto-films.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            film = json.loads(line)
            cr_film_id = int(film["cr_film_id"])
            if cr_film_id in existing:
                continue
            if not (film.get("description") or "").strip():
                continue
            if not any(str(c["upload_id"]) not in burned for c in film.get("candidates", [])):
                continue
            rows.append(film)
            if len(rows) >= limit:
                break
    return rows


def extract_text(data: dict) -> str:
    texts: list[str] = []
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("thought"):
                continue
            text = part.get("text") or ""
            if text.strip():
                texts.append(text.strip())
    return "\n".join(texts).strip()


def clean(text: str) -> str:
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip())
    text = re.sub(r"^(Nový popis|Popis|Výstup)\s*:\s*", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(' "')


def valid(text: str) -> tuple[bool, str]:
    if not (150 <= len(text) <= 700):
        return False, f"bad_length_{len(text)}"
    lowered = text.lower()
    banned = [
        "zde je",
        "tady je",
        "nový popis",
        "přepsaný popis",
        "originální popis",
        "tmdb",
    ]
    if any(item in lowered for item in banned):
        return False, "meta_text"
    if text[-1] not in ".?!…":
        return False, "bad_end"
    return True, "ok"


class KeyRotator:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        self.next_key = 0
        self.lock = threading.Lock()

    def get(self) -> str:
        with self.lock:
            key = self.keys[self.next_key % len(self.keys)]
            self.next_key += 1
            return key


def generate(film: dict, keys: KeyRotator) -> dict:
    source = (film.get("description") or "").strip()
    title = film.get("title") or film.get("display_name") or ""
    year = film.get("year") or ""
    prompt = (
        "Přepiš český filmový popis vlastními slovy pro katalog filmů. "
        "Zachovej fakta, názvy postav a děj, ale nepoužívej stejné věty jako zdroj. "
        "Piš přirozenou češtinou, 3 až 5 vět, bez nadpisu, bez odrážek, "
        "bez marketingových frází. Nezmiňuj, že jde o přepis.\n\n"
        f"Film: {title} ({year})\n"
        f"Zdrojový popis: {source}\n\n"
        "Nový popis:"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "topP": 0.95, "maxOutputTokens": 2000},
    }
    last_error = "unknown"
    for attempt in range(4):
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={keys.get()}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))
            text = clean(extract_text(data))
            ok, reason = valid(text)
            if ok:
                return {
                    "cr_film_id": int(film["cr_film_id"]),
                    "cr_slug": film.get("cr_slug"),
                    "title": film.get("title"),
                    "year": film.get("year"),
                    "display_name": film.get("display_name"),
                    "source_description": source,
                    "new_description": text,
                    "status": "ok",
                    "model": MODEL,
                    "generated_at": now(),
                }
            last_error = reason
        except urllib.error.HTTPError as exc:
            body = exc.read(160).decode("utf-8", "ignore")
            last_error = f"http_{exc.code}:{body}"
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(2 + attempt * 2 + random.random())
                continue
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}:{str(exc)[:120]}"
            time.sleep(1 + attempt + random.random())
    return {
        "cr_film_id": int(film["cr_film_id"]),
        "cr_slug": film.get("cr_slug"),
        "title": film.get("title"),
        "year": film.get("year"),
        "display_name": film.get("display_name"),
        "source_description": source,
        "new_description": "",
        "status": "fail",
        "error": last_error,
        "model": MODEL,
        "generated_at": now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=120)
    args = parser.parse_args()

    api_keys = load_keys()
    if not api_keys:
        raise SystemExit("no API keys found")

    rows = select_rows(args.count)
    print(
        f"target_rows={len(rows)} existing_ok={len(load_existing_ids())} "
        f"first_ids={[r['cr_film_id'] for r in rows[:10]]}",
        flush=True,
    )
    key_rotator = KeyRotator(api_keys)
    lock = threading.Lock()
    done = ok = fail = 0
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(generate, film, key_rotator) for film in rows]
        for future in cf.as_completed(futures):
            row = future.result()
            with lock, OUT.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            done += 1
            ok += int(row.get("status") == "ok")
            fail += int(row.get("status") != "ok")
            if done == 1 or done % 10 == 0:
                print(f"progress completed={done}/{len(rows)} ok={ok} fail={fail}", flush=True)
    print(f"done completed={done} ok={ok} fail={fail}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
