# Fincas documentation

New to Fincas? Read [getting-started.md](getting-started.md) first, then
[architecture.md](architecture.md). The project overview is the
[README](../README.md).

## Install and run

| Document | What it covers |
| :--- | :--- |
| [getting-started.md](getting-started.md) | from `pip install fincas` to a first login, in a project of your own |
| [packaging.md](packaging.md) | what the wheel ships, installing from PyPI, version pinning, the settings overlay, overriding templates |
| [deployment.md](deployment.md) | production: environment, required secrets, Docker Compose, systemd, reverse proxy, database, scheduled jobs, operator commands |
| [deploy-fly.md](deploy-fly.md) | a complete Fly.io deployment, with the storefront as a second app |
| [upgrading.md](upgrading.md) | versioning, the deprecation policy, the migration contract, how to upgrade |
| [releases/](releases/) | per-release notes: what to do before upgrading, behavior changes, migrations |

## Understand and extend

| Document | What it covers |
| :--- | :--- |
| [architecture.md](architecture.md) | the app map, settings layers, the plugin seam, the storefront contract, API authentication |
| [plugins.md](plugins.md) | writing a plugin: hooks, lifecycle signals, outbox consumers, URLs, testing |
| [webhooks.md](webhooks.md) | outbound webhooks: events, payload versions, signatures and verification |
| [fields.md](fields.md) | custom fields in the `metadata` sidecar |
| [labels.md](labels.md) | renaming nouns (Customer, Engagement, …) in the staff UI |
| [roles.md](roles.md) | staff roles, the staff flag, and how they stay in step |
| [identity-unification.md](identity-unification.md) | design notes: how staff, customers and service accounts authenticate |
| [formatting.md](formatting.md) | how money and dates are formatted across the back office, PDFs, CSV and the API |
| [storefront-seo.md](storefront-seo.md) | storefront URLs, metadata and structured data for categories, brands and products |

## CRM

| Document | What it covers |
| :--- | :--- |
| [crm/importing.md](crm/importing.md) | importing from another CRM: the canonical format, dry runs, review, undo |
| [crm/adapters.md](crm/adapters.md) | connected accounts: Aircall, Gmail, ClickUp, Klaviyo, Google Ads, keys and erasure |
| [crm/wiring.md](crm/wiring.md) | wiring the adapters in a deployment: secrets, worker, scheduler, webhooks, first week |
| [crm/fields/](crm/fields/README.md) | field inventories for each provider and how to decide where a field goes |

## Contributing

| Document | What it covers |
| :--- | :--- |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | development setup, checks, how a pull request lands |
| [release-testing.md](release-testing.md) | the back-office journey harness (`scripts/harness/run.sh`) against any Postgres 17 |
| [SECURITY.md](../SECURITY.md) | reporting a vulnerability privately |
| [CHANGELOG.md](../CHANGELOG.md) | what changed in each release |
