#!/usr/bin/env bash
# Upgrade-path test (issue #88): a database built and filled by the previous
# release must `migrate` cleanly with HEAD, and come out consistent.
#
#   PREVIOUS_TAG=v0.4.0-rc.1 DATABASE_URL=postgres://... scripts/upgrade/check_upgrade_path.sh
#
# DATABASE_URL must name an EMPTY database (the script never creates or drops
# one). PYTHON defaults to `python`; both trees share its environment, so the
# previous tag's requirements must be installable alongside HEAD's (true while
# requirements/ is unchanged between them; CI checks that below).
#
# Steps: check out PREVIOUS_TAG in a temporary git worktree; there, migrate the
# empty database, run seed_demo_data, and seed the rows the later data
# migrations key on (scripts/upgrade/seed_previous_release.py: EmailLogs,
# Leads, ActivityLogs, a quote with pipeline data). Then, with HEAD: migrate,
# check_engagements, reconcile_engagements --check, check_schema_parity --compare.
# A fresh database never reaches the data migrations' row-dependent paths;
# this does.
set -euo pipefail

: "${PREVIOUS_TAG:?set PREVIOUS_TAG, e.g. v0.3.0-rc.1}"
: "${DATABASE_URL:?set DATABASE_URL to an empty database}"
PYTHON=${PYTHON:-python}
SETTINGS=--settings=config.settings.dev
HEAD_DIR=$(git rev-parse --show-toplevel)
TMP_DIR=$(mktemp -d)
OLD_DIR=$TMP_DIR/previous

cleanup() { git -C "$HEAD_DIR" worktree remove --force "$OLD_DIR" 2>/dev/null || true; rm -rf "$TMP_DIR"; }
trap cleanup EXIT

git -C "$HEAD_DIR" rev-parse -q --verify "refs/tags/$PREVIOUS_TAG" >/dev/null \
  || git -C "$HEAD_DIR" fetch --quiet --depth=1 origin "refs/tags/$PREVIOUS_TAG:refs/tags/$PREVIOUS_TAG"
git -C "$HEAD_DIR" worktree add --quiet --detach "$OLD_DIR" "$PREVIOUS_TAG"

if ! git -C "$HEAD_DIR" diff --quiet "$PREVIOUS_TAG" HEAD -- requirements/; then
  echo "note: requirements/ differs between $PREVIOUS_TAG and HEAD; the previous tree runs on HEAD's environment"
fi

# Refuse anything but an empty database: the seed resets the admin password
# and adds demo rows, which must never land in a real one.
"$PYTHON" manage.py shell $SETTINGS -c '
from django.db import connection
if connection.introspection.table_names():
    raise SystemExit("refusing: DATABASE_URL is not an empty database")
' || exit 1

echo "== $PREVIOUS_TAG: migrate an empty database and seed it"
(
  cd "$OLD_DIR"
  "$PYTHON" manage.py migrate --noinput $SETTINGS | tail -1
  "$PYTHON" manage.py seed_demo_data --admin-password "$(openssl rand -hex 16)" $SETTINGS >/dev/null
  "$PYTHON" manage.py shell $SETTINGS < "$HEAD_DIR/scripts/upgrade/seed_previous_release.py"
)

# Did the previous tag already apply the email backfill (v0.4.0-rc.1 on)? Then
# the seeded rows arrived after it, HEAD's migrate never meets them, and the
# runbook's order applies: reconcile_engagements --fix, then --check.
BACKFILLED_BEFORE=$("$PYTHON" manage.py shell $SETTINGS -c '
from django.db.migrations.recorder import MigrationRecorder
print(int(MigrationRecorder.Migration.objects.filter(
    app="communications", name="0003_backfill_email_communications").exists()))
' | tail -1)

cd "$HEAD_DIR"
echo "== HEAD ($(git rev-parse --short HEAD)): migrate"
"$PYTHON" manage.py migrate --noinput $SETTINGS
echo "== HEAD: checks"
"$PYTHON" manage.py check_engagements $SETTINGS
# From a tag before the backfill: --check, no --fix first (the migrations
# themselves must have linked every seeded row). From a later tag the seeded
# rows postdate the backfill, so --fix first, as the runbook does.
if [ "$BACKFILLED_BEFORE" = 1 ]; then
  # Only the seeded email logs may be drift here: anything else means HEAD's
  # migrations broke rows that the previous tag's own code wrote correctly.
  drift=$("$PYTHON" manage.py reconcile_engagements --check $SETTINGS 2>&1 | grep -E ': [1-9][0-9]*$' || true)
  echo "$drift"
  if echo "$drift" | grep -qvi 'email'; then
    echo "unexpected drift after HEAD's migrate (only email logs may need --fix)"; exit 1
  fi
  "$PYTHON" manage.py reconcile_engagements --fix $SETTINGS
fi
"$PYTHON" manage.py reconcile_engagements --check $SETTINGS
# docs/pin/ is dev-only (scripts/release/dev-only.txt): no baseline in the public tree.
if [ -f docs/pin/schema-baseline.json ]; then
  "$PYTHON" manage.py check_schema_parity \
    --compare docs/pin/schema-baseline.json --expect-diff docs/pin/expected-diff.txt $SETTINGS
else
  echo "note: docs/pin/schema-baseline.json is not in this tree; schema comparison skipped"
fi
"$PYTHON" manage.py migrate --check $SETTINGS
if [ "$BACKFILLED_BEFORE" = 1 ]; then
  echo "communications.0003 was applied at $PREVIOUS_TAG; its row path is covered by an older PREVIOUS_TAG"
  echo "upgrade path $PREVIOUS_TAG -> HEAD: OK"
  exit 0
fi
# The seeded EmailLogs went through communications.0003 (not around it).
"$PYTHON" manage.py shell $SETTINGS -c '
from communications.models import Communication, EmailLog
logs = EmailLog.objects.exclude(document_type="purchase_order").count()
backfilled = Communication.objects.filter(email_log__isnull=False, metadata__backfill_step=6)
linked = backfilled.count()
print(f"email logs (non-PO): {logs}; backfilled communications: {linked}")
if not logs or linked != logs:
    raise SystemExit("communications.0003 did not backfill every seeded EmailLog")
# The seeded customer has a party and the sender a profile in the same company.
if backfilled.filter(party__isnull=True).exists() or backfilled.filter(performed_by__isnull=True).exists():
    raise SystemExit("a backfilled email lost its party or sender")
if backfilled.filter(participants__party__isnull=True).exists():
    raise SystemExit("a backfilled email participant did not resolve to the party")
if not backfilled.filter(engagement__isnull=False).exists():
    raise SystemExit("the quote email did not land on its engagement")
'
echo "upgrade path $PREVIOUS_TAG -> HEAD: OK (communications.0003 row path exercised)"
