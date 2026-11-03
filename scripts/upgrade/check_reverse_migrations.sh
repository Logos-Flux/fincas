#!/usr/bin/env bash
# Reverse-migration check (docs/release-hardening-plan.md 7.5): every migration
# added between two refs reverses cleanly on a seeded database and applies
# again, or is one-way and listed in the release notes.
#
#   DATABASE_URL=postgres://... scripts/upgrade/check_reverse_migrations.sh v0.4.0-rc.4 v0.4.0-rc.5 \
#       [--expect-one-way-file docs/releases/v0.4.0.md]
#
# DATABASE_URL must name an EMPTY database (the script never creates or drops
# one; make a scratch database and drop it afterwards). PYTHON defaults to
# `python`. The <to-ref> tree is checked out in a temporary git worktree and
# runs on PYTHON's environment.
#
# Steps: list the migrations added in <from-ref>..<to-ref> (the same
# `git diff --name-status -- */migrations/0*.py` listing `manage.py
# release_notes` uses); in the <to-ref> worktree, migrate the empty database
# and seed it (seed_demo_data); then for each added migration, `migrate <app>
# <previous>` (timed), then a full `migrate` back to <to-ref> (timed; a plain
# `migrate <app>` would leave a dependent app's migrations unapplied when
# the reverse cascaded into it). A migration whose RunPython/RunSQL has no
# reverse or a noop reverse is one-way (same classification as
# release_notes); so is one whose reverse Django refuses at run time. The
# one-way list is printed, and with --expect-one-way-file it must appear in
# that file (each `app.name` named somewhere in it).
#
# Exit: 0 every migration reverses and re-applies and the one-way list is
# documented; 2 usage (refs, database, arguments); 10 a one-way migration is
# missing from --expect-one-way-file (or the file lists one that is not);
# 20 a migration failed to reverse or to apply again. The table is the shared
# release-command table (core/management/release_exit_codes.py).
set -uo pipefail

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

FROM_REF=${1:-}
TO_REF=${2:-}
EXPECT_FILE=""
shift 2 2>/dev/null || { usage; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-one-way-file) EXPECT_FILE=${2:-}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[ -n "$FROM_REF" ] && [ -n "$TO_REF" ] || { usage; exit 2; }
: "${DATABASE_URL:?set DATABASE_URL to an empty database}" || exit 2
PYTHON=${PYTHON:-python}
SETTINGS=--settings=config.settings.dev
HEAD_DIR=$(git rev-parse --show-toplevel) || exit 2
cd "$HEAD_DIR" || exit 2

for ref in "$FROM_REF" "$TO_REF"; do
  git rev-parse -q --verify "$ref^{commit}" >/dev/null 2>&1 \
    || { echo "unknown ref $ref (run git fetch --tags)" >&2; exit 2; }
done
if [ -n "$EXPECT_FILE" ] && [ ! -f "$EXPECT_FILE" ]; then
  echo "--expect-one-way-file $EXPECT_FILE does not exist" >&2; exit 2
fi

TMP_DIR=$(mktemp -d)
WORK_DIR=$TMP_DIR/to-ref
cleanup() { git -C "$HEAD_DIR" worktree remove --force "$WORK_DIR" 2>/dev/null || true; rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# --- the migrations added between the refs ---------------------------------------
ADDED=()  # "app name path"
while IFS=$'\t' read -r status path; do
  [ "$status" = "A" ] || continue
  app=$(basename "$(dirname "$(dirname "$path")")")
  name=$(basename "$path" .py)
  ADDED+=("$app $name $path")
done < <(git diff --name-status "$FROM_REF..$TO_REF" -- '*/migrations/0*.py')

if [ ${#ADDED[@]} -eq 0 ]; then
  echo "no migrations added in $FROM_REF..$TO_REF"
  echo "reverse migrations $FROM_REF..$TO_REF: OK (nothing to check)"
  exit 0
fi
echo "== $FROM_REF..$TO_REF: ${#ADDED[@]} migration(s) added"

# Static one-way classification: a RunPython without reverse_code (one
# positional argument) or with RunPython.noop; a RunSQL without reverse_sql or
# with RunSQL.noop. Mirrors core.release_notes.classify_migration.
classify() {
  git show "$TO_REF:$1" > "$TMP_DIR/migration.py" || { echo "cannot read $1 at $TO_REF" >&2; return 1; }
  "$PYTHON" - "$TMP_DIR/migration.py" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
def name(node):
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
def noop(node):
    return isinstance(node, ast.Attribute) and node.attr == "noop"
reasons = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    kind = name(node)
    if kind not in ("RunPython", "RunSQL"):
        continue
    kw = "reverse_code" if kind == "RunPython" else "reverse_sql"
    reverse = node.args[1] if len(node.args) > 1 else None
    for k in node.keywords:
        if k.arg == kw:
            reverse = k.value
    if reverse is None:
        reasons.append(f"{kind} without a reverse")
    elif noop(reverse):
        reasons.append(f"{kind} reverse is noop")
print("; ".join(reasons))
PY
}

# --- the <to-ref> tree, migrated and seeded -----------------------------------------
git -C "$HEAD_DIR" worktree add --quiet --detach "$WORK_DIR" "$TO_REF" || exit 2
cd "$WORK_DIR" || exit 2

"$PYTHON" manage.py shell $SETTINGS -c '
from django.db import connection
if connection.introspection.table_names():
    raise SystemExit("refusing: DATABASE_URL is not an empty database")
' || exit 2

echo "== $TO_REF: migrate an empty database and seed it"
"$PYTHON" manage.py migrate --noinput $SETTINGS | tail -1 || exit 2
"$PYTHON" manage.py seed_demo_data --admin-password "$(openssl rand -hex 16)" $SETTINGS >/dev/null || exit 2

previous_of() {  # app name -> the migration applied before it in that app, or zero
  "$PYTHON" manage.py showmigrations "$1" $SETTINGS 2>/dev/null \
    | sed -n 's/^ \[[X ]\] \([^ ]*\).*/\1/p' \
    | awk -v target="$2" 'BEGIN{prev="zero"} $0==target{print prev; exit} {prev=$0}'
}

now_ms() { date +%s%3N; }

ROWS=()
ONE_WAY=()
STATUS=0   # worst exit so far
worse() { [ "$1" -gt "$STATUS" ] && STATUS=$1; }

for entry in "${ADDED[@]}"; do
  read -r app name path <<<"$entry"
  label="$app.$name"
  reasons=$(classify "$path")
  previous=$(previous_of "$app" "$name")
  if [ -z "$previous" ]; then
    ROWS+=("$label|skipped|skipped|-|-|not in showmigrations at $TO_REF")
    worse 20; continue
  fi
  t0=$(now_ms)
  if "$PYTHON" manage.py migrate --noinput "$app" "$previous" $SETTINGS >"$TMP_DIR/rev.log" 2>&1; then
    rev="pass"
  else
    rev="FAIL"
    if grep -q "IrreversibleError\|not reversible" "$TMP_DIR/rev.log"; then
      rev="refused"; reasons="${reasons:+$reasons; }reverse refused at run time"
    fi
  fi
  t1=$(now_ms)
  if "$PYTHON" manage.py migrate --noinput $SETTINGS >"$TMP_DIR/fwd.log" 2>&1; then
    fwd="pass"
  else
    fwd="FAIL"
  fi
  t2=$(now_ms)
  if [ "$rev" = "FAIL" ] || [ "$fwd" = "FAIL" ]; then
    worse 20
    echo "--- $label: reverse=$rev forward=$fwd"; tail -5 "$TMP_DIR/rev.log" "$TMP_DIR/fwd.log"
  fi
  if [ -n "$reasons" ]; then
    ONE_WAY+=("$label")
  fi
  ROWS+=("$label|$rev|$fwd|$((t1 - t0)) ms|$((t2 - t1)) ms|${reasons:--}")
done

cd "$HEAD_DIR" || exit 2

echo
printf '%-50s %-8s %-8s %-10s %-10s %s\n' migration reverse forward reverse_t forward_t one-way
for row in "${ROWS[@]}"; do
  IFS='|' read -r m r f tr tf reason <<<"$row"
  printf '%-50s %-8s %-8s %-10s %-10s %s\n' "$m" "$r" "$f" "$tr" "$tf" "$reason"
done

echo
if [ ${#ONE_WAY[@]} -eq 0 ]; then
  echo "one-way migrations: none"
else
  echo "one-way migrations (must be in the release notes' one-way list):"
  for m in "${ONE_WAY[@]}"; do echo "  - $m"; done
fi

if [ -n "$EXPECT_FILE" ]; then
  echo
  for m in "${ONE_WAY[@]}"; do
    if ! grep -qF "$m" "$EXPECT_FILE"; then
      echo "MISSING from $EXPECT_FILE: $m"; worse 10
    fi
  done
  for entry in "${ADDED[@]}"; do
    read -r app name _ <<<"$entry"
    label="$app.$name"
    listed=0
    for m in "${ONE_WAY[@]}"; do [ "$m" = "$label" ] && listed=1; done
    if [ "$listed" = 0 ] && grep -qE "one-way.*$label|$label.*one-way" "$EXPECT_FILE"; then
      echo "LISTED as one-way in $EXPECT_FILE but reverses: $label"; worse 10
    fi
  done
  [ "$STATUS" -lt 10 ] && echo "one-way list in $EXPECT_FILE matches"
fi

echo
case "$STATUS" in
  0) echo "reverse migrations $FROM_REF..$TO_REF: OK" ;;
  10) echo "reverse migrations $FROM_REF..$TO_REF: DEGRADED (one-way list and release notes disagree)" ;;
  *) echo "reverse migrations $FROM_REF..$TO_REF: ROLL BACK (a migration failed to reverse or re-apply)" ;;
esac
exit "$STATUS"
