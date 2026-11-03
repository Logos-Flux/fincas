# Fincas

[![CI](https://github.com/Logos-Flux/fincas/actions/workflows/ci.yml/badge.svg)](https://github.com/Logos-Flux/fincas/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/fincas.svg)](https://pypi.org/project/fincas/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Django 6.0](https://img.shields.io/badge/django-6.0-green.svg)](https://www.djangoproject.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Fincas is an open-source commerce and back-office platform built on Django:
quotes and orders, invoicing and a double-entry ledger, inventory and
purchasing, a CRM, and a REST API for a headless storefront. You install it as
a Python package and run it as your own Django project, extended with plugins
instead of forks.

**Status:** actively maintained, beta. One company per instance. Python 3.12,
PostgreSQL 17, Django 6.0.

> **Import name:** the PyPI distribution is `fincas`, but the Python package is
> `golden_erp` (the project's earlier name), and settings keep the `GOLDEN_*`
> prefix. You write `pip install fincas`, then
> `from golden_erp.settings.prod import *` and `include("golden_erp.urls")`.
> Both names stay as they are so existing deployments and plugins keep working.

## Install

### From PyPI (to run Fincas)

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install "fincas[prod]"
```

Fincas is a library: your deployment is a small Django project with a
`config/settings.py` that starts from `golden_erp.settings.prod` and a
`config/urls.py` that mounts `golden_erp.urls`.
[docs/getting-started.md](docs/getting-started.md) takes you from an empty
directory to a first login; [docs/packaging.md](docs/packaging.md) covers the
settings overlay and version pinning.

WeasyPrint renders the PDFs and needs Cairo and Pango from your OS
(`libpango-1.0-0 libpangocairo-1.0-0 libcairo2` on Debian and Ubuntu).

### From source (to work on Fincas)

```bash
git clone https://github.com/Logos-Flux/fincas.git
cd fincas
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements/dev.txt
make migrate          # SQLite in the working directory
make seed             # demo company, catalog, customers, chart of accounts
make run              # http://localhost:8000/erp/
```

`make seed` creates the superuser `admin` with the password `admin`. That is
for a local machine only; pass `--admin-password` to `manage.py seed_demo_data`
anywhere else. The API docs are at `http://localhost:8000/api/docs/`.

[CONTRIBUTING.md](CONTRIBUTING.md) has the full development setup, including
Docker Compose and the storefront.

## What's in it

- **Sales:** quotes with revisions, sales orders with shipment groups and
  drop-ship lines, sales invoices, payments through pluggable gateways
  (Stripe and manual included), coupon codes, price lists with customer-tier
  breaks, tax rates.
- **Operations:** multi-warehouse stock, purchase orders and receiving, pick
  tickets, carrier rates and labels (EasyPost), PDF quotes, orders, invoices
  and purchase orders (WeasyPrint).
- **Accounting:** chart of accounts, fiscal periods, journal entries posted
  from documents by configurable rules, trial balance checks.
- **CRM:** one party record per person or organization (merge, undo, GDPR-style
  erasure), leads, pipelines and engagements with stage history, tasks,
  communications, custom fields, imports from another CRM's export, and
  adapters for Aircall, Gmail, ClickUp, Klaviyo and Google Ads.
- **API and storefront:** a REST API with an OpenAPI 3 schema (Swagger UI and
  Redoc included), a least-privilege storefront identity, a customer portal,
  and a Qwik City storefront in `storefront/`.
- **Extending it:** plugins add navigation, dashboard widgets, payment
  gateways, carriers, CRM adapters, outbox consumers and URLs; HMAC-signed
  outbound webhooks; a label layer to rename nouns in the staff UI; generated,
  WCAG AA-checked back-office themes.
- **Assistant:** an optional AI chat drawer for staff, routed through
  `llmhub`: direct provider keys (Anthropic or any OpenAI-compatible endpoint)
  or your own gateway.

## One company per instance

Each Fincas instance serves one company: one database, one `core.Company` row,
one deployment. Another company gets its own instance. System check
`core.E070` refuses a second Company
([docs/deployment.md](docs/deployment.md#one-instance-per-company)).

## Plugins

Fincas is extended without editing core files. A plugin is a class listed in
`GOLDEN_PLUGINS`:

```python
# hello_fincas/plugin.py
from core.plugins import BasePlugin


class HelloPlugin(BasePlugin):
    name = "hello"
    verbose_name = "Hello, Fincas"
    version = "0.1.0"

    def get_nav_items(self):
        return [{
            "type": "link",
            "label": "Hello",
            "section": "analytics",
            "url_name": "erp-dashboard",
            "icon": "fas fa-chart-line",
        }]
```

```python
# config/settings.py
GOLDEN_PLUGINS = ["hello_fincas.plugin.HelloPlugin"]
```

[docs/plugins.md](docs/plugins.md) covers every hook, lifecycle signals, the
outbox, plugin URLs and testing a plugin.

## LLM features

Every LLM call goes through `llmhub` and is configured on the staff-only
`/erp/llm/` page or by environment: direct provider keys (`ANTHROPIC_API_KEY`,
or `OPENAI_BASE_URL` and `OPENAI_API_KEY` for any OpenAI-compatible endpoint),
or an OpenAI-compatible gateway (`LLMHUB_MODE=gateway`,
`LLMHUB_GATEWAY_BASE_URL`). `python manage.py llmhub_check` makes one small
test call per configured purpose (`--no-live` checks resolution only).

Set `LLMHUB_ALLOWED_BASE_URLS` in production. It is empty by default, and while
it is empty any staff user can point LLM traffic, including ERP data in
prompts, at a host of their choosing from the settings page.

## Documentation

The full index is [docs/README.md](docs/README.md). Start with:

- [Getting started](docs/getting-started.md): from `pip install` to a first login
- [Architecture](docs/architecture.md): the apps, the plugin seam, the storefront contract
- [Deployment](docs/deployment.md): Docker Compose, systemd and Fly.io, required secrets
- [Upgrading](docs/upgrading.md): versioning, deprecations and the migration contract
- [Plugins](docs/plugins.md) and [Webhooks](docs/webhooks.md)
- [Changelog](CHANGELOG.md)

## Contributing and security

Bug reports, fixes and documentation are welcome: see
[CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md).
Report security issues privately, as described in [SECURITY.md](SECURITY.md),
not in a public issue.

## License

[MIT](LICENSE). Logo and brand assets: `fincas-grid-brand/`.
