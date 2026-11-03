#!/usr/bin/env bash
# Release journey R12, the adopter path: docs/getting-started.md followed
# literally on a clean directory, timed, ending in a first invoice raised
# through the back office (docs/release-hardening-plan.md §1, item 7;
# docs/release-process.md §3.2).
#
#   scripts/release/r12_fresh_install.sh                       # wheel built from this tree
#   scripts/release/r12_fresh_install.sh --pip-spec fincas==0.4.0
#   scripts/release/r12_fresh_install.sh --pip-spec fincas==0.4.0 \
#       --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/
#
#   --pip-spec SPEC         install SPEC instead of a wheel built from the tree
#   --index-url URL         pip --index-url for SPEC (TestPyPI)
#   --extra-index-url URL   pip --extra-index-url (TestPyPI has no dependencies)
#   --db postgres|sqlite    postgres (default) follows the page's docker run on
#                           a random loopback port; sqlite takes the page's
#                           SQLite note instead (no Docker)
#   --budget SECONDS        fail when the page plus the first invoice takes
#                           longer (default 300; 0 = report only)
#   --python PY             interpreter for the venv (default python3.12, else python3)
#   --no-ui                 stop after the server answers (no Playwright)
#   --cold                  no pip cache and a fresh `docker pull postgres:17`
#                           (a first-time adopter's download time)
#   --keep                  keep the work directory and the database container
#   --help
#
# The page is the single source: scripts/release/r12_extract.py copies its
# ```bash blocks into one script, with these literal substitutions (each must
# still occur on the page, so the two cannot drift apart silently):
#   python3 -m venv         -> the chosen interpreter
#   pip install fincas      -> the wheel or SPEC (plus the index flags)
#   fincas-postgres         -> a per-run container name
#   127.0.0.1:5432          -> a free loopback port
#   runserver 8000          -> runserver on a free loopback port
# The generated script runs under `env -i` (HOME, PATH, LANG, TMPDIR, plus
# DOCKER_HOST and LD_LIBRARY_PATH when set), so
# nothing from the caller's environment (VIRTUAL_ENV, DJANGO_SETTINGS_MODULE,
# DATABASE_URL, ...) can make it pass.
#
# Then the first invoice, as page step 7 describes it, through Chromium
# (storefront/scripts/r12/first-invoice.spec.ts), and an independent check in
# the project's own shell that the invoice exists and matches the order.
#
# Needs: Python >= 3.12, git, curl, Docker (postgres mode), bun and
# storefront/node_modules with Playwright's Chromium (make harness-setup), and
# WeasyPrint's system libraries. Network: pip and the postgres:17 image.
#
# Exit codes: 0 pass inside the budget · 1 a step failed or the budget was
# exceeded · 2 could not start (a missing tool, bad flags).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DOC="$ROOT/docs/getting-started.md"

usage() { sed -n '2,48p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

PIP_SPEC=""
INDEX_URL=""
EXTRA_INDEX_URL=""
DB_MODE=postgres
BUDGET=300
PY=""
UI=1
KEEP=0
COLD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pip-spec) PIP_SPEC=$2; shift 2 ;;
    --index-url) INDEX_URL=$2; shift 2 ;;
    --extra-index-url) EXTRA_INDEX_URL=$2; shift 2 ;;
    --db) DB_MODE=$2; shift 2 ;;
    --budget) BUDGET=$2; shift 2 ;;
    --python) PY=$2; shift 2 ;;
    --no-ui) UI=0; shift ;;
    --cold) COLD=1; shift ;;
    --keep) KEEP=1; shift ;;
    --help|-h) usage 0 ;;
    *) echo "unknown flag $1 (see --help)"; exit 2 ;;
  esac
done

# ── preflight ────────────────────────────────────────────────────────────────
fail_env() { echo "R12 cannot start: $*"; exit 2; }
case "$DB_MODE" in postgres|sqlite) ;; *) fail_env "--db must be postgres or sqlite" ;; esac
[[ "$BUDGET" =~ ^[0-9]+$ ]] || fail_env "--budget must be a number of seconds"
[ -f "$DOC" ] || fail_env "$DOC is missing"
if [ -z "$PY" ]; then PY=$(command -v python3.12 || command -v python3 || true); fi
[ -n "$PY" ] && command -v "$PY" >/dev/null || fail_env "no Python interpreter (--python)"
PY=$(command -v "$PY")
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || fail_env "$PY is older than 3.12"
for tool in git curl; do command -v $tool >/dev/null || fail_env "$tool is not installed"; done
if [ "$DB_MODE" = postgres ]; then
  command -v docker >/dev/null || fail_env "docker is not installed (or use --db sqlite)"
  docker info >/dev/null 2>&1 || fail_env "the Docker daemon is not reachable by $(id -un)"
fi
if [ "$UI" = 1 ]; then
  command -v bun >/dev/null || fail_env "bun is not installed (https://bun.sh), or pass --no-ui"
  [ -d "$ROOT/storefront/node_modules/@playwright/test" ] \
    || fail_env "Playwright is not installed under storefront/: run 'make harness-setup', or pass --no-ui"
fi

free_port() { "$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/fincas-r12.XXXXXX")
CONTAINER="fincas-r12-$(basename "$WORK" | tr -dc 'a-zA-Z0-9' | tr 'A-Z' 'a-z')"
PGPORT=$(free_port)
WEBPORT=$(free_port)
PROJ="$WORK/fincas-trial"
LOG="$WORK/run.log"

# --keep keeps everything; a failure keeps the work directory (logs, traces)
# but never leaves the database container running.
KEEP_DIR=0
cleanup() {
  local rc=$?
  if [ -f "$PROJ/server.pid" ]; then kill "$(cat "$PROJ/server.pid")" 2>/dev/null || true; fi
  if [ "$KEEP" = 1 ]; then
    echo "kept: $WORK (container $CONTAINER, if it was started)"
    exit $rc
  fi
  [ "$DB_MODE" = postgres ] && { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
  if [ "$KEEP_DIR" = 1 ]; then echo "kept for inspection: $WORK"; else rm -rf "$WORK"; fi
  exit $rc
}
trap cleanup EXIT

echo "R12 adopter path: $DOC"
echo "work dir $WORK · db $DB_MODE · python $("$PY" -V 2>&1) · budget ${BUDGET}s"

# ── what to install ──────────────────────────────────────────────────────────
if [ -z "$PIP_SPEC" ]; then
  echo "== building the wheel from this tree (not timed: release preparation)"
  mkdir -p "$WORK/src"
  (cd "$ROOT" && git ls-files -z -co --exclude-standard | while IFS= read -r -d '' f; do
    [ -e "$f" ] && printf '%s\0' "$f"
  done | tar --null -T - -cf -) | tar -xf - -C "$WORK/src"
  "$PY" -m pip wheel "$WORK/src" --no-deps --quiet -w "$WORK/dist" >/dev/null
  rm -rf "$WORK/src"
  PIP_SPEC=$(ls "$WORK"/dist/fincas-*.whl)
  echo "   $PIP_SPEC"
fi
PIP_ARGS=$(printf '%q' "$PIP_SPEC")
[ -n "$INDEX_URL" ] && PIP_ARGS="--index-url $(printf '%q' "$INDEX_URL") $PIP_ARGS"
[ -n "$EXTRA_INDEX_URL" ] && PIP_ARGS="--extra-index-url $(printf '%q' "$EXTRA_INDEX_URL") $PIP_ARGS"

# ── the page, as a script ────────────────────────────────────────────────────
SUBS=(
  --sub "python3 -m venv=$PY -m venv"
  --sub "pip install fincas=pip install $PIP_ARGS"
  --sub "runserver 8000=runserver 127.0.0.1:$WEBPORT"
)
if [ "$DB_MODE" = postgres ]; then
  SUBS+=(--sub "fincas-postgres=$CONTAINER" --sub "127.0.0.1:5432=127.0.0.1:$PGPORT")
else
  # The page's SQLite note: no docker, DATABASE_URL=sqlite:///db.sqlite3.
  SUBS+=(--drop-match "docker " --sub "postgres://fincas:fincas@127.0.0.1:5432/fincas=sqlite:///db.sqlite3")
fi
"$PY" -I "$ROOT/scripts/release/r12_extract.py" "$DOC" "${SUBS[@]}" > "$WORK/getting-started.sh" \
  || { echo "R12 FAIL: could not turn $DOC into a script (see above)"; exit 1; }

COLD_ENV=""
if [ "$COLD" = 1 ]; then
  COLD_ENV="PIP_NO_CACHE_DIR=1"
  # Untagged locally means the page's docker run pulls it, inside the clock.
  [ "$DB_MODE" = postgres ] && docker image rm postgres:17 >/dev/null 2>&1 || true
fi
echo "== running docs/getting-started.md ($(grep -c '^echo "\$(date' "$WORK/getting-started.sh") blocks); log: $LOG"
t0=$(date +%s.%N)
set +e
CLEAN_PATH="/usr/local/bin:/usr/bin:/bin"
command -v docker >/dev/null && CLEAN_PATH="$CLEAN_PATH:$(dirname "$(command -v docker)")"
cd "$WORK" && env -i HOME="$HOME" PATH="$CLEAN_PATH" LANG="${LANG:-C.UTF-8}" TMPDIR="${TMPDIR:-/tmp}" \
  ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} ${LD_LIBRARY_PATH:+LD_LIBRARY_PATH="$LD_LIBRARY_PATH"} ${COLD_ENV:-} \
  bash "$WORK/getting-started.sh" > "$LOG" 2>&1 < /dev/null
rc=$?
set -e
if [ $rc -ne 0 ]; then
  echo "R12 FAIL: the page's commands failed (exit $rc). Last lines of $LOG:"
  tail -25 "$LOG" | sed 's/^/   /'
  [ -f "$PROJ/server.log" ] && { echo "   server.log:"; tail -10 "$PROJ/server.log" | sed 's/^/   /'; }
  KEEP_DIR=1; exit 1
fi

# The server block returns at once: wait for the login page, as a person would.
BASE="http://127.0.0.1:$WEBPORT"
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "$BASE/erp/login/" 2>/dev/null && break
  kill -0 "$(cat "$PROJ/server.pid")" 2>/dev/null || break
  sleep 0.5
done
if ! curl -fsS -o /dev/null "$BASE/erp/login/"; then
  echo "R12 FAIL: the server never answered $BASE/erp/login/. server.log:"
  tail -25 "$PROJ/server.log" | sed 's/^/   /'
  KEEP_DIR=1; exit 1
fi
t_server=$(date +%s.%N)

# Per-block wall time from the markers the extractor put in.
echo "   per block:"
"$PY" -I - "$LOG" "$t_server" <<'EOF'
import re, sys
marks = [(float(m.group(1)), m.group(2)) for m in
         (re.match(r"^(\d+\.\d+) (== block .*)$", line) for line in open(sys.argv[1], encoding="utf-8", errors="replace"))
         if m]
end = float(sys.argv[2])
for (t, label), nxt in zip(marks, [m[0] for m in marks[1:]] + [end]):
    print(f"     {nxt - t:6.1f}s  {label}")
EOF

# The superuser's credentials, from seed_demo_data's own output (page step 5).
creds=$(grep -E 'Admin credentials: ' "$LOG" | tail -1 | sed -E 's/.*Admin credentials: //')
R12_USER=${creds%% / *}
R12_PASS=${creds#* / }
[ -n "$R12_USER" ] && [ -n "$R12_PASS" ] && [ "$R12_PASS" != "(as supplied)" ] \
  || { echo "R12 FAIL: seed_demo_data printed no credentials line"; KEEP_DIR=1; exit 1; }

# ── the first invoice (page step 7) ──────────────────────────────────────────
INVOICE_OUT="$WORK/first-invoice.json"
if [ "$UI" = 1 ]; then
  echo "== first invoice through the back office (Chromium)"
  set +e
  (cd "$ROOT/storefront" && R12_BASE_URL="$BASE" R12_USER="$R12_USER" R12_PASS="$R12_PASS" \
    R12_OUT="$INVOICE_OUT" R12_ARTIFACTS="$WORK/playwright" \
    bunx playwright test -c scripts/r12/playwright.config.ts) > "$WORK/playwright.log" 2>&1
  prc=$?
  set -e
  if [ $prc -ne 0 ] || [ ! -s "$INVOICE_OUT" ]; then
    echo "R12 FAIL: the first-invoice journey failed (exit $prc). $WORK/playwright.log:"
    tail -40 "$WORK/playwright.log" | sed 's/^/   /'
    echo "   server.log:"; grep -vE '^\s*$' "$PROJ/server.log" | tail -15 | sed 's/^/   /'
    KEEP_DIR=1; exit 1
  fi
  t_invoice=$(date +%s.%N)

  # Independent of the browser: the project's own shell must see the invoice.
  echo "== the invoice in the database (the project's own manage.py)"
  cat > "$WORK/verify.py" <<'EOF'
import json
import os
from decimal import Decimal

from orders.models import SalesInvoice

seen = json.load(open(os.environ["R12_OUT"]))
inv = SalesInvoice.objects.select_related("order").get(pk=seen["invoice_id"])
problems = []
if inv.invoice_number != seen["invoice_number"]:
    problems.append(f"number {inv.invoice_number} != page {seen['invoice_number']}")
if str(inv.order_id) != seen["order_id"]:
    problems.append(f"order {inv.order_id} != page {seen['order_id']}")
if inv.grand_total <= Decimal("0") or inv.grand_total != inv.order.grand_total:
    problems.append(f"total {inv.grand_total} vs order {inv.order.grand_total}")
if SalesInvoice.objects.filter(order_id=inv.order_id).count() != 1:
    problems.append("not exactly one invoice on the order")
print(("FAIL " + "; ".join(problems)) if problems else
      f"OK {inv.invoice_number} for {inv.order.order_number}: {inv.grand_total} ({inv.status})")
EOF
  verdict=$(cd "$PROJ" && env -i HOME="$HOME" PATH="/usr/bin:/bin" LANG="${LANG:-C.UTF-8}" R12_OUT="$INVOICE_OUT" \
    ${LD_LIBRARY_PATH:+LD_LIBRARY_PATH="$LD_LIBRARY_PATH"} \
    .venv/bin/python manage.py shell < "$WORK/verify.py" 2>&1 | tail -1)
  echo "   $verdict"
  [[ "$verdict" == OK* ]] || { KEEP_DIR=1; echo "R12 FAIL: the invoice the page showed is not what the database holds"; exit 1; }
else
  t_invoice=$t_server
fi

# ── timing ───────────────────────────────────────────────────────────────────
read -r page_s invoice_s total_s < <("$PY" -c "a,b,c=$t0,$t_server,$t_invoice; print(f'{b-a:.1f} {c-b:.1f} {c-a:.1f}')")
echo
echo "R12 timing: page ${page_s}s (install to server up) + first invoice ${invoice_s}s = ${total_s}s (budget ${BUDGET}s)"
if [ "$BUDGET" -gt 0 ] && "$PY" -c "import sys; sys.exit(not ($total_s > $BUDGET))"; then
  echo "R12 FAIL: over the ${BUDGET}s budget"
  exit 1
fi
echo "R12 PASS"
