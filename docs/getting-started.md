# Getting started: from `pip install` to your first invoice

Five minutes on a machine with Python and Docker: install Fincas from PyPI into
a fresh virtual environment, wrap it in a small Django project, point it at
PostgreSQL 17, load the demo data, sign in to the back office and raise an
invoice.

Every command on this page, except the system packages and the clean-up, is
run as written by `scripts/release/r12_fresh_install.sh` (release journey
R12) in an empty directory before each release, timed, through to the
invoice; only the ports, the container name and the package source are
swapped for its run. If a step
here does not work as written, that is a bug: please open an issue.

## What you need

- **Python 3.12 or later** (CI tests 3.12) with `venv`.
- **Docker**, for the database. Fincas is built and tested on PostgreSQL 17;
  every deployment runs it.
- **WeasyPrint's system libraries** (Pango), which Fincas uses to render
  invoice and quote PDFs.

On Debian or Ubuntu, this installs the Python and WeasyPrint parts:

```bash r12:skip
sudo apt-get install -y python3-venv libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 shared-mime-info fonts-dejavu-core
```

On macOS: `brew install python pango`. Other systems: WeasyPrint's
installation guide lists the packages.

## 1. Install Fincas into a fresh virtual environment

The distribution is called `fincas`; the Python package it installs is
`golden_erp` (plus the Django apps `core`, `orders`, `accounting`, ...).

```bash
mkdir fincas-trial && cd fincas-trial
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install fincas
```

## 2. Create a project around it

Fincas ships as a set of Django apps plus base settings and URLs. Your
deployment is an ordinary Django project that imports them. Start one with
Django's own template, then replace its settings and URLs:

```bash
django-admin startproject config .
```

```bash
cat > config/settings.py <<'EOF'
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Before the import below: Fincas' base settings read the environment.
load_dotenv(BASE_DIR / ".env")

import dj_database_url  # noqa: E402

from golden_erp.settings.base import *  # noqa: E402,F401,F403
from core.plugin_apps import compose_installed_apps  # noqa: E402

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
DATABASES = {"default": dj_database_url.config(conn_max_age=60)}
STATIC_ROOT = BASE_DIR / "staticfiles"
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
# Refuse to migrate without the deployment's own keys (step 4), as production does.
GOLDEN_REQUIRE_DEPLOY_SECRETS = True
# Print outgoing mail in the server's console instead of sending it.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Your own Django apps and Fincas plugins go here (docs/plugins.md).
GOLDEN_PLUGIN_APPS = []
INSTALLED_APPS = compose_installed_apps(INSTALLED_APPS, GOLDEN_PLUGIN_APPS)
EOF
```

```bash
cat > config/urls.py <<'EOF'
from django.urls import include, path

urlpatterns = [path("", include("golden_erp.urls"))]
EOF
```

Mount `golden_erp.urls` at the root: the back office lives under `/erp/`
and the API under `/api/`.

This is a local trial: plain HTTP, `DEBUG` off, mail to the console. For a
server, start from `golden_erp.settings.prod` instead (HTTPS-only cookies, HSTS,
`DATABASES` from `DATABASE_URL`, Mailgun) and follow `docs/deployment.md`.

## 3. Start PostgreSQL 17

```bash
docker run -d --name fincas-postgres -e POSTGRES_USER=fincas -e POSTGRES_PASSWORD=fincas -e POSTGRES_DB=fincas -p 127.0.0.1:5432:5432 postgres:17
until docker exec fincas-postgres pg_isready -q -h 127.0.0.1 -U fincas; do sleep 1; done
```

The port is bound to loopback only. If something already listens on 5432,
change the first `5432` here and the port in `DATABASE_URL` below.

**SQLite works for a quick look** (`DATABASE_URL=sqlite:///db.sqlite3` in
step 4, and skip this step), and the demo data loads on it. It is not a
supported deployment: SQLite ignores column lengths and several constraints
that Postgres enforces, so something that works on SQLite can still fail in
production.

## 4. Set the database and the deployment's secrets

Settings come from the environment; the settings file above loads `.env`
from the project directory. Fincas needs four secrets of its own (the Django
secret key, the API token signing key and two encryption keys), and
`generate_secrets` prints a fresh set:

```bash
echo "DATABASE_URL=postgres://fincas:fincas@127.0.0.1:5432/fincas" > .env
DEBUG=1 python manage.py generate_secrets >> .env
chmod 600 .env
```

`DEBUG=1` lets the settings load once without `DJANGO_SECRET_KEY`, which does
not exist until this command has printed it. Keep `.env` out of version
control. Changing these keys later has side effects (a new
`DJANGO_SECRET_KEY` signs everyone out, a new `GOLDEN_SECRET_KEYS` cannot read
credentials stored under the old one): `docs/deployment.md`, "Required
secrets".

## 5. Create the tables and load the demo data

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py seed_demo_data
```

`seed_demo_data` creates one company, a warehouse, products with stock,
customers, suppliers and CRM records, and a superuser. It prints the
superuser's credentials at the end: `admin` / `admin` unless you pass
`--admin-password`. Change that password before anyone else can reach the
server (`python manage.py changepassword admin`).

## 6. Run it and sign in

```bash r12:background
python manage.py runserver 8000
```

Open <http://localhost:8000/erp/> and sign in as `admin` with the password
from step 5. Only staff accounts can use `/erp/`.

## 7. Your first invoice

In the back office:

1. Open **Sales Orders** in the sidebar and click **New Sales Order**.
2. In **Customer**, type `Acme` and pick **Acme Industrial Technologies**
   from the list.
3. In the first line, type `ACT-500W` in the item search and pick the
   product; set the quantity to `2`.
4. Click **Save & Continue**. The order gets its number (`SO-...`) and stays
   open in the editor.
5. Click **Create Invoice** and confirm. You land on the invoice's page, with
   its number (`INV-...`), the total, a print preview and the PDF.

From the invoice page you can record a payment against it. **Email Invoice**
on the order sends the PDF with a payment link; in this trial the message is
printed in the server's console.

## Stop or start over

```bash r12:skip
docker rm -f fincas-postgres
```

That deletes the database with the container. To keep it between sessions,
use `docker stop fincas-postgres` and `docker start fincas-postgres`.

## Next

- `docs/deployment.md`: Docker Compose, systemd and Fly, required secrets,
  backups.
- `docs/packaging.md`: what the package contains, overriding templates and
  static files, pinning a version.
- `docs/plugins.md`: adding your own apps and adapters.
- `docs/architecture.md`: how the apps fit together.
