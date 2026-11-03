#!/usr/bin/env bash
# The back-office release harness runner (docs/release-testing.md).
#
#   scripts/harness/run.sh [--journey R1|all] [--step n] [--from-step n] [--db NAME]
#                          [--synth SOURCE_DB [--provenance-dir DIR]] [--keep-db]
#                          [--retries N] [--port P] [--runs-dir DIR] [--cleanup] [--help]
#
# What it does, in order: builds a Postgres database (fresh: createdb +
# migrate + seed_release_test + demo_roll_dates; synth: pg_dump -Fc |
# pg_restore --no-owner --no-acl of SOURCE_DB + migrate + seed_release_test
# --overlay; --db NAME: reuse as is), starts golden's server on
# config.settings.harness with GOLDEN_HARNESS=1 and a request/exception log,
# waits for /erp/login/, runs the Playwright journeys under
# storefront/scripts/erp-harness/ with the journey/step filters, writes
# .runs/<sha>/report.md, and drops the database unless --keep-db or a step
# failed (the repro command then names it).
#
# Postgres: HARNESS_PG_URL (a server URL without a database, e.g.
# postgres://golden:pw@db.example.internal:5432) from the environment or from
# .runs/harness.env (mode 0600, never committed); or, in CI, DATABASE_URL
# pointing at an existing empty database (used as is, never dropped).
# Flags are the interface; the env aliases JOURNEY, STEP, FROM_STEP, DB,
# KEEP_DB, HARNESS_RETRIES, HARNESS_PORT exist for CI only.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
DOCS="docs/release-testing.md"

usage() {
  cat <<EOF
scripts/harness/run.sh: run the back-office release harness (docs: $DOCS)

  --journey ID      R0, R1, ... or "all" (default all; R0 is the capture self-test)
                    people and setup: R7 import and undo, R8 people data, R10 setup
                    (fresh seed only); R11 returning user (--synth clone only)
  --step n          run only step n of the journey (needs --db from a previous run)
  --from-step n     start at step n (needs --db)
  --db NAME         reuse this database: no create, no seed, no drop
  --synth SOURCE    clone SOURCE (pg_dump -Fc | pg_restore --no-owner --no-acl),
                    seed_release_test --overlay, run, drop
  --provenance-dir  with --synth: directory of the scramble scripts; its SHA256
                    goes in the report (kept in the downstream's private repo)
  --keep-db         keep the database after a passing run
  --retries N       Playwright retries per failed step (default 0; the nightly uses 1)
  --port P          server port (default 8771; 8000/8001 are the dev servers)
  --runs-dir DIR    artefacts (default .runs/<short sha>)
  --cleanup         drop every \${HARNESS_DB_PREFIX}_* database on HARNESS_PG_URL and exit
  --help            this text

Environment: HARNESS_DB_PREFIX (default golden_harness_a) names the databases a run
creates, so concurrent lanes on one Postgres keep apart (golden_harness_j1_...).

The API journeys J1-J4 (scripts/j*_*.py) and the UI journeys they second:
  J1 procure-to-sell   -> R4 (procure to stock) + R1 (quote, order, payment, ship, invoice)
  J2 returns/refund    -> R5 (returns and refunds)
  J3 promo combine     -> R1 (pricing on the order; no journey of its own)
  J4 period close      -> R9 (books)

Repro of a failed step: make harness JOURNEY=R1 STEP=n DB=<name from the report>
EOF
}

JOURNEY=${JOURNEY:-all}
STEP=${STEP:-}
FROM_STEP=${FROM_STEP:-}
DB=${DB:-}
SYNTH=""
PROVENANCE_DIR=""
KEEP_DB=${KEEP_DB:-}
RETRIES=${HARNESS_RETRIES:-0}
PORT=${HARNESS_PORT:-8771}
RUNS_DIR=""
CLEANUP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --journey) JOURNEY=$2; shift 2 ;;
    --step) STEP=$2; shift 2 ;;
    --from-step) FROM_STEP=$2; shift 2 ;;
    --db) DB=$2; shift 2 ;;
    --synth) SYNTH=$2; shift 2 ;;
    --provenance-dir) PROVENANCE_DIR=$2; shift 2 ;;
    --keep-db) KEEP_DB=1; shift ;;
    --retries) RETRIES=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --runs-dir) RUNS_DIR=$2; shift 2 ;;
    --cleanup) CLEANUP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown flag $1 (see --help)"; exit 2 ;;
  esac
done

# ── preflight ────────────────────────────────────────────────────────────────
[ -f config/settings/harness.py ] \
  || { echo "config/settings/harness.py is missing: the harness settings module is not in this checkout. See $DOCS."; exit 2; }
PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi
command -v bun >/dev/null || { echo "bun is not installed (https://bun.sh). See $DOCS."; exit 2; }
for tool in psql createdb dropdb pg_dump pg_restore curl; do
  command -v $tool >/dev/null || { echo "$tool is not installed (postgresql-client, curl). See $DOCS."; exit 2; }
done
[ -d storefront/node_modules/@playwright/test ] \
  || { echo "Playwright is not installed under storefront/: run 'make harness-setup' first. See $DOCS."; exit 2; }

if [ -f .runs/harness.env ]; then
  set -a; . .runs/harness.env; set +a
fi
# After the env file, so HARNESS_DB_PREFIX set there takes effect (it was
# read before the file until 2026-10-17, so the file's value did nothing).
DB_PREFIX=${HARNESS_DB_PREFIX:-golden_harness_a}
PG_URL=${HARNESS_PG_URL:-}
CI_DB_URL=${DATABASE_URL:-}

pg_admin() { psql "$PG_URL/postgres" -v ON_ERROR_STOP=1 -qAt "$@"; }

if [ -n "$CLEANUP" ]; then
  [ -n "$PG_URL" ] || { echo "--cleanup needs HARNESS_PG_URL"; exit 2; }
  for name in $(pg_admin -c "select datname from pg_database where datname like '${DB_PREFIX}_%'"); do
    echo "dropping $name"; dropdb --if-exists "$name" --maintenance-db="$PG_URL/postgres" 2>/dev/null \
      || psql "$PG_URL/postgres" -qAt -c "drop database if exists \"$name\" with (force)"
  done
  exit 0
fi

SHA=$(git rev-parse --short HEAD 2>/dev/null || echo nosha)
RUN=${RUNS_DIR:-.runs/$SHA}
mkdir -p "$RUN" && chmod 700 "$RUN"
RUN=$(cd "$RUN" && pwd)
# A fresh run starts clean; a --db repro keeps the previous state files.
if [ -z "$DB" ]; then
  rm -rf "$RUN/steps" "$RUN/video" "$RUN/test-results" "$RUN/playwright-report" \
         "$RUN"/steps.jsonl "$RUN"/state-*.json "$RUN"/server.log "$RUN"/report.md "$RUN"/webhooks.jsonl
fi
START_TS=$(date +%s)

# ── database ─────────────────────────────────────────────────────────────────
MODE="fresh"
CREATED_DB=""
if [ -n "$DB" ]; then
  MODE="reuse"
  [ -n "$PG_URL" ] || { echo "--db needs HARNESS_PG_URL (see $DOCS)"; exit 2; }
  DB_URL="$PG_URL/$DB"
elif [ -n "$SYNTH" ]; then
  MODE="synth"
  [ -n "$PG_URL" ] || { echo "--synth needs HARNESS_PG_URL (see $DOCS)"; exit 2; }
  DB="${DB_PREFIX}_synth_$(date +%y%m%d%H%M%S)"
  DB_URL="$PG_URL/$DB"
  echo "== clone $SYNTH -> $DB"
  pg_admin -c "create database \"$DB\""
  CREATED_DB=$DB
  pg_dump -Fc "$PG_URL/$SYNTH" | pg_restore --no-owner --no-acl -d "$DB_URL" || true
elif [ -n "$CI_DB_URL" ] && [ -z "$PG_URL" ]; then
  MODE="ci"
  DB_URL=$CI_DB_URL
  DB=${DB_URL##*/}
else
  [ -n "$PG_URL" ] || { echo "no Postgres: set HARNESS_PG_URL (or put it in .runs/harness.env), or DATABASE_URL in CI. See $DOCS."; exit 2; }
  DB="${DB_PREFIX}_$(date +%y%m%d%H%M%S)"
  DB_URL="$PG_URL/$DB"
  echo "== create $DB"
  pg_admin -c "create database \"$DB\""
  CREATED_DB=$DB
fi

export GOLDEN_HARNESS=1 DATABASE_URL="$DB_URL" ALLOWED_HOSTS="127.0.0.1,localhost"
export GOLDEN_HARNESS_PORT=$PORT GOLDEN_HARNESS_LOG="$RUN/server.log" GOLDEN_HARNESS_WEBHOOK_LOG="$RUN/webhooks.jsonl"
export HARNESS_RUN_DIR="$RUN" DJANGO_SETTINGS_MODULE=config.settings.harness
# Dedicated keys so the server does not warn about deriving them from SECRET_KEY.
export API_JWT_SIGNING_KEY=${API_JWT_SIGNING_KEY:-harness-jwt-$SHA} LLMHUB_FERNET_KEY=${LLMHUB_FERNET_KEY:-$($PYTHON -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')}
GOLDEN_PORTAL_COMPANY=${GOLDEN_PORTAL_COMPANY:-$($PYTHON -c 'from core.release_test import HARNESS_COMPANY_ID;print(HARNESS_COMPANY_ID)')}
export GOLDEN_PORTAL_COMPANY

PROVENANCE="{}"
case "$MODE" in
  fresh|ci)
    echo "== migrate + seed_release_test ($MODE)"
    "$PYTHON" manage.py migrate --noinput | tail -1
    "$PYTHON" manage.py seed_release_test --runs-dir "$RUN" | tail -4
    "$PYTHON" manage.py demo_roll_dates --force | tail -1
    PROVENANCE="{\"seed\": \"seed_release_test\", \"mode\": \"$MODE\"}"
    ;;
  synth)
    echo "== migrate + reconcile_engagements --fix + seed_release_test --overlay (synth clone)"
    "$PYTHON" manage.py migrate --noinput | tail -1
    # The cutover's sequence after migrate (docs/pin/cutover-runbook.md):
    # a clone must carry the same repairs a real upgrade gets, e.g. the
    # history rows a lazy ensure once wrote as observed (R11).
    "$PYTHON" manage.py reconcile_engagements --fix | tail -1
    SEED_OUT=$("$PYTHON" manage.py seed_release_test --overlay --runs-dir "$RUN" ${SYNTH_COMPANY:+--company "$SYNTH_COMPANY"})
    echo "$SEED_OUT" | tail -4
    # The overlay's tenant is the clone's own company, not the fresh seed's
    # fixed id: the API key and the portal setting must name it.
    SYNTH_PORTAL=$(echo "$SEED_OUT" | sed -n 's/^GOLDEN_PORTAL_COMPANY=//p' | tail -1)
    if [ -n "$SYNTH_PORTAL" ]; then GOLDEN_PORTAL_COMPANY=$SYNTH_PORTAL; export GOLDEN_PORTAL_COMPANY; fi
    SCRAMBLE_SHA=""
    if [ -n "$PROVENANCE_DIR" ]; then
      SCRAMBLE_SHA=$(cd "$PROVENANCE_DIR" && find . -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1)
    fi
    LEDGER=$("$PYTHON" manage.py showmigrations --plan 2>/dev/null | grep -c '\[X\]' || echo 0)
    LAST_CORE=$("$PYTHON" manage.py showmigrations core --plan 2>/dev/null | grep '\[X\]' | tail -1 | sed 's/.*core\.//' || true)
    PROVENANCE="{\"mode\": \"synth\", \"source\": \"$SYNTH\", \"scramble_scripts_sha256\": \"$SCRAMBLE_SHA\", \"applied_migrations\": $LEDGER, \"last_core_migration\": \"$LAST_CORE\", \"golden_version\": \"$(git describe --tags --always 2>/dev/null)\"}"
    ;;
  reuse)
    echo "== reuse $DB (no seed)"
    PROVENANCE="{\"mode\": \"reuse\"}"
    ;;
esac
# A service-account key for the API steps (R1 posts a lead through the API
# as an integration would: role lead-intake, contract crm.add_lead). Issued once per database; 0600, never printed.
if [ "$MODE" != "reuse" ] || [ ! -s "$RUN/api-key.txt" ]; then
  "$PYTHON" manage.py create_api_key --name lead-intake --role lead-intake --company "$GOLDEN_PORTAL_COMPANY" \
    --key-name "harness-$(date +%s)" 2>/dev/null | tail -1 > "$RUN/api-key.txt"
  chmod 600 "$RUN/api-key.txt"
fi
SEED_TS=$(date +%s)

# ── server ───────────────────────────────────────────────────────────────────
BASE="http://127.0.0.1:$PORT"
if curl -fsS -o /dev/null "$BASE/erp/login/" 2>/dev/null; then
  echo "something already answers on $BASE; pick another --port"; exit 2
fi
echo "== server on $BASE (log: $RUN/server.log)"
"$PYTHON" manage.py runserver "127.0.0.1:$PORT" --noreload > "$RUN/server.stdout" 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT
for _ in $(seq 1 90); do
  if curl -fsS -o /dev/null "$BASE/erp/login/" 2>/dev/null; then break; fi
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "server exited; see $RUN/server.stdout"; tail -20 "$RUN/server.stdout"; exit 1; }
  sleep 1
done
curl -fsS -o /dev/null "$BASE/erp/login/" || { echo "server did not answer on $BASE in 90 s; see $RUN/server.stdout"; exit 1; }

# ── journeys ─────────────────────────────────────────────────────────────────
# Playwright file filters: one journey = one spec file, journeys/<id>.spec.ts.
GREP=()
if [ "$JOURNEY" != "all" ]; then
  for j in ${JOURNEY//,/ }; do
    [ -f "storefront/scripts/erp-harness/journeys/$j.spec.ts" ] \
      || { echo "no journey $j (storefront/scripts/erp-harness/journeys/$j.spec.ts); see --help"; exit 2; }
    GREP+=("$j.spec.ts")
  done
fi
echo "== journeys: $JOURNEY (retries $RETRIES)${STEP:+ step $STEP}${FROM_STEP:+ from step $FROM_STEP}"
set +e
(
  cd storefront
  HARNESS_BASE_URL="$BASE" HARNESS_RUN_DIR="$RUN" HARNESS_LOGINS="$RUN/logins.txt" \
  HARNESS_SERVER_LOG="$RUN/server.log" HARNESS_RETRIES="$RETRIES" \
  HARNESS_STEP="$STEP" HARNESS_FROM_STEP="$FROM_STEP" HARNESS_DB="$DB" \
  HARNESS_API_KEY="$(cat "$RUN/api-key.txt" 2>/dev/null)" HARNESS_COMPANY="$GOLDEN_PORTAL_COMPANY" \
    bunx playwright test --config scripts/erp-harness/playwright.config.ts "${GREP[@]}"
)
RC=$?
set -e
END_TS=$(date +%s)

# ── report ───────────────────────────────────────────────────────────────────
cat > "$RUN/meta.json" <<EOF
{"sha": "$(git rev-parse HEAD 2>/dev/null || echo nosha)", "short_sha": "$SHA", "branch": "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)",
 "database": "$DB", "mode": "$MODE", "provenance": $PROVENANCE, "journey": "$JOURNEY", "step": "$STEP",
 "from_step": "$FROM_STEP", "retries": $RETRIES, "base": "$BASE", "host": "$(hostname)",
 "seed_seconds": $((SEED_TS - START_TS)), "journey_seconds": $((END_TS - SEED_TS)), "total_seconds": $((END_TS - START_TS)),
 "playwright_exit": $RC, "started": "$(date -u -d @$START_TS +%FT%TZ)", "ended": "$(date -u -d @$END_TS +%FT%TZ)"}
EOF
"$PYTHON" scripts/harness/report.py --run-dir "$RUN"
echo "report: $RUN/report.md (total $((END_TS - START_TS)) s: seed $((SEED_TS - START_TS)) s, journeys $((END_TS - SEED_TS)) s)"

# ── database teardown ────────────────────────────────────────────────────────
cleanup
trap - EXIT
if [ -n "$CREATED_DB" ]; then
  if [ "$RC" -eq 0 ] && [ -z "$KEEP_DB" ]; then
    echo "== drop $CREATED_DB"
    psql "$PG_URL/postgres" -qAt -c "drop database if exists \"$CREATED_DB\" with (force)"
  else
    echo "== kept $CREATED_DB (repro: make harness JOURNEY=<id> STEP=<n> DB=$CREATED_DB; drop all with: make harness-clean)"
  fi
fi
exit "$RC"
