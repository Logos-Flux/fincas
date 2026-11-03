#!/usr/bin/env bash
# Persona run against a kept harness database (docs/release-testing.md,
# docs/release-hardening-plan.md section 5).
#
#   scripts/personas/run.sh <kept-db-name> [persona|all] [max-steps]
#
# Starts golden's server on config.settings.harness for that database (port
# PERSONA_PORT, default 8799), takes the LLM gateway from GATEWAY_URL and
# GATEWAY_KEY in the environment (export them from your secret store: never on
# the command line, never in the repo), runs the personas one at a time,
# stops the server. Output under .runs-persona/personas/<persona>/
# (findings.md, steps.jsonl, steps/NN.png).
set -euo pipefail
DB="${1:?kept harness database, e.g. golden_harness_pe_261018031500}"
PERSONA="${2:-all}"
MAX_STEPS="${3:-40}"
PORT="${PERSONA_PORT:-8799}"
: "${GATEWAY_URL:?set GATEWAY_URL (the LLM gateway base URL) in the environment}"
: "${GATEWAY_KEY:?set GATEWAY_KEY (the gateway key) in the environment}"
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY="${PYTHON:-$ROOT/.venv/bin/python}"
RUN="$ROOT/.runs-persona"
cd "$ROOT"
PG=$(grep -oE "postgres://[^ ]+" .runs/harness.env | head -1)
export GOLDEN_HARNESS=1 DATABASE_URL="${PG%/}/$DB" ALLOWED_HOSTS="127.0.0.1,localhost"
export GOLDEN_HARNESS_PORT=$PORT GOLDEN_HARNESS_LOG="$RUN/server.log" GOLDEN_HARNESS_WEBHOOK_LOG="$RUN/webhooks.jsonl"
export HARNESS_RUN_DIR="$RUN" DJANGO_SETTINGS_MODULE=config.settings.harness
"$PY" manage.py runserver "127.0.0.1:$PORT" --noreload > "$RUN/server.stdout" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do curl -s -o /dev/null "http://127.0.0.1:$PORT/erp/login/" && break; sleep 1; done
export GATEWAY_URL GATEWAY_KEY
cd storefront
ERP_URL="http://127.0.0.1:$PORT" HARNESS_LOGINS="$RUN/logins.txt" HARNESS_SERVER_LOG="$RUN/server.log" \
  PERSONA="$PERSONA" MAX_STEPS="$MAX_STEPS" OUT="$RUN/personas" node scripts/personas/run.mjs
