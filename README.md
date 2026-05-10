# prehrajto-to-prehrajto

Re-upload mirror filmů z prehraj.to (cizí uploady) do našeho účtu
`filmy.prehrajto@post.cz`. Sourcuje z [`ceskarepublika.wiki`](https://ceskarepublika.wiki)
DB (tabulka `film_prehrajto_uploads`, naplněná z prehraj.to sitemapy) a uploaduje
přes stejný flow jako [`prehrajto-sync`](https://github.com/Olbrasoft/prehrajto-sync)
(přímý multipart POST na `api.premiumcdn.net/upload/`).

## Architektura

```
┌─ GitHub Actions cron / dispatch (ubuntu-latest, 14 GB disk) ────────┐
│                                                                     │
│  1. git pull                                                        │
│  2. pick_next_film.py     → další cr_film_id, který není ve state   │
│  3. resolve_stream.py     → 1080p MP4 URL z prehraj.to detail page  │
│  4. download.py           → curl /tmp/film.mp4 (~1.5–3 GB, 35-60 s) │
│  5. prehrajto_upload.py   → multipart POST na náš účet              │
│  6. git commit state/uploaded.json + push                           │
└─────────────────────────────────────────────────────────────────────┘
```

## Phase 1 scope (2026-05)

- Jazykové třídy: `CZ_DUB` + `CZ_NATIVE` z `film_prehrajto_uploads`.
  SK a CZ_SUB jsou na později.
- Source preference: 1080p > 720p > nejvyšší co existuje. Stahujeme
  **player stream URL** (`pp-storageN.premiumcdn.net/.../*.mp4`), což je
  prehraj.to-transcoded MP4 ~1.5–3 GB. Originální mkv (4K, až 71 GB)
  nepoužíváme.
- Naming na našem účtu:
  - `CZ_NATIVE`: `Titul (rok)`
  - `CZ_DUB`:    `Titul (rok) Dabing`
- Popis: z TMDB (cachováno v CR DB).
- Phase 1 pool: **17 650 filmů** (export z produkční CR DB 2026-05-10).

## Repo layout

```
backlog/
  prehrajto-films.jsonl.gz   17 650 filmů, gzipped JSONL (~8 MB)
src/
  export_backlog.py          CR DB → JSONL (read-only přes SSH tunel na prod)
  resolve_stream.py          prehraj.to URL → 1080p MP4 URL
  download.py                MP4 URL → /tmp/film.mp4 (curl + Range)
  pick_next_film.py          (TODO) backlog ∖ state → next film
  sync_batch.py              (TODO) batch orchestrátor
  prehrajto_upload.py        (TODO) zkopírováno z prehrajto-sync
state/
  uploaded.json              persistent state: {cr_film_id: {…}}
.github/workflows/
  sync.yml                   (TODO) workflow_dispatch + cron
```

## Quickstart

```bash
pip install -r requirements.txt

# Refresh backlog (read-only proti prod CR DB přes SSH tunel).
# DB heslo viz ~/Dokumenty/přístupy/hosting.md (lokální gitignored vault).
ssh -p 2222 -L 15432:127.0.0.1:5432 -N -f root@46.225.101.253
DATABASE_URL='postgres://cr:$CR_DB_PASSWORD@127.0.0.1:15432/cr?options=-c default_transaction_read_only=on' \
  python3 -m src.export_backlog --out backlog/prehrajto-films.jsonl.gz

# Test resolveru
python3 src/resolve_stream.py "https://prehraj.to/.../upload_id"

# Test downloaderu
python3 src/download.py "https://pf-storage4.premiumcdn.net/...mp4" /tmp/x.mp4
```

## Credentials

- Prehraj.to upload účet: `filmy.prehrajto@post.cz` (heslo viz
  `~/Dokumenty/přístupy/prehrajto.md`).
- E-mail post.cz schránka přesměrovaná na `tuma.rsrobot@gmail.com`
  (viz `~/Dokumenty/přístupy/email-accounts.md`).
- CR prod DB: SSH tunel přes `46.225.101.253:2222`, **read-only**.

## Licence

Interní projekt Olbrasoft. Žádná veřejná licence.
