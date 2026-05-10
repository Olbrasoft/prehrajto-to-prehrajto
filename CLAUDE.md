# CLAUDE.md — handoff pro Claude Code

Tento repo je **fáze 1** re-upload pipeline z prehraj.to (cizí uploady) na náš
účet `filmy.prehrajto@post.cz`. Sister repos:

- `~/GitHub/Olbrasoft/prehrajto-sync` — sktorrent → prehraj.to (zdroj
  `prehrajto_upload.py`, který sem kopírujeme verbatim)
- `~/GitHub/Olbrasoft/sledujtetocz-to-prehrajto` — sledujteto.cz → prehraj.to
  (vzor pro orchestrátor + workflow yaml)
- `~/GitHub/Olbrasoft/cr` — CR DB s tabulkou `film_prehrajto_uploads`
  (zdroj backlogu)

## Conventions

### Phase 1 filtr
- Pouze `lang_class IN ('CZ_DUB','CZ_NATIVE')` a `is_alive`.
- 17 650 filmů, 76 336 kandidátních uploadů. Některé uploady mají 1 zdroj,
  jiné až 9 — orchestrátor zkusí top kandidáty po pořadí (preferred
  lang_class → resolution_hint → view_count).

### Naming na našem účtu
- `CZ_NATIVE` → `Titul (rok)`
- `CZ_DUB`    → `Titul (rok) Dabing`

### Source preference
1. 1080p MP4 z player stream URL
2. 720p MP4 fallback
3. Cokoliv co je
- Originální `Stáhnout soubor` (4K mkv, až 71 GB) **nepoužíváme** — neprošlo
  by GitHub Actions diskem (14 GB) a nedává nám to nic navíc, prehraj.to
  player stejně streamuje max 1080p.

## Read-only proti CR prod DB

CR produkce je **přísně read-only**. Připojuj přes SSH tunel; heslo
najdeš v `~/Dokumenty/přístupy/hosting.md` (lokální gitignored vault) nebo
v `/etc/cr.env` na serveru — **nikdy ho nepiš sem do repa**.

```bash
ssh -p 2222 -L 15432:127.0.0.1:5432 -N -f root@46.225.101.253
DATABASE_URL='postgres://cr:$CR_DB_PASSWORD@127.0.0.1:15432/cr?options=-c default_transaction_read_only=on'
```

`default_transaction_read_only=on` je hardcoded pojistka — i kdyby si
omylem napsal UPDATE, server ho odmítne. Po skončení **vždy zavři tunel**:

```bash
ps -ef | grep "ssh -p 2222" | grep -v grep | awk '{print $2}' | xargs -r kill
```

## CDN URL TTL

Player stream URL z `pp-storageN.premiumcdn.net` mají `?expires=` query
parametr s TTL ~24 h. **Nikdy je necachuj mezi batchi** — vždy resolvuj
těsně před downloadem.

## GitHub Actions runner constraints

- 14 GB disk → max 1 film naráz, po uploadu smazat.
- 350 min batch timeout.
- Azure US datacenter IPs — některé CDN hosty datacentry blokují. Před
  spuštěním produkčního batche ověř `test-cdn-access.yml` (TODO).

## Stav

Hotové: `export_backlog.py`, `resolve_stream.py`, `download.py`, backlog
gzipped commitnutý.

Zbývá: `prehrajto_upload.py` (kopie), `pick_next_film.py`, `sync_batch.py`,
`.github/workflows/sync.yml`, `test-cdn-access.yml`.
