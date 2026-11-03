# Changelog

All notable changes to Fincas are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Fincas uses
[semantic versioning](https://semver.org/) as described in
[docs/upgrading.md](docs/upgrading.md). Each release also has full notes in
[docs/releases/](docs/releases/): what to do before upgrading, behavior
changes, known limitations and the migration list.

## 0.4.1

The first public release. Full notes: [docs/releases/v0.4.1.md](docs/releases/v0.4.1.md).

### Added

- First public release, published as open source under the MIT license on
  GitHub (`Logos-Flux/fincas`) and PyPI (`fincas`).
- Public documentation set: getting started, architecture, deployment
  (Docker Compose, systemd, Fly.io), upgrading and the deprecation policy, the
  CRM adapter wiring guide, and a documentation index.
- `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue and pull request templates.

### Changed

- The distribution is published as `fincas`. The Python import name stays
  `golden_erp`, and every `GOLDEN_*` setting keeps its name.
- The bundled plugins moved from the top-level `addons` package to
  `golden_erp.addons`. Update `GOLDEN_PLUGINS` entries such as
  `addons.clickup.plugin.ClickUpPlugin` to
  `golden_erp.addons.clickup.plugin.ClickUpPlugin`.
- Test fixtures and demo data are brand-neutral.
- The ERP assistant's HTTP client reads `GOLDEN_ERP_API_KEY` and
  `GOLDEN_ERP_BASE`. Older deployment-specific variable names are no longer
  read: rename them before upgrading.
- New setting `GOLDEN_DEFAULT_COMPANY_NAME` (also read from the environment)
  names the company the order and quote editors preselect. Without it, they
  preselect only when exactly one company exists.
- Document emails are headed with the configured company name, and the
  invoice's order reference reads "Sales Order #".
- A storefront theme id that named a real company was renamed
  (`solar-industrial`); a stored old id falls back to the default theme.

### Fixed

- Order confirmation and shipping notification emails showed "Commerce" in
  their header instead of the configured company name; every document email
  now uses `ERP_BRAND["company_name"]`.

### Deprecated

- Importing the bundled plugins from the top-level `addons` package. A shim
  keeps the old paths working, with a `DeprecationWarning`, until they are
  removed in 0.6.0 under the
  [deprecation policy](docs/upgrading.md#deprecation-policy); switch to
  `golden_erp.addons.*`.

## 0.4.0

Party-based CRM (one record per person or organization, merge and erasure),
engagements and tasks, the CRM importer and adapters, the outbox, service
accounts for API callers, required production secrets (`core.E060`), and one
company per instance (`core.E070`). Full notes, including the steps to take
before upgrading: [docs/releases/v0.4.0.md](docs/releases/v0.4.0.md).
