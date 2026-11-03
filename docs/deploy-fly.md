# Deploying Fincas on Fly.io

This chapter deploys the back office (web and outbox worker) on Fly.io with
Fly Managed Postgres 17, and optionally the storefront as a second app. It
builds the repository's production `Dockerfile` from a checkout. Read
[deployment.md](deployment.md) first for the environment variables and the
required secrets.

Replace the placeholders throughout:

| Placeholder | Example |
|---|---|
| `<app>` | `acme-erp` (the back-office app) |
| `<shop-app>` | `acme-shop` (the storefront app, optional) |
| `<org>` | your Fly organization slug |
| `<region>` | `iad` |
| `erp.example.com`, `shop.example.com` | your hostnames |

`fly` is `flyctl`. No command here takes a secret on its command line.

## What runs

| Piece | What | Suggested size |
|---|---|---|
| `web` process | the production `Dockerfile`, uvicorn with 2 workers | shared-cpu-2x, 2 GB |
| `outbox` process | same image, `manage.py process_outbox --loop` | shared-cpu-1x, 512 MB |
| release command | `manage.py migrate`, once per deploy, in a temporary machine | |
| Managed Postgres 17 | Basic plan is enough to start, same region as the app | |
| Redis (optional) | without it each web machine keeps its own cache and throttle counters, fine for one web machine | |
| storefront (optional) | Qwik SSR under `vite preview`, can auto-stop when idle | shared-cpu-1x, 512 MB |

Uploaded media lives on the machine's disk, which a deploy replaces. If your
deployment takes uploads, add a Fly volume on `/app/media` (one web machine) or
configure object storage before going live.

## 1. App and database

```sh
fly apps create <app> --org <org>
fly mpg create --name <app>-db --org <org> --region <region> \
  --plan Basic --pg-major-version 17 --volume-size 10
fly mpg attach <cluster id> -a <app>
```

`mpg create` prints the connection string once; don't paste it anywhere.
`mpg attach` stores it as the `DATABASE_URL` secret, pointing at the cluster's
PgBouncer (`pgbouncer.<id>.flympg.net`).

**Point the app at the direct host instead** (`direct.<id>.flympg.net`). The
CRM import holds Postgres session advisory locks, which do not survive a
transaction pooler ([deployment.md](deployment.md#known-limitation-the-crm-import-needs-a-direct-connection)).
Two uvicorn workers and one outbox worker are a handful of connections, well
inside the Basic plan's limit. To rewrite the secret without printing it, run
a small script on a running machine that prints `DATABASE_URL=` followed by its
own `DATABASE_URL` with `@pgbouncer.` replaced by `@direct.`, and pipe that
into `fly secrets import --stage -a <app>`.

The cluster role does not own schema `public` and cannot create databases.
`migrate` needs neither.

## 2. Secrets

```sh
python manage.py generate_secrets | fly secrets import --stage -a <app>
```

That stages `DJANGO_SECRET_KEY` and every required key (`API_JWT_SIGNING_KEY`,
`LLMHUB_FERNET_KEY`, `GOLDEN_SECRET_KEYS`). With one missing, the release
command's `migrate` fails on `core.E060` and the release stops before any
machine changes. Keep a copy in your team's secret store.

Add any integration keys the same way (`STRIPE_SECRET_KEY`,
`EASYPOST_API_KEY`, `MAILGUN_API_KEY`, …).

## 3. Configuration (`fly.toml`)

Put everything that is not a secret in `fly.toml` at the repository root:

```toml
app = "<app>"
primary_region = "<region>"
kill_signal = "SIGTERM"
kill_timeout = "30s"

[build]
  dockerfile = "Dockerfile"

[deploy]
  # Migrates once per release, before any process starts on the new image.
  # A failed migrate (or core.E060) stops the release; the old machines keep running.
  release_command = "python manage.py migrate --noinput"
  strategy = "rolling"

[env]
  DJANGO_SETTINGS_MODULE = "config.settings.prod"
  AUTO_MIGRATE = "0"
  ALLOWED_HOSTS = "erp.example.com,<app>.fly.dev"
  CSRF_TRUSTED_ORIGINS = "https://erp.example.com,https://<app>.fly.dev"
  # Fly's edge proxy connects from its private ranges.
  TRUSTED_PROXIES = "172.16.0.0/12,fdaa::/16"
  # Only with the storefront app (section 7):
  # CORS_ALLOWED_ORIGINS = "https://shop.example.com,https://<shop-app>.fly.dev"
  # GOLDEN_STOREFRONT_TOKENS = "True"

[processes]
  web = "uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers 2 --no-access-log"
  outbox = "python manage.py process_outbox --loop"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = "off"
  auto_start_machines = true
  min_machines_running = 1
  processes = ["web"]

  [[http_service.checks]]
    grace_period = "30s"
    interval = "15s"
    timeout = "5s"
    method = "GET"
    path = "/erp/login/"
    [http_service.checks.headers]
      X-Forwarded-Proto = "https"
      Host = "<app>.fly.dev"

[[vm]]
  processes = ["web"]
  size = "shared-cpu-2x"
  memory = "2gb"

[[vm]]
  processes = ["outbox"]
  size = "shared-cpu-1x"
  memory = "512mb"
```

Notes:

- The `Dockerfile` does not set `DJANGO_SETTINGS_MODULE`; `fly.toml` must.
- `AUTO_MIGRATE=0`: the release command migrates, so the machines never do.
- Without `TRUSTED_PROXIES`, check `core.W114` warns and every visitor shares
  one login throttle bucket.
- The health check sends `X-Forwarded-Proto: https` (skipping the HTTPS
  redirect, so it sees the page's own status) and a `Host` from
  `ALLOWED_HOSTS`.
- `min_machines_running = 1` with auto-stop off: a cold start would cost the
  first visitor several seconds.
- With more than one web machine, add Redis (`fly redis create`, then set
  `REDIS_URL` as a secret) so throttles and the cache are shared.

## 4. Deploy

```sh
fly deploy -a <app> --remote-only --ha=false
```

Fly builds the image remotely, runs `migrate` in a release machine, then
replaces `web` and `outbox`. `--ha=false` keeps one machine per process. Static
files are collected into the image at build time; never mount a volume over
`/app/staticfiles`.

## 5. Hostname

```sh
fly certs add erp.example.com -a <app>
```

`certs add` prints the DNS records to create (an A record to the app's IPv4
and an AAAA record to its IPv6). If your DNS provider can proxy traffic, leave
these records unproxied: Fly terminates TLS and issues the certificate.
`fly certs check erp.example.com -a <app>` shows when it is issued. The
`.fly.dev` name keeps working.

## 6. First data and first login

Create the first staff account:

```sh
fly ssh console -a <app> --process-group web -C "python manage.py createsuperuser"
```

Then load your own data (the importers in [crm/importing.md](crm/importing.md),
or the back office). For a demo instance, `seed_demo_data` loads a demo
catalog, customers and chart of accounts. It resets the superuser password to
`admin` unless given one, so pass the password on stdin rather than running it
bare:

```sh
<password on stdin> | fly ssh console -a <app> --process-group web -C \
  "python manage.py shell -c 'import sys; from django.core.management import call_command; call_command(\"seed_demo_data\", admin_password=sys.stdin.readline().strip())'"
```

Add the due-task job from [deployment.md](deployment.md#crm_emit_due-every-5-minutes)
as a scheduled machine (`fly machine run … --schedule hourly` is the finest
schedule Fly offers, so for a five-minute cadence run a small cron process
group instead).

## 7. Storefront (optional)

The storefront is its own app, built from `storefront/` with
`storefront/Dockerfile`. Create `storefront/fly.toml`:

```toml
app = "<shop-app>"
primary_region = "<region>"

[build]
  dockerfile = "Dockerfile"
  [build.args]
    VITE_API_BASE_URL = "https://erp.example.com/api"

[env]
  VITE_API_BASE_URL = "https://erp.example.com/api"
  PREVIEW_ALLOWED_HOSTS = "shop.example.com,<shop-app>.fly.dev"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

  [[http_service.checks]]
    grace_period = "20s"
    interval = "30s"
    timeout = "5s"
    method = "GET"
    path = "/"
    [http_service.checks.headers]
      Host = "<shop-app>.fly.dev"

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
```

```sh
fly apps create <shop-app> --org <org>
fly deploy storefront -c storefront/fly.toml -a <shop-app> --remote-only --ha=false
fly certs add shop.example.com -a <shop-app>
```

- `VITE_API_BASE_URL` is a **build arg**. Vite bakes it into the browser
  bundle, so changing it means rebuilding. The same value goes in `[env]` for
  server-side loaders. Leave `VITE_DEMO_CATALOG` unset: a production build
  never shows the offline demo catalog.
- `PREVIEW_ALLOWED_HOSTS` lists the hosts `vite preview` answers; any other
  `Host` gets 403.
- **No API key, no secrets in the storefront.** A visitor's browser asks the
  back office for a one-hour token for the least-privilege `storefront-api`
  identity (catalog reads and guest order creation). A key baked into the
  bundle would be public. On the back-office app, set
  `GOLDEN_STOREFRONT_TOKENS = "True"` and list the shop's origins in
  `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS`. Without
  `GOLDEN_STOREFRONT_TOKENS`, token issuance falls back to the legacy `API_KEY`;
  with that unset too, the endpoint answers 500, checkout gets a 401, and the
  catalog still renders, so the fault is easy to miss.
- The image's files belong to the `bun` user because `vite preview` writes its
  bundled config under `node_modules/.vite-temp` at start.
- With auto-stop, the first visit after an idle period waits a couple of
  seconds for the machine to start.

## 8. Verify

```sh
curl -sI https://erp.example.com/erp/login/                                 # 200
curl -sI https://erp.example.com/static/assets/css/themes/golden.css         # 200 text/css
fly ssh console -a <app> --process-group web -C "python manage.py check --deploy"
fly ssh console -a <app> --process-group web -C "python manage.py smoke_cutover"
fly logs -a <app> --no-tail | grep -i traceback                              # nothing
```

`smoke_cutover` exits 0 when there is one Company, no unapplied migrations, a
staff login and a clean outbox ([deployment.md](deployment.md#operator-commands)).
