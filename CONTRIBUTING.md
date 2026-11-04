# Contributing to Fincas

Thanks for your interest in Fincas. Bug reports, fixes, documentation and new
plugins are all welcome. This page gets you from a fresh clone to a passing
test run, and explains how a pull request moves through review.

By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Two names

The product, the repository and the PyPI distribution are **Fincas**. The
Python package is **`golden_erp`** (with the Django apps beside it as
top-level packages), and settings are prefixed `GOLDEN_*`. Those are the
project's earlier name and stay as they are, so deployments and plugins written
against them keep working. Don't rename them in a pull request.

## Development setup

**Prerequisites**

- Python 3.12
- Cairo and Pango for PDF rendering (Debian and Ubuntu:
  `sudo apt install libpango-1.0-0 libpangocairo-1.0-0 libcairo2`)
- Optional: PostgreSQL 17 (production and CI run on it; the quick loop uses
  SQLite), Docker with Compose, and [bun](https://bun.sh) for the storefront

**Fresh clone to a running back office** (about a minute on a warm pip cache):

```bash
git clone https://github.com/<you>/fincas.git     # your fork of Logos-Flux/fincas
cd fincas
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements/dev.txt
make migrate          # creates db.sqlite3 in the working directory
make seed             # demo company, catalog, customers, 90 days of orders and books
make run              # http://localhost:8000/erp/
```

Sign in at `http://localhost:8000/erp/` as `admin` / `admin`: `make seed` sets
that password for local development. Pass `--admin-password` to
`manage.py seed_demo_data` on anything reachable from a network. The API docs
are at `http://localhost:8000/api/docs/`.

No `.env` is needed for this loop. To use Postgres instead of SQLite, set
`DATABASE_URL=postgres://user:password@localhost:5432/fincas_dev` in the
environment or in a `.env` file (`.env.example` lists every variable the
settings read; copy only what you need, since its `DATABASE_URL` is a
placeholder).

**Docker Compose** (Postgres, Valkey and the web container):

```bash
make up
make compose-migrate
make compose-seed
```

**Storefront** (Qwik City, in `storefront/`):

```bash
make storefront-install
make storefront-dev    # talks to the API at http://localhost:8000/api
```

`make help` lists every target.

## Running the checks

```bash
make test           # the suite on SQLite in memory
make lint           # ruff
make check          # system checks, missing migrations
make tripwire       # rejects company-specific names in tracked files
make package-check  # builds the wheel, installs it in a fresh venv, runs a project on it
```

CI runs the same suite again on PostgreSQL 17, migrates an empty database,
replays the upgrade from earlier releases, and builds and installs the wheel.
To run the suite on Postgres locally, point `TEST_DATABASE_URL` at a server
where your role can create databases:

```bash
TEST_DATABASE_URL=postgres://user:password@localhost:5432/fincas_test \
  python manage.py test --noinput --parallel 4 --settings=config.settings.test
```

The test runner refuses any network connection other than loopback and the
database and cache hosts. Mock HTTP clients at the call site.

## Guidelines

1. **Nothing company-specific in core.** Core apps stay industry-neutral: no
   brand, vertical vocabulary, hostnames or customer data. Industry features
   belong in plugins (a package of your own, see
   [docs/plugins.md](docs/plugins.md)). `make tripwire` runs before every
   commit and in CI.
2. **Settings-driven registries validate when loaded.** `ERP_THEMES`,
   `ERP_NAV`, `GOLDEN_ROLES`, `GOLDEN_PLUGINS` and friends raise
   `ImproperlyConfigured` (or fail a system check) on bad config instead of
   dropping an entry silently. Follow that pattern for anything a deployment
   would need to change.
3. **Migrations are generated** with
   `python manage.py makemigrations --settings=config.settings.dev` (the test
   settings disable migrations), never written by hand. A data migration uses
   only historical models (`apps.get_model`). Say in the PR whether each new
   migration reverses ([docs/upgrading.md](docs/upgrading.md)).
4. **Templates and static files** under `golden_erp/` and the apps can be
   overridden by deployments, so a changed template is a behavior change:
   mention it in the PR so it reaches the release notes.
5. **Tests go with the change.** A bug fix comes with a test that fails without
   it.
6. **US English** in user-visible text (color, canceled, organization). Money
   we pay is a *cost*; money a customer pays is a *price*.
7. **Deprecate before removing.** A public setting, API field or import path
   warns for at least one minor release before it goes
   ([docs/upgrading.md](docs/upgrading.md)).

## Pull requests

1. Fork `Logos-Flux/fincas` and branch from `main`
   (`git checkout -b fix/short-description`).
2. Make the change with its tests and docs. Run `make check test lint tripwire`.
3. Commit with a clear subject (`fix: …`, `feat: …`, `docs: …`) and open the
   pull request against `main`. The template asks what changed, how you tested
   it and whether it adds a migration.

**How a pull request lands.** The maintainers develop Fincas in a separate
repository and publish each release to `Logos-Flux/fincas` as a single commit.
Your pull request is reviewed and discussed here, on the public repository, and
CI runs on it as usual. Once it is approved, a maintainer applies it to the
development repository with your authorship kept on the commit, it ships in the
next release, and the release notes credit you. The pull request is then
closed with a link to the release that contains it, rather than merged with
the merge button. Nothing about your change is lost on the way.

Larger changes (a new app, a new public setting, a schema change across apps):
open an issue first so we can agree on the shape before you write the code.

**Security issues** go through private reporting, never a public issue or pull
request: see [SECURITY.md](SECURITY.md).

## Where things are documented

- [README.md](README.md): what Fincas is and how to install it
- [docs/README.md](docs/README.md): the index of the documentation
- [docs/architecture.md](docs/architecture.md): the apps, the plugin seam, the storefront contract
- [docs/release-testing.md](docs/release-testing.md): the back-office journeys (`scripts/harness/run.sh`)
- [docs/releases/](docs/releases/) and [CHANGELOG.md](CHANGELOG.md): release notes and upgrade steps
