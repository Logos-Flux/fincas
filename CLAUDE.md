# CLAUDE.md — Fincas

Conventions for an AI coding assistant working in this repository. The human
version is `CONTRIBUTING.md`; the docs index is `docs/README.md`.

## What this is

Fincas is a Django commerce and back-office platform (orders, quotes, CRM,
accounting, inventory, purchasing, a storefront API) that runs **one company per
instance**. The PyPI distribution is `fincas`; the Python import name is
**`golden_erp`** (the project's earlier name), with the Django apps (`core`,
`orders`, `crm`, …) as top-level packages beside it. Settings are prefixed
`GOLDEN_*`. Do not rename any of these: deployments import and configure them by
those names.

## Layout

- `golden_erp/`: base and production settings (`settings/base.py`,
  `settings/prod.py`), `urls.py`, and the project-level `templates/` and
  `static/`. Settings find them through `golden_erp.paths`, never `BASE_DIR`.
- `config/`: this repository's own runnable project shell (`manage.py`
  settings, asgi). It is never shipped; put anything a deployment needs in
  `golden_erp/` or an app.
- `pyproject.toml` has an explicit package include list: a new app must be added
  there and to `INSTALLED_APPS`, or it will not ship.
- `storefront/`: the Qwik City storefront (bun). It talks to the API only.
- `docs/`: the documentation (`docs/README.md` is the index).

## Running things

- Python 3.12, Django 6.0, PostgreSQL 17 in production, SQLite for the quick loop.
- `make test` runs the suite on SQLite. On Postgres:
  `TEST_DATABASE_URL=postgres://… python manage.py test --noinput --parallel 4 --settings=config.settings.test`.
- `make lint` (ruff), `make check` (system checks, `makemigrations --check`),
  `make tripwire`, `make package-check` (builds the wheel, installs it in a fresh
  venv and runs a downstream-style project on it).
- Migrations: `python manage.py makemigrations --settings=config.settings.dev`.
  The test settings disable migrations, so `makemigrations --check` under them
  always reports clean.
- The test runner refuses non-loopback network connections; mock HTTP clients at
  the call site.
- Journeys: `scripts/harness/run.sh` drives the back office end to end against any
  Postgres 17 (`docs/release-testing.md`).

## Rules

- **Settings-driven registries validate when loaded.** `ERP_THEMES`, `ERP_NAV`,
  `GOLDEN_ROLES`, `GOLDEN_PLUGINS` and friends raise `ImproperlyConfigured` (or
  fail a system check) on a bad entry instead of dropping it. Follow that
  pattern for anything a deployment would need to change.
- **Nothing company-specific in core**: no brand, host, customer or industry
  vocabulary. Industry features are plugins in their own package
  (`docs/plugins.md`). `scripts/tripwire.sh` rejects a list of forbidden names
  before every commit and in CI; it does not catch everything, so don't add
  examples that name real companies.
- **Migrations are generated, never hand-written.** Data migrations use only
  historical models (`apps.get_model`, passed down to helpers). Say whether a new
  migration reverses (`docs/upgrading.md`).
- **Deprecate before removing** a setting, API field or import path: it warns for
  at least one minor release (`docs/upgrading.md`).
- Every model has a UUID default primary key: "is this a create?" is
  `instance._state.adding`, never `if not instance.pk`.
- Templates and static files under `golden_erp/` and the apps are overridable by
  deployments, so a changed template is a documented behavior change.
- No secrets in the tree, and no credential-shaped literals in tests (build fake
  keys at runtime). `.env.example` lists every variable; `manage.py
  generate_secrets` prints a fresh set of required keys.
- User-visible text is US English. Money is "cost" when we pay and "price" when a
  customer pays.
- Note behavior changes for the release notes (`docs/releases/`) and
  `CHANGELOG.md`.
