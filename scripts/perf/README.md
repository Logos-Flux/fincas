# Real-volume performance gate

The volume gate of `docs/release-hardening-plan.md` section 8: every list,
search, report and the engagement page inside its query budget and p95 under
500 ms on a clone of the scrubbed copy, on gunicorn with `DEBUG=False`. A 10x
synthetic run is recorded as a trend and never blocks. Reports live in
`docs/perf/`; numbers are host-relative, only regressions on the same host
gate.

Two scripts:

- `measure_pages.py`: query counts in-process (`connection.execute_wrapper`,
  two page sizes per list, busiest and leanest row per detail page) and
  p50 / p95 / max over HTTP against a gunicorn the caller starts. Exit 1 when
  any page is over budget. Writes JSON and a markdown table.
- `synthesize_volume.py`: multiplies a clone's business rows (`bulk_create`
  inside `suppressed()`), then runs `reconcile_parties --fix`, the engagement
  backfills and `reconcile_engagements --fix`, and requires
  `reconcile_engagements --check`, `reconcile_parties --check` and
  `check_engagements` clean. Refuses a database whose name lacks `_perf_`.

## Exact commands (any Postgres 17 server you can create databases on)

`H` is the server's host and `golden` a role that may create databases; the
scrubbed copy is a database named `golden_synth` on the same server. Read the
password into `PGPASSWORD` from your secret store, never from the command line.

```bash
cd path/to/your/checkout
PY=.venv/bin/python
export PGPASSWORD="..."        # the golden role's password, from your secret store
H=db.example.internal

# 1. A disposable clone of the scrubbed copy (never run migrate on golden_synth).
createdb -h $H -U golden -E UTF8 golden_perf_1x
pg_dump -h $H -U golden -Fc golden_synth | pg_restore -h $H -U golden --no-owner --no-acl -d golden_perf_1x
DATABASE_URL="postgres://golden:$PGPASSWORD@$H/golden_perf_1x" $PY manage.py migrate --noinput --settings=config.settings.dev

# 2. Prod-like environment: golden_erp.settings.prod through golden's config shell,
#    DEBUG off, the three E060 secrets set to throwaway values, locmem cache.
umask 077
{ echo DJANGO_SETTINGS_MODULE=config.settings.prod
  echo ALLOWED_HOSTS=127.0.0.1
  echo SECURE_SSL_REDIRECT=False
  echo "DATABASE_URL=postgres://golden:$PGPASSWORD@$H/golden_perf_1x"
  DJANGO_SETTINGS_MODULE=config.settings.dev $PY manage.py generate_secrets
} > /tmp/perf-1x.env           # holds the DB password and the throwaway keys; delete afterwards

# 3. gunicorn, 3 sync workers, logs to .runs/perf/ (gitignored).
mkdir -p .runs/perf
( set -a; . /tmp/perf-1x.env; set +a; TMPDIR=$(mktemp -d) \
  .venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8830 --workers 3 --timeout 120 \
  --access-logfile .runs/perf/gunicorn-1x-access.log --error-logfile .runs/perf/gunicorn-1x-error.log & )

# 4. Measure: 3 warm-ups, 30 requests per URL.
( set -a; . /tmp/perf-1x.env; set +a; TMPDIR=$(mktemp -d) \
  $PY scripts/perf/measure_pages.py --label 1x --base http://127.0.0.1:8830 \
    --out .runs/perf/1x.json --markdown .runs/perf/1x.md )

# 5. The 10x trend: a second clone, multiplied, repaired, measured the same way.
createdb -h $H -U golden -E UTF8 golden_perf_10x
pg_dump -h $H -U golden -Fc golden_synth | pg_restore -h $H -U golden --no-owner --no-acl -d golden_perf_10x
DATABASE_URL="postgres://golden:$PGPASSWORD@$H/golden_perf_10x" $PY manage.py migrate --noinput --settings=config.settings.dev
DATABASE_URL="postgres://golden:$PGPASSWORD@$H/golden_perf_10x" DJANGO_SETTINGS_MODULE=config.settings.dev \
  $PY scripts/perf/synthesize_volume.py --factor 10 > .runs/perf/synth-10x.log 2>&1
sed 's#/golden_perf_1x#/golden_perf_10x#' /tmp/perf-1x.env > /tmp/perf-10x.env
# start gunicorn on :8831 with perf-10x.env, then
# measure_pages.py --label 10x --base http://127.0.0.1:8831 --out .runs/perf/10x.json

# 6. Clean up: stop the gunicorns, drop the clones, delete the env files.
pkill -f "gunicorn config.wsgi:application --bind 127.0.0.1:883"
dropdb -h $H -U golden golden_perf_1x; dropdb -h $H -U golden golden_perf_10x
rm -f /tmp/perf-1x.env /tmp/perf-10x.env
```

`measure_pages.py` creates a non-superuser staff login `perf-staff` on the
clone (random password, never printed) with a `UserProfile` on the first
company, and a session row for the HTTP pass. The in-process pass uses the
Django test client, so `response.context` gives each list's row count.

## Reading the output

| column | meaning |
|---|---|
| rows | rows on the page (lists: the default page size; details: related rows of the busiest row) |
| queries | queries for the URL as listed (lists at the viewer's page size, details on the busiest row) |
| q@small / q@100 | lists: queries at 10 and at 100 rows; details: queries on the leanest row |
| budget | the suite's cap for pinned pages, else "no growth" (queries must not rise with rows) |

A page fails on a cap breach, on growth (list: q@100 > q@10; detail: more
than three queries over the leanest row with a repeated shape), on p95 over
the budget, or on a non-200. The JSON carries the slowest query's shape
(literals replaced, column list folded) and the shapes that repeated more
than twice in one request, which is the N+1 signature.

## What the synthesis does and does not do

Copies parties (not the tenant's own, merged or erased ones), their e-mails,
phones, addresses, roles and relationships; customers and addresses;
engagements with stage history, tasks, communications and leads; quotes,
orders, lines, shipment groups, sub-orders, invoices and payments; journal
entries with lines. Document numbers get `-S<k>`, e-mail addresses
`s<k>.` on the local part, external ids a suffix, hashes are re-derived, and
every date moves back `3 * k` days for copy `k` (`created_at` included: the
`auto_now` stamps are disabled for the run). Portal logins and
purchase-order / pick-ticket links are blanked on the copies. Touchpoints,
external refs, email logs and the catalog are not copied; `reconcile_parties
--fix` recreates what the party sync derives.

Committed files hold counts and shapes only: nothing from the scrubbed copy
(names, amounts, addresses) belongs in `docs/perf/` or in a PR.
