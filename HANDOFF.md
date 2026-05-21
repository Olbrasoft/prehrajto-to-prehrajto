# HANDOFF — prehrajto-to-prehrajto (Phase 1 re-upload pipeline)

Stav předávky: **2026-05-21**. Tento dokument popisuje, co je hotovo,
kde jsem skončil a kde můžeš pokračovat. Před prvním spuštěním si přečti
celý dokument — některé věci (TTL CDN URL, kapacita runneru, geofence proxy,
GA cancellation na 60 min) nejsou zjevné z kódu.

---

## TL;DR

- Cíl: re-upload **17 650** filmů (lang_class `CZ_DUB` + `CZ_NATIVE`,
  `is_alive=true`) z prehraj.to cizích uploadů na náš účet
  `filmy.prehrajto@post.cz`.
- Stav k 2026-05-21: **9 877 / 17 650 (55,96 %)** nahráno → **7 773 zbývá**.
- Tempo posledních 24 h: ~43 filmů/h ⇒ ETA při zachování tempa **~7,5 dne**.
- Nahrávání je momentálně **zastavené** (uživatel požádal o pauzu):
  dispatcher loop zabit, dvě in-flight workflow běhy zrušeny.
- **Cron `0 */6 * * *` v `sync.yml` je ale stále aktivní** — pokud nechceš,
  aby každých 6 h vystřelila další dávka, viz sekci „Jak úplně zastavit".

---

## Kde najdeš co (souborový rozcestník)

```
prehrajto-to-prehrajto/
├── CLAUDE.md                              # konvence Phase 1, source preference, naming
├── HANDOFF.md                             # tento dokument
├── backlog/
│   └── prehrajto-films.jsonl.gz           # 17 650 řádků, 1 řádek = 1 film + jeho kandidáti
├── src/
│   ├── pick_next_film.py                  # výběr dalšího filmu pro shard
│   ├── resolve_stream.py                  # HTML scrape přes CZ proxy → 1080p/720p MP4 URL
│   ├── download.py                        # curl, 6 GB cap, HEAD pre-check
│   ├── prehrajto_upload.py                # login + multipart upload (verbatim copy ze sister repa)
│   ├── sync_batch.py                      # ORCHESTRÁTOR, push_state po každém filmu
│   ├── split_state_by_shard.py            # jednorázová migrace mono-state → per-shard state
│   ├── export_backlog.py                  # generuje backlog z CR DB (read-only)
│   └── rename_uploaded.py                 # už nepoužívám (legacy)
├── state/
│   ├── uploaded.json                      # LEGACY pre-sharding (zamrazený snapshot ke 2026-05-12)
│   ├── uploaded-shard-0.json              # AKTIVNÍ — 4 948 uploadů + 725 failed_attempts
│   ├── uploaded-shard-1.json              # AKTIVNÍ — 4 929 uploadů + 236 failed_attempts
│   ├── sync.log                           # LEGACY log (před shardingem)
│   ├── sync-shard-0.log                   # aktivní log shard 0
│   └── sync-shard-1.log                   # aktivní log shard 1
└── .github/workflows/
    ├── sync.yml                           # 2× matrix shard, cron 6h, batch_size 20
    └── test-cdn-access.yml                # ad-hoc test, zatím nepoužitý
```

---

## Architektura pipeline

### Jeden „batch run" = jeden GitHub Actions job

Workflow `sync.yml` má `strategy.matrix.shard: [0, 1]` ⇒ jeden dispatch
spustí **dva paralelní jobs** (shard 0 a shard 1). Každý job:

1. `checkout main`, `git fetch + reset --hard origin/main` (kvůli queue lag).
2. Login na prehraj.to (`PREHRAJTO_EMAIL`/`PREHRAJTO_PASSWORD` secrets).
3. Loop `--count 20` (default batch_size):
   - `pick_next_film.py` vrátí film, kde `cr_film_id % 2 == SYNC_SHARD_ID`
     a který ještě není v shard state souboru a má aspoň jeden „nevypálený"
     kandidát (kandidát se vypálí, když selže `permanent=True`: 404, oversize,
     dead, geoblock).
   - Pro každý kandidát v prioritním pořadí: resolve → HEAD size check →
     download (`/tmp`, max 6 GB) → upload → `save_state` → **`push_state`**
     (`git commit + git push`, 5 retry pro race s druhým shardem).
4. Závěrečný `Commit state + log` jako pojistka pro případ, že `push_state`
   selhal uprostřed (různé soubory, takže rebase mezi shardy automaticky
   konflikty neřeší — žádné nejsou).

### Per-shard state

- Klíč rozdělení: `cr_film_id % NUM_SHARDS == SHARD_ID`. NUM_SHARDS=2,
  SHARD_ID=0|1.
- Každý shard čte/píše **jen svůj** `uploaded-shard-{N}.json` ⇒ dva runnery
  nezávodí o ten samý soubor.
- `state/uploaded.json` (legacy, mono-state) zůstal pro historii zamrznutý
  z 2026-05-12. Sharded reader ho ignoruje, jakmile `SYNC_NUM_SHARDS > 1`.
- Migrace se dělala přes `src/split_state_by_shard.py 2`. **Znovu už ji
  nespouštěj** — přepsala by aktuální shard soubory na zamrazený 2026-05-12
  obsah.

### `push_state` (crash-safe progress)

GitHub Actions na tomto účtu začalo občas zabíjet dlouhé joby v okolí
60 min (pravděpodobně peak-hours throttle nebo runner preemption — root
cause nejasný). Final `Commit state + log` step se přitom přeskočí, takže
**bez push_state by 30 nahraných filmů z půlce dávky bylo ztracených**.

Proto `sync_batch.py::push_state(reason)`:
- staguje shard state + log, commitne `chore(shard N/2): +Title (cr_film_id=X)`,
- 5× retry `pull --rebase + push` na race s druhým shardem (rebase je
  vždy bez konfliktu, protože píšou do různých souborů; ztrácí se jen
  ref-update race),
- selže-li i pátý pokus, pokračuje dál — další iterace push dohoní.

Výsledek: ztráta max 1 filmu na cancellation místo celé batche.

### `MAX_FILE_SIZE = 6 GB` (src/download.py)

Runner má ~14 GB free disk. Praxe: cokoli nad ~7 GB padá v upload fázi
(soubor drží na disku do dokončení uploadu + multipart POST má vlastní
tmp buffery + OS s nástroji už zabírají ~3 GB).
- Konkrétně `cr_film_id=822` (Smrtonosná past 3, 9,6 GB 1080p) shodil
  dvě dávky v řadě.
- Cap `6 GB` znamená, že oversize kandidát se označí `permanent=True`
  a orchestrátor padne na další kandidát (většina filmů má 720p variantu
  v rozsahu 1,5–3 GB).
- **Nezvyšuj nad 6 GB**, dokud nezměníš runner na self-hosted s víc diskem.

### CDN URL TTL ~24 h

Player stream URL z `pp-storageN.premiumcdn.net` má query parametr
`?expires=...` s TTL ~24 h. **Nikdy je necachuj mezi batchi.**
`sync_batch.py` resolvuje immediately před downloadem ⇒ správně.

### Geofence — proč CZ proxy

- prehraj.to **HTML scrape** (detail page, resolve player stream) je
  geofenced na CZ residential IPs. GA runnery jsou v Azure US ⇒ vrací
  block page nebo 403.
- Cesta: `resolve_stream.py` posílá `GET https://prehraj.to/foo` přes
  `CZ_PROXY_URL` (chobotnice.aspfree.cz/Proxy.ashx, stejný ASP relay
  jako cr-web's stream resolver), autentizuje hlavičkou `CZ_PROXY_KEY`.
- **Login (POST /login) a multipart upload (POST /upload)** geofence
  nemají ⇒ jdou napřímo. Ověřeno v sister repo `prehrajto-sync`.

### Naming na našem účtu

- `CZ_NATIVE` → `Titul (rok)`
- `CZ_DUB` → `Titul (rok) Dabing`
- Implementuje `export_backlog.py` při tvorbě `display_name` v backlogu.

### Source preference (`pick_best` v resolve_stream.py)

1. 1080p MP4 z player stream URL.
2. 720p MP4 fallback.
3. Cokoli, co se dá rezolvovat.
4. **Originální "Stáhnout soubor"** (4K mkv až 71 GB) **se nepoužívá** —
   nevejde se do runneru a prehraj.to player stejně streamuje max 1080p,
   takže to nic nepřináší.

---

## Failed attempts — co znamenají

V `state/uploaded-shard-{N}.json` je sekce `failed_attempts`, suma napříč
oběma shardy: **961 záznamů, z toho 462 permanent**.

- **permanent=True** (462) = upload_id se víc nezkusí. Důvody:
  - `oversize: ... > 6_000_000_000` (cap),
  - `resolve_failed: 404` / `dead`,
  - `resolve_failed: geo_block` (nemělo by se dít přes proxy, ale občas
    se to projeví).
- **permanent=False** (499) = tranzientní (proxy 502, upload timeout,
  download error). Při dalším spuštění se může zkusit znovu, ale
  `in-batch exclude` (`extra_exclude`) brání zacyklení v rámci jedné dávky.

Filmů, kde **všichni kandidáti** jsou permanent-burned, neexistuje
v rámci jedné dávky — `pick_next` skipne. Skutečně „mrtvé" filmy
(žádný živý kandidát) zůstanou ve frontě donekonečna, ale prakticky se
nepíchnou, protože filter projede backlog celý a vrátí None.

**Co s tím v budoucnu**: až dojde nahrávání, hodí se proanalyzovat
failed_attempts a manuálně rozhodnout (a) které lze obejít zmenšením
MAX_FILE_SIZE capu zpět na vyšší (selfhosted runner), (b) které jsou
prokazatelně mrtvé i v CR DB (sloupec `is_alive` se updatuje samostatným
syncem v sister repu).

---

## Read-only přístup k CR DB

CR produkce je **přísně read-only**. Heslo NIKDY nepiš sem do repa.

- Heslo: `~/Dokumenty/přístupy/hosting.md` (lokální gitignored vault)
  nebo `/etc/cr.env` na produkčním serveru.
- SSH tunel:
  ```bash
  ssh -p 2222 -L 15432:127.0.0.1:5432 -N -f root@46.225.101.253
  DATABASE_URL='postgres://cr:$CR_DB_PASSWORD@127.0.0.1:15432/cr?options=-c default_transaction_read_only=on'
  ```
- `default_transaction_read_only=on` je hardcoded pojistka — i kdyby si
  omylem napsal UPDATE, server ho odmítne.
- **Po skončení vždy zavři tunel:**
  ```bash
  ps -ef | grep "ssh -p 2222" | grep -v grep | awk '{print $2}' | xargs -r kill
  ```
- Backlog se regeneruje přes `src/export_backlog.py` — ale to potřebovat
  nebudeš, dokud nedoběhne Phase 1, protože zamrazený gzip pokrývá
  vše. (Případné nové filmy v CR od 2026-05-10 sem netečou — Phase 2.)

---

## GitHub Actions konfigurace

- Workflow soubor: `.github/workflows/sync.yml`
- Dispatch inputs:
  - `batch_size` (default 20) — kolik filmů per shard per job.
  - `num_shards` (default 2) — počet paralelních shardů.
- Schedule: `cron: "0 */6 * * *"` — **každých 6 h vystřelí matrix job**
  (oba shardy najednou). To **běží i bez dispatcher loopu**.
- Concurrency: `prehrajto-sync-shard-${{ matrix.shard }}` per shard,
  `cancel-in-progress: false` ⇒ druhý dispatch ve frontě čeká,
  neforce-killne probíhající.
- Timeout: 350 min (ale prakticky se končí na ~55 min kvůli batch_size).
- Secrets v repu (`gh secret list -R Olbrasoft/prehrajto-to-prehrajto`):
  - `PREHRAJTO_EMAIL` = `filmy.prehrajto@post.cz`
  - `PREHRAJTO_PASSWORD`
  - `CZ_PROXY_URL`
  - `CZ_PROXY_KEY`

---

## Jak zase rozjet nahrávání

### A) Manuální jednorázový dispatch (nejjednodušší, doporučeno pro start)

```bash
cd /home/jirka/Olbrasoft/prehrajto-to-prehrajto
gh workflow run sync.yml -f batch_size=20 -f num_shards=2
sleep 6
gh run list --workflow=sync.yml --limit=4
```

Spustí jednu matrix dvojici (shard 0 + shard 1), nahraje cca 2×20=40
filmů za ~55 min, hotovo. Další dispatch musíš spustit ručně.

### B) Kontinuální loop (jak to běželo doposud)

```bash
# Skript je v /tmp/sync_loop_sharded.sh (přežil reboot? Možná ne. Pak ho
# znovu nalijež — text níže.)
LAST_RUN=$(gh run list --workflow=sync.yml --limit=1 --json databaseId --jq '.[0].databaseId')
nohup bash /tmp/sync_loop_sharded.sh "$LAST_RUN" 20 2 >/tmp/sync_loop.log 2>&1 &
disown
```

Obsah `sync_loop_sharded.sh` (pro případ, že už neexistuje):

```bash
#!/bin/bash
set +e
cd /home/jirka/Olbrasoft/prehrajto-to-prehrajto
LAST_RUN=$1
BATCH=${2:-20}
SHARDS=${3:-2}
while true; do
  echo "[$(date -u +%H:%M:%S)] watching run $LAST_RUN"
  until [ "$(gh run view "$LAST_RUN" --json status --jq .status 2>/dev/null)" = "completed" ]; do
    sleep 90
  done
  CONCL=$(gh run view "$LAST_RUN" --json conclusion --jq .conclusion 2>/dev/null)
  echo "[$(date -u +%H:%M:%S)] run $LAST_RUN done conclusion=$CONCL"
  git pull --rebase origin main >/dev/null 2>&1 || true
  gh workflow run sync.yml -f batch_size=$BATCH -f num_shards=$SHARDS
  sleep 8
  LAST_RUN=$(gh run list --workflow=sync.yml --limit=1 --json databaseId --jq '.[0].databaseId')
  echo "[$(date -u +%H:%M:%S)] new run id=$LAST_RUN"
done
```

Loop hlídá poslední run, čeká na dokončení, pullne nový state,
disptachne další matrix a posune se na nový run-id. Není nutný — cron
sám každých 6 h střelí, ale loop dělá throughput 4–5× vyšší
(~40 filmů/h vs. ~10 filmů/h z cronu).

### C) Jen cron, žádné ruční dispatchování

Nic nedělej. Každých 6 h se spustí matrix dvojice automaticky. Tempo
~10 filmů/h, ETA na zbylých 7 773 filmů: ~32 dní.

---

## Jak úplně zastavit

1. **Loop dispatcher**: `pkill -f sync_loop_sharded.sh`
   (kontrola: `ps -ef | grep sync_loop | grep -v grep` — musí být prázdné).
2. **Cron schedule**: edituj `.github/workflows/sync.yml`, zakomentuj
   `schedule:` blok (řádky 14–17), commit + push. Bez toho cron jede dál.
3. **In-flight běhy**: `gh run list --workflow=sync.yml --status in_progress`
   a `gh run cancel <ID>` pro každý. Cancellation se vrátí jako "failed"
   ve wake hooku, ale to je očekávané.
4. **Nezapomeň**, že queued runy v concurrency frontě se neviditelně drží
   až 5 h — pokud chceš opravdu hard stop, vypni schedule v UI:
   `https://github.com/Olbrasoft/prehrajto-to-prehrajto/actions/workflows/sync.yml`
   → tlačítko `...` → `Disable workflow`.

---

## Lokální vývojové prostředí

```bash
cd /home/jirka/Olbrasoft/prehrajto-to-prehrajto
python3 -m venv .venv  # pokud ještě nemáš
source .venv/bin/activate
pip install -r requirements.txt

# test pick_next_film bez side-effectu
SYNC_NUM_SHARDS=2 SYNC_SHARD_ID=0 python3 src/pick_next_film.py
SYNC_NUM_SHARDS=2 SYNC_SHARD_ID=1 python3 src/pick_next_film.py

# dry-run jedné dávky lokálně (potřebuje secrets + CZ proxy)
export PREHRAJTO_EMAIL='filmy.prehrajto@post.cz'
export PREHRAJTO_PASSWORD='...'  # vault!
export CZ_PROXY_URL='...'
export CZ_PROXY_KEY='...'
export SYNC_NUM_SHARDS=2
export SYNC_SHARD_ID=0
# CI env var nutí push_state aktivovat se; lokálně NEnastavuj — jinak
# bude orchestrátor commitovat každý úspěšný film.
unset CI
python3 src/sync_batch.py --count 1
```

---

## Známá rizika a gotchy

1. **Při znovurozjetí pull napřed**: lokální `state/uploaded-shard-*.json`
   může být zastaralý oproti `origin/main` (push_state v jobech pushuje
   za nás). Vždy `git pull --rebase origin main` před manuálním
   dispatchováním nebo před restartem dispatcher loopu.

2. **`gh run cancel` se ve wake hooku zobrazí jako `ci-failure`**. To
   není skutečná chyba kódu — je to artefakt toho, jak ghnotify maapuje
   cancelled→failure. Nereaguj na to push fixem; cancelled runy jsou
   účelové.

3. **Cron schedule jede paralelně s ručním dispatchem**. Pokud dispatchneš
   v 5:59 a cron vystřelí v 6:00, druhý běh queueuje na concurrency lock
   a spustí se ihned po prvním ⇒ 2 dávky za sebou, ne jedna. Není to bug,
   jen překvapení.

4. **Pokud změníš `num_shards` z 2 na něco jiného**, musíš:
   - upravit `matrix.shard` v `sync.yml` (`[0, 1]` → `[0, 1, 2, 3]` apod.),
   - spustit `python3 src/split_state_by_shard.py N` lokálně a commitnout
     nově vzniklé `uploaded-shard-{0..N-1}.json` (ALE pozor: split čte
     z legacy `uploaded.json`! Pro re-shardování ze stávajících per-shard
     souborů `split_state_by_shard` nestačí, musel by se rozšířit).

5. **CZ proxy** je sdílený resource (chobotnice.aspfree.cz) — pokud začne
   vracet 502 přes tři filmy v řadě, `MAX_CONSECUTIVE_FAILURES = 3` v
   `sync_batch.py` celou dávku bezpečně přeruší.

6. **Velikost backlogu vs. realita**: `pick_next_film.py` může vrátit
   None i když je v backlogu „papírově" víc filmů než nahraných — pokud
   všechny zbývající mají všechny kandidáty permanent-burned. Stav
   k 2026-05-21 to ještě není (462 permanent vs. 7 773 zbývajících
   filmů), ale ke konci Phase 1 to bude zřetelnější.

---

## Commit & push konvence

- Workflow commituje jako `github-actions[bot]`. Lokální commity dělej
  pod svým mailem.
- Commit messages stylu: `chore(shard N/2): +Title (cr_film_id=X)` pro
  state pushe, ostatní `feat:` / `fix:` / `chore:` / `docs:`.
- Sister repo `~/GitHub/Olbrasoft/cr` má tabulku `film_prehrajto_uploads`
  — zdroj backlogu. **Nezapisuj tam.**

---

## Kontakty / referenční repa

- `~/GitHub/Olbrasoft/prehrajto-sync` — sktorrent → prehraj.to,
  zdroj `prehrajto_upload.py` (verbatim copy sem).
- `~/GitHub/Olbrasoft/sledujtetocz-to-prehrajto` — sledujteto.cz →
  prehraj.to, vzor orchestrátoru + workflow yamlu.
- `~/GitHub/Olbrasoft/cr` — CR DB schéma a sync joby pro
  `film_prehrajto_uploads`.

---

## Co dělat jako první po převzetí

1. **Přečti `CLAUDE.md`** v rootu (project conventions) — pár vět navíc
   k tomuto dokumentu (lang_class filtry, naming).
2. **Ověř, že nic neběží**:
   ```bash
   ps -ef | grep sync_loop | grep -v grep        # má být prázdné
   gh run list --workflow=sync.yml --limit=3     # má být všechno completed
   ```
3. **Sync state lokálně**: `git pull --rebase origin main`.
4. **Sanity check counts**:
   ```bash
   python3 -c "import json
   a=json.load(open('state/uploaded-shard-0.json'))
   b=json.load(open('state/uploaded-shard-1.json'))
   print('shard-0:', len(a['uploads']), 'shard-1:', len(b['uploads']),
         'total:', len(a['uploads'])+len(b['uploads']))"
   ```
   Mělo by sedět s předáním (~9 877).
5. **Když chceš pokračovat v nahrávání**: postupuj podle „Jak zase
   rozjet" sekce A nebo B.

Hodně štěstí.
