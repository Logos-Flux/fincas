# Changelog

All notable changes to Fincas are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Fincas uses
[semantic versioning](https://semver.org/) as described in
[docs/upgrading.md](docs/upgrading.md). Each release also has full notes in
[docs/releases/](docs/releases/): what to do before upgrading, behavior
changes, known limitations and the migration list.

## 0.4.2

Guest checkout end to end. Full notes: [docs/releases/v0.4.2.md](docs/releases/v0.4.2.md).

### Added

- Storefront guest checkout copies the cart lines onto the sales order
  (OP #3426). The order create takes `lines: [{variant, quantity}]`; the
  server prices each line from the variant's public price, totals the order,
  puts the lines in one shipment group, and checks stock without reserving it
  (preorder and backorder variants may be ordered without stock). Refusals are
  400 with stable codes, per line where it applies; nothing is created and no
  order number is used. The storefront shows the server's priced lines and
  totals, and marks refused cart lines with a way to fix them.
- Guest checkout is rate-limited per client IP: `GOLDEN_GUEST_ORDER_RATE`,
  default `10/minute` (DRF rate syntax; empty disables it), answering 429.
- Guest checkout honors an `Idempotency-Key` header (OP #3431), and the
  storefront sends one per checkout, so a retry after a timeout returns the
  order that was already placed (200) instead of creating a second one. The
  same key with any different stored detail (email, lines, address, PO,
  notes, payment method, coupon) is refused with 409 `idempotency_key_reused`
  rather than answered with the old order; a malformed key with 400
  `idempotency_key_invalid`; a same-key submit that waits more than 5 s for
  the first one with 503 `checkout_in_progress`. `CORS_ALLOW_HEADERS` now includes
  `idempotency-key`, and system check `core.W115` warns when a deployment's
  own `CORS_ALLOW_HEADERS` leaves it out (cross-origin checkout would fail).

### Changed

- **Breaking for custom storefronts that send `Idempotency-Key`:** it was
  ignored on guest checkout before. Now a key must be 16-200 characters of
  `A-Za-z0-9._:-` (else 400), and reusing one key for different checkouts
  within 24 hours is refused (409). Send one random key per checkout.
- **Breaking for custom storefronts:** a guest checkout without `lines` is
  refused (`lines_required`); the old `items` key is ignored. The guest
  response is now a narrow confirmation (priced lines, totals,
  `total_amount`) instead of the full staff order payload, which exposed
  costs, metadata and the buyer's IP address to the browser. The 503 for an
  instance without exactly one Company carries `code:
  guest_checkout_unavailable`.
- Guest orders no longer copy a client-sent `metadata` object; only the
  buyer fields (name, company, shipping address, payment method, coupon
  code) are kept, as strings of at most 1,000 characters. The order's
  `ip_address` is the client past `TRUSTED_PROXIES`, not the proxy.
- The storefront no longer says "no order was placed" after a timeout,
  network error or 5xx on checkout: the order may exist, and it tells the
  shopper that submitting the same cart again is safe.
- Guest checkout ignores a body `external_id`; only the `Idempotency-Key`
  header dedups a guest order. A non-string `email` is refused with 400
  instead of a server error.

### Fixed

- The `order.placed` webhook reported `total_amount` as `"0.00"` for every
  order: it read a field `Order` doesn't have. It now carries the order's
  `grand_total` (OP #3435), always with two decimal places. The key and
  payload version are unchanged. A
  guest checkout emits after its lines are priced, so its total is the real
  one; an order created header-first through the staff API emits on that
  first save, before lines exist, and still reports the header's total.
- Storefront: the disabled product-card button's accessible name now starts
  with its visible text ("Sold out: …", "Ask about …"), as axe-core 4.14
  requires.
- Storefront: removing the last cart line, or placing an order, no longer
  brings the old cart back from browser storage.

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
