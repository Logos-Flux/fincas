# Packaging and installing Fincas

Fincas builds as one wheel (distribution `fincas`, setuptools) that your own
Django project installs and runs on. The import name is `golden_erp` (with the
Django apps beside it as top-level packages); the distribution and the product
are called Fincas. Proof that the wheel works on its own: `make package-check`
(`scripts/check_package_install.sh`, also run in CI).

## Installing from PyPI

```bash
pip install "fincas==0.4.3"            # runtime
pip install "fincas[prod]==0.4.3"      # plus uvicorn, gunicorn, sentry-sdk
```

**Pin an exact version.** Fincas ships migrations with every release, and an
upgrade is a deliberate step (back up, install, `migrate`, restart), not
something a resolver should do for you. Put the pin in your requirements file
(`fincas[prod]==X.Y.Z`) and, if you use a lock file, lock it with hashes
(`pip-compile --generate-hashes`, `uv pip compile --generate-hashes`).
[upgrading.md](upgrading.md) has the version policy and what each kind of
release may change; read the release notes in [releases/](releases/) before
moving the pin.

Check what is installed:

```bash
python -c "import importlib.metadata as m; print(m.version('fincas'))"
```

To try an unreleased change, install from a git ref instead:

```bash
pip install "fincas @ git+https://github.com/Logos-Flux/fincas.git@main"
```

## What the wheel ships

| Top-level package | What it is |
|---|---|
| `golden_erp` | `settings/base.py` (base settings), `settings/prod.py` (base + production hardening), `urls.py` (URL patterns), `paths.py`, and the project-level `templates/` and `static/` |
| `core`, `products`, `orders`, `accounting`, … (22 apps, incl. `erp_assist`) | The Django apps, with their `templates/`, `static/`, `fixtures/`, `data/` and migrations |
| `addons` | The plugin add-ons, with their fixtures and tests: the CRM adapters a downstream lists in `GOLDEN_PLUGINS` (`golden_erp.addons.aircall`, `golden_erp.addons.gmail`, `golden_erp.addons.clickup`, `golden_erp.addons.klaviyo`, `golden_erp.addons.google_ads`, `golden_erp.addons.hubspot_export`; dotted paths in [crm/adapters.md](crm/adapters.md)), plus `golden_erp.addons.fixture_crm` and `golden_erp.addons.example_audit_notifier`, which ship only because the test suite uses them and must **never** be listed by a deployment |

Not shipped: `config/` (the repository's own runnable project shell:
`manage.py` settings, wsgi/asgi), `storefront/`, `scripts/`, `docs/`,
`requirements/`. Your project has its own `config` package, so the
repository's must never be importable under that name.

**Your project must not have its own top-level `addons` package.** Before
0.4.1 the shipped add-ons were a top-level `addons` package; they now live in
`golden_erp.addons`, and the wheel ships a top-level `addons` only as a
deprecated alias through 0.5.x (it warns, and resolves `addons.<x>` to the same
module objects as `golden_erp.addons.<x>`; removed in 0.6.0). Two packages of
one name shadow each other: whichever is first on `sys.path` wins, and the
other's plugins fail to import. Put your plugins in a package with your
project's own name.

The package check enables the shipped adapters in its test project's
`GOLDEN_PLUGINS`, imports each `golden_erp.addons.<x>.plugin` from the venv (never the
checkout), asserts the registry lists their providers, and that an adapter
fixture (`golden_erp/addons/clickup/fixtures/erase/case.json`) was installed.

`[project].dependencies` mirrors `requirements/base.txt`, so the wheel alone is
a complete runtime install. Server extras: `fincas[prod]` (uvicorn,
gunicorn, sentry-sdk). The include list is explicit in `pyproject.toml`; a
new app package must be added there or it will not ship.

## Downstream settings and URLs

A *downstream* here is your own Django project that installs Fincas.

A production overlay starts from `golden_erp.settings.prod`: the base plus
`DATABASES` from `DATABASE_URL`, `SECURE_*`/HSTS/secure cookies and
`SECURE_PROXY_SSL_HEADER` (TLS terminated at a proxy), Mailgun via anymail
(console backend while a demo login is configured), and Sentry when
`SENTRY_DSN` is set (needs the `[prod]` extra; events from `manage.py shell`
are dropped). Use `golden_erp.settings.base` for development or tests and set
`DATABASES` yourself, as the package check does.

```python
# config/settings.py
from pathlib import Path

from golden_erp.settings.prod import *  # noqa: F401,F403
from core.plugin_apps import compose_installed_apps

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_ROOT = BASE_DIR / "staticfiles"
ROOT_URLCONF = "config.urls"
GOLDEN_PLUGIN_APPS = ["my_app"]
INSTALLED_APPS = compose_installed_apps(INSTALLED_APPS, GOLDEN_PLUGIN_APPS)
```

```python
# config/urls.py
from django.urls import include, path

urlpatterns = [path("", include("golden_erp.urls"))]
```

Notes:

- The base settings read the environment when imported and **fail fast
  without `DJANGO_SECRET_KEY`** unless `DEBUG` is set in the environment.
  They do not load a `.env` file; call `load_dotenv()` before the import if
  you want one.
- `BASE_DIR` defaults to the working directory (or `$GOLDEN_BASE_DIR`). Only
  `STATIC_ROOT` derives from it; set both in the overlay.
- `base` sets no `DATABASES`; neither module sets `WSGI_APPLICATION`.
  `prod` reads `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SECURE_SSL_REDIRECT`,
  `SECURE_HSTS_SECONDS`, `DB_SSL_REQUIRE`, `MAILGUN_*` and `DEFAULT_FROM_EMAIL`
  from the environment.
- Most settings have a default that applies only when the variable is
  **absent**; an empty value is not the same (`SECURE_SSL_REDIRECT=` turns the
  redirect off, `SECURE_HSTS_SECONDS=` fails at import). The repository's
  `docker-compose.prod.yml` therefore passes `.env` whole (`env_file`) rather
  than a `${NAME:-}` list; do the same in your own compose file.
- Mount `golden_erp.urls` at the root: `LOGIN_URL` and the back-office
  redirects use absolute `/erp/` paths.

## How templates and static are found

`golden_erp/paths.py` resolves `TEMPLATES_DIR` and `STATIC_DIR` from the
package's own location. The base settings put them in `TEMPLATES[0]["DIRS"]`
and `STATICFILES_DIRS`, so they resolve the same way in a checkout and in
site-packages, independent of `BASE_DIR`. App templates/static come from
`APP_DIRS` and the app-directories finder as usual.

To override a Fincas template or asset, put your directory **first**:

```python
TEMPLATES[0]["DIRS"].insert(0, BASE_DIR / "templates")
STATICFILES_DIRS.insert(0, BASE_DIR / "static")
```

`collectstatic` copies the packaged static files into your `STATIC_ROOT`.

## Repo layout this implies

- Project-level templates/static live in `golden_erp/templates` and
  `golden_erp/static`, not the repository root. The ERP
  theme generator writes `golden_erp/static/assets/css/themes/<name>.css`.
- Base and production settings live in `golden_erp/settings/{base,prod}.py`;
  `config/settings/base.py` imports the base and adds only the repository's
  project-local values (repo `BASE_DIR`, `STATIC_ROOT`, `ROOT_URLCONF`,
  `WSGI_APPLICATION`, `.env` loading), and `config/settings/prod.py` layers
  the packaged prod on top and re-applies those values.
- URL patterns live in `golden_erp/urls.py`; `config/urls.py` re-exports them.
