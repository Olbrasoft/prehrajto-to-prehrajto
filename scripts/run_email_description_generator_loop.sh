#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

BATCH_SIZE="${BATCH_SIZE:-120}"
SLEEP_AFTER_BATCH_SECONDS="${SLEEP_AFTER_BATCH_SECONDS:-30}"

commit_and_push_descriptions() {
  python3 scripts/validate_email_descriptions.py

  git add state/email-generated-descriptions-v3.jsonl
  if git diff --cached --quiet; then
    echo "no new generated descriptions to commit"
    return 0
  fi

  ok_count="$(
    python3 - <<'PY'
import json
from pathlib import Path
rows = [
    json.loads(line)
    for line in Path("state/email-generated-descriptions-v3.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
print(sum(1 for row in rows if row.get("status") == "ok" and row.get("new_description")))
PY
  )"
  git commit -m "chore: generate more email descriptions (${ok_count} ready)"
  git pull --rebase origin main
  git push origin HEAD:main
}

while true; do
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "===== description generator loop ${ts} batch_size=${BATCH_SIZE} ====="

  commit_and_push_descriptions

  git fetch origin main
  git pull --rebase origin main

  python3 scripts/generate_email_descriptions.py --count "${BATCH_SIZE}"
  commit_and_push_descriptions

  echo "sleeping ${SLEEP_AFTER_BATCH_SECONDS}s before next description batch"
  sleep "${SLEEP_AFTER_BATCH_SECONDS}"
done
