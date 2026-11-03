# Deploying Fincas to production

This guide covers what a production Fincas instance needs (Postgres 17, the
required secrets, a worker for the outbox, a few scheduled jobs) and three ways
to run it: Docker Compose, systemd on a plain host, and Fly.io. It assumes you
already have Fincas running locally; if not, start with
[getting-started.md](getting-started.md).

Fincas runs **one company per instance** (see
[One instance per company](#one-instance-per-company)). Supported stack:
Python 3.12, Django 6.0, PostgreSQL 17. SQLite is for development and tests only.

Two ways to deploy:

- **From a checkout of this repository.** The repo's own project shell
  (`config/`, `manage.py`, `Dockerfile`, `docker-compose.prod.yml`) runs as is,
  with `DJANGO_SETTINGS_MODULE=config.settings.prod`.
- **From PyPI in your own project** (`pip install "fincas[prod]"`), with your own
  `config/settings.py` that starts from `golden_erp.settings.prod`
  ([packaging.md](packaging.md#downstream-settings-and-urls)). This is the
  path for a deployment that adds its own plugins or templates. Use the
  commands below with your own settings module.

---

## 1. Environment variables

Fincas reads its configuration from environment variables.

| Variable | Required? | Default | Description |
| :--- | :--- | :--- | :--- |
| `DJANGO_SETTINGS_MODULE` | Yes | - | `config.settings.prod` in a checkout, or your own settings module. |
| `DJANGO_SECRET_KEY` | Yes | - | High-entropy secret for sessions, signed cookies and password-reset links. |
| `API_JWT_SIGNING_KEY` | Yes (prod) | - | Signs API bearer tokens. See [Required secrets](#required-secrets). |
| `LLMHUB_FERNET_KEY` | Yes (prod) | - | Encrypts the stored LLM gateway token. |
| `GOLDEN_SECRET_KEYS` | Yes (prod) | - | Fernet keys, comma-separated, newest first; encrypt connected-account credentials. |
| `ALLOWED_HOSTS` | Yes | - | Comma-separated host names (e.g. `erp.example.com`). |
| `CSRF_TRUSTED_ORIGINS` | Yes (prod) | - | Comma-separated origins (e.g. `https://erp.example.com`). |
| `DATABASE_URL` | Yes | - | `postgres://user:pass@host:5432/dbname`. See [Database](#database). |
| `DB_SSL_REQUIRE` | No | auto | `true`/`false`. By default TLS is required unless the host is `db`, `localhost` or `127.0.0.1`. |
| `REDIS_URL` | Recommended | - | Valkey/Redis for the cache and the throttle counters. Without it each process keeps its own in-memory cache, which is fine for a single web process. |
| `TRUSTED_PROXIES` | Behind a proxy | empty | Comma-separated IPs or CIDRs of your own reverse proxies. Throttles keyed by client address (login, OIDC start, token, coupon) read `X-Forwarded-For` past these hops; left empty behind a proxy, every visitor shares the proxy's bucket (warning `core.W114`). A malformed entry fails `check` (`core.E080`). |
| `GOLDEN_CACHE_DOWN_FAIL_OPEN` | No | `False` | With the cache unreachable, the cache-counted gates refuse (login 503, API throttles 429, OIDC start 503). Set to `1` during a cache outage to let attempts through uncounted. Pages degrade on their own. |
| `GOLDEN_PORTAL_COMPANY` | No | - | The Company the customer portal signs in to. Optional with one Company. |
| `SECURE_SSL_REDIRECT` | No | `True` | Set `False` only if the proxy already redirects. An empty value also turns it off. |
| `SECURE_HSTS_SECONDS` | No | `31536000` | HSTS max-age. An empty value fails at import. |
| `MAILGUN_API_KEY`, `MAILGUN_SENDER_DOMAIN`, `DEFAULT_FROM_EMAIL` | For email | - | Outgoing mail through Mailgun (django-anymail). |
| `AUTO_MIGRATE` | No | `0` | `1` makes the container entrypoint run `migrate` before starting. |
| `SENTRY_DSN` | No | empty | Error reporting (needs the `[prod]` extra). |
| `STRIPE_SECRET_KEY` | Optional | empty | Stripe payment gateway. |
| `EASYPOST_API_KEY` | Optional | empty | EasyPost shipping rates and labels. |

`.env.example` in the repository lists every variable the settings read.

A setting's default applies only when the variable is **absent**: an empty value
is a value. Pass your environment file whole (Compose `env_file`, systemd
`EnvironmentFile=`) rather than a list of `${NAME:-}` passthroughs, which turn
"unset" into "empty".

### Required secrets

`golden_erp.settings.prod` refuses to run without its own signing and
encryption keys: system check **`core.E060`** fails `migrate` and `check` until
all of them are set. Because `migrate` runs on every deploy, a release with a
missing secret stops before any server starts. Outside production these keys
fall back to values derived from `DJANGO_SECRET_KEY`, which is fine for
development and wrong for production: rotating the secret key would then void
every API token and make every stored credential unreadable.

| Secret | Protects | Setting a new value on a running install |
| :--- | :--- | :--- |
| `DJANGO_SECRET_KEY` | sessions, password-reset links, signed cookies | signs every user out |
| `API_JWT_SIGNING_KEY` | API bearer tokens (`/api/v1/core/auth/token/`, plugin tokens) | voids issued bearer tokens: re-mint or re-pair each client; `Api-Key` callers are unaffected |
| `LLMHUB_FERNET_KEY` | the LLM gateway token saved on `/erp/llm/` | first time: harmless, the old derived key still decrypts; replacing one dedicated key with another: re-enter the token |
| `GOLDEN_SECRET_KEYS` | connected-account credentials (`core.IntegrationCredential`) | never replace: prepend the new key, run `manage.py rotate_secrets`, then drop the old one ([crm/adapters.md](crm/adapters.md#keys)) |

Generate a fresh set, already in environment-file syntax:

```bash
python manage.py generate_secrets              # all four
python manage.py generate_secrets --only API_JWT_SIGNING_KEY
```

The command skips system checks, so it runs while `core.E060` is failing. Put
the output wherever your host keeps secrets:

- **Docker Compose:** append it to the `.env` file the stack reads.
- **systemd:** an `EnvironmentFile=` owned by root, mode `0600`.
- **Fly.io:** `python manage.py generate_secrets | fly secrets import -a <app>`
  (stdin, so nothing lands in shell history or the process list).
- **Kubernetes:** a `Secret` mounted with `envFrom`.

Keep a copy in your team's secret store. Losing `GOLDEN_SECRET_KEYS` loses every
stored credential (check `core.E040` names them); losing the others costs a
re-mint, a sign-out or a re-entered token.

---

## 2. What runs

| Process | Command | Notes |
| :--- | :--- | :--- |
| web | `uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers 2` | ASGI; static files served by WhiteNoise from `collectstatic` output |
| outbox worker | `python manage.py process_outbox --loop` | delivers webhooks and integration syncs; see [Scheduled jobs](#scheduled-jobs) |
| migrate | `python manage.py migrate --noinput` | once per deploy, before the new web processes start |
| scheduler | `crm_emit_due` every 5 minutes, plus the integration jobs you enable | cron, systemd timers or the platform's scheduler |

Back-office pages load Google Fonts and Font Awesome from public CDNs. A
deployment that must work offline has to vendor both.

---

## 3. Docker Compose

`docker-compose.prod.yml` runs Postgres, Valkey, the web container and an
outbox worker on one host, built from the repository's `Dockerfile`.

**Step 1.** Create `.env` next to the compose file:

```bash
POSTGRES_DB=golden_erp
POSTGRES_USER=golden
POSTGRES_PASSWORD=<openssl rand -hex 24>
DJANGO_SECRET_KEY=<openssl rand -hex 32>
ALLOWED_HOSTS=erp.example.com
CSRF_TRUSTED_ORIGINS=https://erp.example.com
TRUSTED_PROXIES=172.16.0.0/12        # the proxy in front of the container
PORT=8000
```

Then append the required secrets:

```bash
python manage.py generate_secrets >> .env
```

(Run it from any environment with Fincas installed, or after step 2 with
`docker compose -f docker-compose.prod.yml run --rm web python manage.py generate_secrets`.)

**Step 2.** Start the stack:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

The web container runs `migrate` on start and reports healthy once uvicorn
answers; the outbox worker starts after that, so `up -d` waits for web. If web
is not healthy within about six minutes (a long migration), `up` fails with the
worker not started: run `up -d` again.

**Step 3.** Create the first staff account:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

Static files are collected into the image at build time. Never mount a volume
over `/app/staticfiles`: a named volume is filled once, and every later image's
CSS and JavaScript would be hidden behind the first deploy's copy. Uploaded
media lives in the `media_data` volume.

---

## 4. systemd on a plain host

This chapter assumes a project directory `/srv/fincas` holding a virtualenv
with `fincas[prod]` installed and your `config/` package (settings, `urls.py`
and `asgi.py`, as in [getting-started.md](getting-started.md)), run by a system
user `fincas`.

**Environment file** `/etc/fincas/env` (root-owned, mode `0600`):

```bash
DJANGO_SETTINGS_MODULE=config.settings
DATABASE_URL=postgres://fincas:<password>@127.0.0.1:5432/fincas
REDIS_URL=redis://127.0.0.1:6379/0
ALLOWED_HOSTS=erp.example.com
CSRF_TRUSTED_ORIGINS=https://erp.example.com
TRUSTED_PROXIES=127.0.0.1
# plus DJANGO_SECRET_KEY and the output of `manage.py generate_secrets`
```

**Release steps**, run as `fincas` after each upgrade:

```bash
cd /srv/fincas
set -a; . /etc/fincas/env; set +a
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart fincas-web fincas-outbox
```

(`manage.py` here is your project's; `django-admin` with the same environment
works too.)

**Web service** `/etc/systemd/system/fincas-web.service`:

```ini
[Unit]
Description=Fincas web
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=fincas
WorkingDirectory=/srv/fincas
EnvironmentFile=/etc/fincas/env
ExecStart=/srv/fincas/.venv/bin/uvicorn config.asgi:application --host 127.0.0.1 --port 8000 --workers 2 --no-access-log
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

**Outbox worker** `/etc/systemd/system/fincas-outbox.service`: the same unit
with `Description=Fincas outbox` and

```ini
ExecStart=/srv/fincas/.venv/bin/python manage.py process_outbox --loop
```

**Due-task job**: a oneshot service and a timer.

```ini
# /etc/systemd/system/fincas-crm-due.service
[Unit]
Description=Fincas: emit due CRM tasks

[Service]
Type=oneshot
User=fincas
WorkingDirectory=/srv/fincas
EnvironmentFile=/etc/fincas/env
ExecStart=/srv/fincas/.venv/bin/python manage.py crm_emit_due
```

```ini
# /etc/systemd/system/fincas-crm-due.timer
[Unit]
Description=Run fincas-crm-due every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fincas-web fincas-outbox fincas-crm-due.timer
```

Put a reverse proxy in front of port 8000 ([section 6](#6-reverse-proxy-and-tls)).

---

## 5. Fly.io

[deploy-fly.md](deploy-fly.md) walks through a Fly deployment: app and Managed
Postgres, secrets, `fly.toml`, release command, custom hostname, and the
storefront as a second app.

---

## 6. Reverse proxy and TLS

Terminate TLS in a reverse proxy and forward to the web process. Production
settings trust `X-Forwarded-Proto: https` from the proxy, and the proxy's
address belongs in `TRUSTED_PROXIES`.

### Caddy

```caddy
erp.example.com {
    encode gzip zstd
    reverse_proxy localhost:8000
}
```

### Nginx

```nginx
server {
    listen 80;
    server_name erp.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name erp.example.com;

    ssl_certificate /etc/letsencrypt/live/erp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/erp.example.com/privkey.pem;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

### Kubernetes and other container platforms

The web and outbox containers are stateless (static files are in the image;
put media on object storage with `django-storages` or a persistent volume). Run
`migrate --noinput` as a `Job` or release phase before rolling out new pods, the
outbox worker as its own `Deployment`, and the scheduled jobs as `CronJob`s.
`/erp/login/` answers `200` once the app is up and makes a usable health check
(send a `Host` from `ALLOWED_HOSTS` and `X-Forwarded-Proto: https`).

---

## Database

- **PostgreSQL 17.** CI runs the full test suite, the migrations and the upgrade
  path on Postgres 17.
- Production settings keep connections open for 60 seconds and disable
  server-side cursors, so a transaction-mode pooler does not break queries.

### Known limitation: the CRM import needs a direct connection

`manage.py crm_import` serializes runs with a Postgres **session** advisory lock
(`crm_import/stage.py`). A transaction-mode pooler (PgBouncer in transaction
mode, Supavisor, a managed database's pooled endpoint) hands each transaction to
whichever backend is free, so a session lock can be taken on one backend and
never released, or released on the wrong one. The lock then leaks: later imports
for the same source refuse to start until that backend's session ends.

Run imports against the database's **direct** host, not the pooler. The
simplest setup is to point the whole application at the direct host when its
connection count allows it (a couple of web workers and one outbox worker is a
handful of connections). Otherwise keep the pooled URL for web and set a direct
`DATABASE_URL` only in the environment that runs `crm_import`.

---

## One instance per company

Fincas runs **one company per instance**: one database, one `core.Company`
row, one deployment. Another company gets its own instance (its own database,
settings and host), not a second Company in this one. Per-company plumbing in
the code (`company` foreign keys, `scope_to_company`) remains and keeps
working, but nothing supports two companies side by side.

Three guards enforce it:

- **System check `core.E070`** (an Error) fires when more than one Company
  exists, naming each one by pk and name:

  > 2 companies exist (…: Acme Corp; …: Beta Ltd). golden runs one company
  > per instance; keep one Company and run a separate instance for another
  > company. See docs/deployment.md#one-instance-per-company.

  It is tagged `database`, so it runs on **`manage.py migrate`** and
  **`manage.py check --database default`**, never on `runserver` or a plain
  `check`. It passes on an empty database and before the `core_company` table
  exists (the first `migrate`). There is no opt-out: `migrate` refuses until
  one Company remains. On a database that already holds several, keep the
  one this instance serves and move the other company's data to its own
  instance (company foreign keys `PROTECT` their rows, so a Company cannot
  simply be deleted while anything references it; a new instance with a
  fresh seed is usually the right answer).
- **The Django admin** offers no "Add company" once one exists (the add view
  answers 403 with the same message), and the changelist says why.
- **The API** (`POST /api/v1/core/companies/`) refuses a second Company with
  `409` and a stable `code` of `one_company_per_instance` (the `detail` is
  the message above). Clients branch on the code, never the text.

`Company.save()` itself stays permissive: the test suite and
`seed_test_edge` build two companies to keep proving the per-company
scoping. `parties.E001` (portal sign-in configured, several Companies,
`GOLDEN_PORTAL_COMPANY` unset) is unchanged; with one Company the portal
resolves it on its own and the setting is optional.

### Tenancy backfill (older databases)

A database that predates the tenancy columns has every `Warehouse.company`
NULL and no `UserProfile` rows for its staff. Company-scoped reads fail closed
on NULL, so stock disappears and session users resolve no company. Run the
idempotent backfill once:

```bash
python manage.py backfill_tenancy --company <uuid-or-exact-name> --dry-run
python manage.py backfill_tenancy --company <uuid-or-exact-name>
```

It assigns NULL-company warehouses and profiles to the company, creates a
`UserProfile` for each active staff user without one, and lists unlinked
profiles without linking them. Other company-scoped models are only reported
unless named with `--also app_label.Model`. Run `--help` for details. A blank
report page on a fresh deployment usually means this has not run yet.

---

## Scheduled jobs

### `process_outbox` (always on when webhooks or integrations are enabled)

Webhook deliveries and integration syncs are rows in the outbox
(`core.OutboxEvent`), written in the transaction that made the change and
delivered by a separate worker with no transaction open. Run **one** of:

- a long-running worker: `python manage.py process_outbox --loop` (the
  compose file's `outbox` service, the systemd unit above). Several replicas
  may run at once: each row is claimed with a conditional update and a lease.
- every minute from a scheduler:

  ```cron
  * * * * * cd /app && python manage.py process_outbox --once --max-seconds 50
  ```

`--consumer NAME` limits a worker to some consumers. `--prune` (for example once
an hour) deletes finished rows past the `GOLDEN_OUTBOX` retention (done 7 days,
`*.inbound` done 1 day, canceled 7 days; dead rows stay).

Monitor it with `manage.py check_outbox [--max-age-minutes 30]`, which exits 1
when a row is dead or the oldest pending row is older than the threshold.
Without a worker nothing is lost; rows only wait. Dead letters:
`manage.py outbox list --state dead`, then `manage.py requeue_outbox <id> …` or
`manage.py outbox requeue --consumer NAME` after the fix.

### `crm_emit_due` (every 5 minutes)

`task_due` (and the `task.due` webhook) is time-driven: nothing sends it from a
save. Run the command every 5 minutes:

```cron
*/5 * * * * cd /app && python manage.py crm_emit_due
```

It sends once for each open CRM task whose `due_at` has passed, claiming the
task's `due_notified_at` with a conditional update and writing the `task.due`
outbox row in the same transaction, so two overlapping runs send once and a
re-run sends nothing new. A missed run sends late, never twice for one due
time; rescheduling a task clears the marker. `--limit N` caps one run (oldest
first).

### Integration jobs

Connected CRM adapters add their own scheduler lines (`integrations_poll`,
`integrations_sync`, `check_erasure`) and a webhook ingress. Per-adapter wiring
is in [crm/adapters.md](crm/adapters.md).

---

## Backups and restore

Take daily dumps in custom format and keep them off the database host:

```bash
pg_dump -Fc -d "$DATABASE_URL" > /backups/fincas_$(date +%F).dump
```

Restore into an empty database, then migrate (a dump may predate the running
release):

```bash
pg_restore --no-owner --no-acl -d "$DATABASE_URL" /backups/fincas_2026-11-01.dump
python manage.py migrate --noinput
```

Practice the restore before you rely on it. Keep `GOLDEN_SECRET_KEYS` with the
backups' recovery instructions: a restored database without it cannot decrypt
its stored credentials.

---

## Upgrading

Read [upgrading.md](upgrading.md) for the version policy and the migration
contract, and the release notes in [releases/](releases/) for the version you
are moving to. The short form: pin `fincas==X.Y.Z`, back up, install the new
version, run `migrate`, restart web and the outbox worker, then run the checks
below.

## Operator commands

These ship in the wheel and work against any deployment.

- **`manage.py smoke_cutover [--dead-max N] [--overdue-minutes M] [--json]`**
  runs post-deploy smoke checks against the current database: one Company, no
  unapplied migrations, a staff login, the storefront identity, outbox dead and
  overdue rows, engagement and party drift. It lists the checks that stay
  manual. Each check prints
  `name: observed=… threshold=… -> OK|DEGRADED|ROLL BACK (where to act)`.
- **`manage.py check_money [--limit N] [--samples N] [--json]`** recomputes
  every quote, order, sales invoice and purchase order with the model's own
  `recalculate_totals()` inside one transaction that is always rolled back,
  and buckets each difference by a named cause (legacy import, finalized
  invoice snapshot, rounding, coupon excess, stale line totals, re-derived
  discount, discount applied at a different level, stale tax, no lines, stale
  or zeroed balance, invoice/order drift, unexplained). It also checks that
  the trial balance is zero (posted journal entries) and that every invoice's
  `balance_due` is `grand_total - amount_paid` and its `amount_paid` is the sum
  of its captured or settled payments. Exit 20 when a posted invoice is off by
  more than one minor unit with no historical cause, when the ledger does not
  balance, or when a posted invoice's balance does not add up; 10 when every
  difference has a named cause; 0 when the stored totals are exactly what the
  code computes. `--limit N` examines the newest N documents per type.
- **`manage.py check_numbering [--samples N] [--json]`** checks document
  numbers for quotes, orders, invoices, purchase orders, engagements, leads and
  payments: a number held by more than one row exits 20; a later row carrying a
  lower sequence than an earlier one, or a blank number, exits 10; gaps are
  reported as a statistic. Quotes, orders, invoices, engagements and payments
  share one `DocumentSequence`, so gaps inside one type are expected. Numbers
  outside `PREFIX-<digits>` (imports) are counted and skipped.
- **`manage.py check_schema_parity`** (Postgres only) snapshots the live
  schema as sorted JSON (`--snapshot PATH`) or diffs it against a snapshot
  (`--compare BASELINE`, or `--compare BASELINE --against OTHER` with no
  database). Use it to confirm a restored or upgraded database has the schema
  a fresh `migrate` produces: snapshot a scratch database migrated from
  scratch, then compare production against it. Exit 0 for no differences, 1
  for a difference, 2 when it could not run.
- **`manage.py release_notes <from> <to> [--write PATH]`** (maintainers, from
  a git checkout with full history) lists what changed between two refs:
  migrations added and whether each reverses, required secrets, settings added
  or removed, new environment reads, and the commit subjects.

Reverse migrations are checked by
`scripts/upgrade/check_reverse_migrations.sh <from-ref> <to-ref>` (in a
checkout, with `DATABASE_URL` naming an empty scratch Postgres database): it
reverses and re-applies each migration added between the refs and prints the
one-way list.

### Exit codes

Every operator command uses this table (`core/management/release_exit_codes.py`).

| Code | Name | Meaning |
| :--- | :--- | :--- |
| `0` | OK | Every check passed. |
| `2` | USAGE | Bad arguments, or an environment the command refuses (shallow clone, unknown ref). Nothing was checked. |
| `10` | DEGRADED | Something needs a person, but the release can stay up (dead outbox rows, drift a `--fix` repairs). |
| `20` | ROLL BACK | The release must not stay up (two companies, unapplied migrations, no staff login). Restore the previous version and its database backup. |

`smoke_cutover` exits with the worst status of its checks.
