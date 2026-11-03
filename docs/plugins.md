# Fincas Plugin Architecture

Fincas features an extensible plugin system allowing developers to build vertical-specific extensions, custom payment processors, shipping carriers, and dashboard widgets without modifying core code.

---

## 0. Hello world

The smallest plugin is a class and one settings line. Put it in your own
package (here `hello_fincas/`, anywhere on the Python path of your project):

```python
# hello_fincas/plugin.py
from core.plugins import BasePlugin


class HelloPlugin(BasePlugin):
    name = "hello"                      # required, unique across plugins
    verbose_name = "Hello, Fincas"
    version = "0.1.0"

    def get_nav_items(self):
        return [{
            "type": "link",
            "label": "Hello",
            "section": "analytics",
            "url_name": "erp-dashboard",   # any URL name the ERP can reverse
            "icon": "fas fa-chart-line",
        }]
```

```python
# your settings module
GOLDEN_PLUGINS = ["hello_fincas.plugin.HelloPlugin"]
```

Restart the server and a **Hello** link appears in the back-office sidebar.
A test that proves the plugin installs, runnable with your project's
`manage.py test`:

```python
# hello_fincas/tests.py
from django.test import SimpleTestCase, override_settings

from core.plugins import plugin_registry


class HelloPluginTests(SimpleTestCase):
    @override_settings(GOLDEN_PLUGINS=["hello_fincas.plugin.HelloPlugin"])
    def test_registered(self):
        plugin = plugin_registry.get_plugin("hello")
        self.assertIsNotNone(plugin)
        labels = [item["label"] for item in plugin_registry.get_nav_items()]
        self.assertIn("Hello", labels)
```

Changing `GOLDEN_PLUGINS` with `override_settings` reloads the registry, so
the test sees exactly the plugins it lists. The rest of this page covers
what a plugin can contribute.

---

## 1. Creating a Plugin

Every plugin inherits from `core.plugins.BasePlugin`.

```python
# my_plugins/my_addon/plugin.py  (your own package; never `addons`)
from core.plugins import BasePlugin, DashboardWidget

class MyCustomPlugin(BasePlugin):
    name = "my_custom_addon"
    verbose_name = "My Custom Addon"
    version = "1.0.0"
    author = "Acme Devs"
    description = "Provides specialized business logic."

    def on_ready(self) -> None:
        """Called on Django startup."""
        pass

    def get_nav_items(self):
        """Inject sidebar navigation items."""
        return [
            {
                "type": "link",
                "label": "Custom Analytics",
                "section": "analytics",
                "url_name": "erp-dashboard",
                "icon": "fas fa-chart-line",
                "color": "text-primary",
                "staff_only": False,
            }
        ]

    def get_dashboard_widgets(self):
        """Contribute cards to the main executive dashboard."""
        return [
            DashboardWidget(
                id="analytics_widget",
                title="Live Operations",
                component_or_template="my_addon/widget.html",
                width_cols=6,
                order=20,
            )
        ]
```

---

## 2. Registering Plugins

Plugins can be registered in two ways:

### Option A: `settings.GOLDEN_PLUGINS`
Specify either the class object or dotted string path in your Django settings or environment.
In a project that installs Fincas from the wheel, name the package something
other than `addons`: the wheel still ships a top-level `addons` package through
0.5.x, a deprecated alias of `golden_erp.addons` (the CRM adapters), and a second
one shadows it (`docs/packaging.md`). The old `addons.<x>.plugin.<Class>` spelling
of a `GOLDEN_PLUGINS` entry loads the same plugin with a `DeprecationWarning` until
0.6.0; listing both spellings of one plugin is refused.

```python
# your settings overlay (golden: config/settings/dev.py)
GOLDEN_PLUGINS = [
    "my_plugins.my_addon.plugin.MyCustomPlugin",
]
```

### Option B: Python Entry Points
In your standalone package's `pyproject.toml` or `setup.cfg`:

```toml
[project.entry-points."golden_erp.plugins"]
my_addon = "my_addon.plugin:MyCustomPlugin"
```
Fincas discovers these when the registry loads (at startup, and on every
reload).

### Discovery rules

- `GOLDEN_PLUGINS` loads first, in list order. Each entry is a `BasePlugin`
  subclass or its dotted import path. Two entries with the same `name` are an
  `ImproperlyConfigured` error.
- Entry points in the group `golden_erp.plugins` load next. One whose `name`
  is already registered (by `GOLDEN_PLUGINS` or an earlier entry point) is
  skipped, so the settings list wins. An entry point that does not resolve to
  a `BasePlugin` subclass is ignored; one that fails to import is logged as a
  warning and skipped.
- At startup a `GOLDEN_PLUGINS` entry that fails to load is logged as an
  error and skipped, so the server still starts. An explicit
  `plugin_registry.reload()` (tests, the shell) raises instead.
- A plugin's `on_ready()` runs once the registry is loaded, from
  `CoreConfig.ready()`.

### Django apps: `settings.GOLDEN_PLUGIN_APPS`
A plugin that ships models, migrations, admin or templates is a real Django
app. List it in `GOLDEN_PLUGIN_APPS` and finish the deployment's overlay
settings module with `compose_installed_apps`, which appends the apps to
`INSTALLED_APPS` at settings import time:

```python
# mysite/settings.py (deployment overlay)
from golden_erp.settings.prod import *  # noqa: F401,F403
from core.plugin_apps import compose_installed_apps

GOLDEN_PLUGINS = ["shop_ebay.plugin.EbayPlugin"]   # BasePlugin glue
GOLDEN_PLUGIN_APPS = ["shop_ebay"]                  # the Django app
INSTALLED_APPS = compose_installed_apps(INSTALLED_APPS, GOLDEN_PLUGIN_APPS)
```

The boundary is enforced, never silent. Startup fails with
`ImproperlyConfigured` when:

- a `BasePlugin` class is listed in `GOLDEN_PLUGIN_APPS`;
- a Django app (module path, `AppConfig` path or class) is listed in `GOLDEN_PLUGINS`;
- a `GOLDEN_PLUGIN_APPS` entry is missing from `INSTALLED_APPS` (the overlay
  forgot `compose_installed_apps`);
- `GOLDEN_PLUGIN_APPS` repeats an entry or holds a non-string.

---

## 3. Pluggable Payment Gateways

Implement `payments.gateways.BasePaymentGateway` and expose it via `get_payment_gateways()` in your plugin:

```python
from decimal import Decimal
from payments.gateways import BasePaymentGateway, GatewayResult

class CustomPaymentGateway(BasePaymentGateway):
    gateway_id = "custom_pay"
    display_name = "Custom Pay Processor"

    def charge(self, amount: Decimal, currency: str, payment_source: str, metadata=None) -> GatewayResult:
        # Call third-party API
        return GatewayResult.successful(
            transaction_id="tx_12345",
            amount=amount,
            currency=currency,
        )
```

In your plugin class:
```python
    def get_payment_gateways(self):
        return {
            CustomPaymentGateway.gateway_id: CustomPaymentGateway,
        }
```

---

## 4. Pluggable Shipping Carriers

Implement `fulfillment.carriers.BaseShippingCarrier` and expose it via `get_shipping_carriers()`:

```python
from fulfillment.carriers import BaseShippingCarrier, ShippingRate, LabelResult

class CustomFreightCarrier(BaseShippingCarrier):
    carrier_id = "custom_freight"
    display_name = "Custom Freight Logistics"

    def get_rates(self, from_addr, to_addr, parcel):
        return [
            ShippingRate(
                carrier_id=self.carrier_id,
                service_name="Freight Standard",
                service_code="FREIGHT_STD",
                rate=Decimal("120.00"),
            )
        ]

    def create_label(self, from_addr, to_addr, parcel, service_code, label_format="ZPL"):
        return LabelResult(
            success=True,
            tracking_number="FRT-998811",
            label_url="https://example.com/freight-bol.pdf",
            postage_cost=Decimal("120.00"),
        )
```

---

## 5. Listening to Canonical Lifecycle Signals

Plugins can hook into core business events without monkey-patching:

```python
from core.plugins.signals import order_placed, payment_completed

class MyPlugin(BasePlugin):
    # ...
    def on_ready(self):
        order_placed.connect(self._handle_order, dispatch_uid="my_plugin.order")

    def _handle_order(self, sender, order=None, **kwargs):
        print(f"New order placed: {order.order_number}")
```

Where each signal is sent (the model `save()` that persists the transition,
so ERP forms, DRF, admin, services and webhooks all emit):

| Signal | Sent when | Kwargs |
|---|---|---|
| `order_placed` | `Order` created in a live status (not `incomplete`, `cancelled` or `declined`), or leaves `incomplete` for one | `order` |
| `order_status_changed` | `Order.status` changes | `order, old_status, new_status` |
| `order_cancelled` | `Order` enters `cancelled` (with `order_status_changed`) | `order, reason` (`""`) |
| `payment_completed` | non-refund `Payment` first reaches captured/settled | `payment` |
| `payment_refunded` | `Payment` enters `refunded` | `payment, refund` |
| `shipment_dispatched` | `SubOrder` enters `shipped` from a pre-shipment status | `shipment, tracking_number` |
| `shipment_delivered` | `SubOrder` enters `delivered` | `shipment` |
| `inventory_adjusted` | `WarehouseStock.quantity` changes (or a row is created non-zero) | `warehouse_stock, delta, reason` (`""`) |
| `customer_registered` | `Customer` created | `customer, party, account_holder, schema_version` (v2; see below) |
| `party_created` | `Party` created through `save()` (not the backfill) | `party` |
| `party_updated` | an existing `Party` saved (once per `save()`) | `party, update_fields` (sorted tuple) |
| `party_merged` | a merge commits | `survivor, merged` (list), `merge_id` |
| `party_unmerged` | a merge is undone | `survivor, restored` (list), `merge_id` |
| `party_erased` | erasure commits | `party_id, company_id` |
| `engagement_created` | `Engagement` created through `save()` (`crm.engagements.create`: ERP, API, lead intake, a quote's thread) | `engagement` |
| `engagement_stage_changed` | `crm.engagements` writes a history row whose stage moved (move, change of pipeline, reopen at another stage) | `engagement, from_stage` (or `None`), `to_stage, change` |
| `engagement_won` | status becomes `won` | `engagement, change` |
| `engagement_lost` | status becomes `lost` | `engagement, change, loss_reason` |
| `engagement_reopened` | a won or lost engagement returns to `open` | `engagement, change, previous_status` |
| `task_created` | `crm.Task` created through `save()` | `task` |
| `task_assigned` | an existing task's `assignee` changes | `task, previous_assignee` (UserProfile pk or `None`) |
| `task_completed` | task status becomes `done` | `task` |
| `task_reopened` | task status leaves `done` / `cancelled` for an open status | `task` |
| `task_updated` | every `save()` of an existing task (reschedule, cancel, edit, completion, …); not on create | `task, update_fields` (sorted field names written), `changed_fields` (those whose value changed; unsnapshotted columns always included) |
| `task_due` | **time-driven**: `manage.py crm_emit_due` finds the task past `due_at` | `task` |
| `communication_logged` | `Communication` created (`ingest.record`, a golden-sent document email, the activity compat endpoint, lead intake) | `communication` |

**`customer_registered` v2 (0.3).** The signal now also passes `party`
(the countersigning Party: the person, or the organisation for a business
customer), `account_holder` (the person who logs in, or `None`) and
`schema_version=2`. Both parties are `None` if the party sync failed (it
never blocks the customer write; `reconcile_parties` reports it). A v1
receiver taking only `customer` keeps working because receivers must accept
`**kwargs`; nothing is removed, so there is no in-process v1 to pin.

**Party signals.** The sender is `Party`. The party backfill
(`parties.0002_backfill`, `manage.py backfill_parties`) writes with
`bulk_create` / `update` and sends none of them, nor `customer_registered`,
so a cutover sends no flood. One customer edit can save its parties more
than once, so `party_updated` can repeat for one business change: key side
effects on `(party.pk, party.updated_at)`. `update_fields` lists the columns
the save *wrote* (a plain `save()` writes nearly all of them), not a diff.
`party_merged` and `party_unmerged` are sent by the merge review
(`parties/merge.py`), `party_erased` after an erasure commits. After
`party_merged`, re-key anything stored against a merged party's id to
`survivor`. After `party_erased`, delete any personal data copied from it.

**CRM signals.** The sender
is the model class (`Engagement`, `Task`, `Communication`). `change` is the
append-only `EngagementStageChange` row the service wrote: key idempotent
side effects on `change.pk`. A reopen at a different stage sends
`engagement_reopened` then `engagement_stage_changed`; a no-op move or close
sends nothing. `task_assigned` is not sent on create (`task_created` already
carries the assignee). `task_due` is never sent from a `save()`:
`manage.py crm_emit_due`, run every 5 minutes by the deployment's scheduler
(`docs/deployment.md`), claims each task's `due_notified_at` with a
conditional `UPDATE … WHERE due_notified_at IS NULL` and sends only for the
rows it won, so overlapping runs send once; rescheduling clears the marker,
and a missed run sends late, never twice for one due time. A provider
re-import of an existing communication does not repeat `communication_logged`.

**Backfills send nothing.** The CRM backfills (`crm.backfill`,
`communications.backfill`, `manage.py backfill_crm`) and
`manage.py reconcile_engagements --fix` either write with `bulk_create` or run
inside `core.plugins.dispatch.suppressed()`, which makes `emit` a no-op for the
block (context-local; it never silences another request). Any new data step
that reuses live code that saves must run inside it too.

**Webhook payload versions.** The outbound `customer.registered` webhook
moved to v2 in 0.3 (adds `schema_version`, `party_id`,
`account_holder_party_id`, `company_id`). Subscribers that need the exact
v1 shape list `customer.registered@1`; the deprecation timeline (v1 honoured
in 0.3, logged as deprecated on every delivery in 0.4, removed in 0.5) and
the version table are in `docs/webhooks.md` section 1.1.

The sender is the model class. Rules:

- **Receivers must be idempotent** — treat delivery as at-least-once; the
  same fact can arrive twice (a status re-entered, a retried request).
- Receivers run **after the emitting transaction commits** (`on_commit`),
  each in its own `transaction.atomic()`. A rolled-back change never emits.
- A receiver that raises is logged (`logger.exception` on
  `golden_erp.plugins.signals`: signal, receiver, object pk) and skipped. It
  can never fail or roll back the payment/order that emitted it.
- Per-receiver duration is logged at DEBUG. Dispatch is synchronous in the
  request thread, so keep receivers fast.
- Best-effort: `QuerySet.update()`, `bulk_create()` and raw SQL do not emit,
  and a crash between commit and dispatch loses the call. Work that must
  not be lost, and **anything that does I/O**, is an outbox consumer
  (section 5c), not a receiver.
- Duplicated orders start `incomplete`, so they announce `order_placed` only
  when moved on. A storefront guest order is placed header-first: its totals
  can be 0 until staff build the lines.
- Golden's own outbound webhooks are an outbox consumer
  (`webhooks/outbox.py`): they hear these signals but stay **off** unless
  `WEBHOOKS_DELIVER_ON_SIGNALS = True`. With it on, a dead endpoint costs a
  request nothing (delivery runs in `process_outbox`).
- Plugin URL *names* are reserved too: a plugin route reusing a core URL
  name (or another plugin's) fails at URLconf load, because `reverse()`
  would otherwise resolve it to the plugin.

## 5a. Plugin URLs

Return URL patterns from `get_urls()`; they mount at the site root **after**
every core route (a plugin cannot shadow a core URL), so use full paths:

```python
from django.urls import path

class EbayPlugin(BasePlugin):
    name = "shop_ebay"

    def get_urls(self):
        return [path("erp/ebay/", EbayDashboardView.as_view(), name="erp-ebay")]
```

- **Staff-only by default.** Every view (including those under `include()`)
  is wrapped: anonymous visitors get the same redirect as `/erp/`
  (`/erp/login/?next=…`), signed-in non-staff users get 403.
- **Opting out is explicit:** set `url_auth = "public"` on the plugin class
  (logged as a warning at boot). Use it for endpoints that authenticate
  themselves, e.g. DRF APIs or inbound webhooks. Any other value fails startup.
- **Collisions fail startup:** two plugins returning the same pattern raise
  `ImproperlyConfigured` naming both plugins and the path. Patterns are
  compared as written, so two different patterns matching one URL are not
  detected.

## 5b. Template slots

A page offers a **slot** where plugins add markup without shadowing the
page's template. A plugin targets one by returning `TemplateExtension`s from
`get_template_extensions()`; the page renders every extension for the slot
with `{% plugin_slot "<slot>" %}` (from `erp_tags`), sorted by `order`, using
the page's own context plus `plugin_extension` (the extension itself).

```python
from core.plugins import BasePlugin, TemplateExtension

class EbayPlugin(BasePlugin):
    name = "my_ebay"

    def get_template_extensions(self):
        return [
            TemplateExtension("product_profile_panels", "my_ebay/profile_panel.html", order=20),
            TemplateExtension("product_profile_modals", "my_ebay/profile_modal.html"),
            TemplateExtension("product_profile_scripts", "my_ebay/profile_script.html"),
        ]
```

Slots that exist today (all on the product profile, `/erp/products/<pk>/`,
rendered for existing products only, never on "New Product"):

| slot | where | notes |
|---|---|---|
| `product_profile_panels` | in the page body, after Supplier Costs, before the timestamps | **inside** the profile `<form>`: a panel must not contain a `<form>` (put one in a modal), and its inputs are greyed with the rest of the page while the profile is locked. Use buttons + `fetch` for actions. Context: `product`, `images`, `categories`, `supplier_products`, `request`, `perms`. |
| `product_profile_modals` | after the profile form | modals, which may hold forms. |
| `product_profile_scripts` | at the end of the `javascripts` block, after the page's own script | a self-contained `<script>`; the page's script is an IIFE, so nothing in it is reachable. Read ids from data attributes or `{{ product.pk }}`. |

A missing template fails the page loudly (`TemplateDoesNotExist`), like any
include. Adding a slot to another page is one `{% plugin_slot %}` line plus a
row in this table.

## 5c. Outbox consumers

Durable work fed by lifecycle signals (or enqueued explicitly) is a
`core.outbox.OutboxConsumer` the plugin returns from
`get_outbox_consumers()`.

```python
from core import outbox

def build(signal_name, sender, obj, kwargs):
    task = kwargs["task"]
    # Runs INSIDE the business transaction: no network, a query or two at most.
    return [outbox.EventSpec(
        idempotency_key=f"{task.pk}:{task.updated_at.isoformat()}",  # names the fact
        event=signal_name, payload={"task_id": str(task.pk)},       # ids, not personal data
        company_id=task.company_id, object_type="crm.task", object_id=task.pk,
        party_id=task.party_id,                                     # erasure deletes pending rows
    )]

def handle(event):
    # Runs in `process_outbox` with NO transaction open. Never sleep.
    resp = client.push(event.payload)          # one attempt
    if resp.status == 429:
        return outbox.Retry(after=resp.retry_after, error="http_429")
    if 400 <= resp.status < 500:
        return outbox.Dead(f"http_{resp.status}")
    if resp.status >= 500:
        return outbox.Retry(error=f"http_{resp.status}")
    return outbox.Done()

class MyPlugin(BasePlugin):
    name = "my_sync"

    def get_outbox_consumers(self):
        return [outbox.OutboxConsumer(name="my_sync.task", signals=("task_created",),
                                      build=build, handle=handle, provider="my_sync")]
```

Rules:

- **Delivery is at least once.** A worker can die after the provider
  accepted a request and before the row is marked done; the row is retried
  when its lease expires. Make the handler idempotent (the provider's own
  idempotency key, or an upsert by `ExternalRef`).
- `build` returning `[]` writes nothing. A raising `build` is logged at ERROR
  and skipped; it never fails the save (each consumer's `build` runs in its
  own savepoint). Rows are dropped with a rollback. Pass `enabled=` (a
  `() -> bool`) so a switched-off consumer costs a save no query.
- `idempotency_key` is unique per consumer (a repeat is ignored);
  `coalesce_key` is unique among a consumer's *pending* rows (a burst of
  edits is one sync).
- Outcomes: `Done()`, `Retry(after=None, error="")` (backoff 30 s doubling,
  capped at 6 h, ±20 %; dead after `max_attempts`, default 10, about 4.3 h),
  `Dead(error)`, `Cancel(error)`. A raised exception is a `Retry` named by
  its class. `last_error` is an error code, never a response body.
- `suppressed()` writes no row; `core.outbox.origin("<provider>")` around
  inbound writes keeps a consumer with that `provider` from echoing them
  back, while every other consumer still hears them.
- `core.outbox.enqueue(name, spec)` writes a row outside a signal (respects
  `suppressed()` unless `ignore_suppression=True`).
- A consumer name claimed twice (two plugins, or a plugin and core) fails
  naming both owners.
- Tests call `core.outbox.testing.drain()`; there is no eager mode.

## 6. Payroll & Source Adapters

Payroll is a sync, not a module: adapters pull provider runs, the ERP books
the cost lines through `post_balanced_entry`. There is no HR schema to
extend — the path of least resistance is always "write an adapter."

```python
# my_plugins/my_payroll/plugin.py
from core.plugins.base import BasePlugin
from core.plugins.payroll import BasePayrollProvider, PayrollRunDraft

class MyPayrollAdapter(BasePayrollProvider):
    provider_id = "my_payroll"  # required; duplicates across plugins 400
    schema_version = 1  # bump when the provider's payload shape changes

    def pull_runs(self, since: str):
        ...  # -> list[PayrollRunDraft], raw_envelope attached verbatim

    def fetch_raw(self, provider_run_id: str):
        ...  # -> {schema_version, provider, payload}

class MyPayrollPlugin(BasePlugin):
    name = "my_payroll_plugin"

    def get_payroll_providers(self):
        return {"my_payroll": MyPayrollAdapter}
```

Contract rules (enforced by `assert_provider_contract`, which every adapter
ships as a test):

1. `provider_id` non-empty; `schema_version` a positive int.
2. Every raw envelope carries `{schema_version, provider, payload}` —
   verbatim provider responses, so schema drift replays from raw.
3. Readers call `require_known_version(envelope, known_max)` before parsing:
   unknown-newer payloads fail loudly, never misread.
4. Fixtures live at `golden_erp/addons/<id>/fixtures/` as provider-native samples +
   `*.expected.json`. CI fails on live sockets — contract tests run on
   recorded fixtures only. This is enforced: `config.settings.test` runs the
   suite with `core.testing.no_network.NoNetworkRunner`, so any connection to
   a non-loopback host (or resolution of a non-local name) raises
   `LiveSocketError`. A test that truly needs the network wraps the call in
   `core.testing.no_network.allow_network()`.
5. Every error message states problem + cause + fix + docs link.
6. Duplicate provider ids across plugins raise `ImproperlyConfigured`
   naming both plugins — payroll must never silently re-route.

### Why your plugin didn't load

- `name` empty → `ValueError` at construction.
- Duplicate provider id → `ImproperlyConfigured` naming both plugins.
- A `GOLDEN_PLUGINS` entry that fails to load → boot logs the cause and
  skips that entry (fail-closed); explicit `reload()` stays strict.
- Django app in `GOLDEN_PLUGINS`, `BasePlugin` in `GOLDEN_PLUGIN_APPS`, or a
  plugin URL claimed twice → `ImproperlyConfigured` at startup (section 2, 5a).
- Adapter import error at boot → same fail-closed path; check the boot log,
  not the adapter.
- A communication kind claimed twice → `ImproperlyConfigured` at startup
  naming both owners (section 6a).

## 6a. Communication kinds

A CRM adapter (calls, meetings, mail) logs interactions as
`communications.Communication` rows.
Its kinds register through the plugin and merge into the registry after
golden's built-ins (`email`, `sms`, `call`, `meeting`, `linkedin`, `note`) and
the deployment's `GOLDEN_COMMUNICATION_KINDS`:

```python
from core.plugins import BasePlugin
from communications.kinds import CommunicationKind

class CallsAdapter(BasePlugin):
    name = "calls_adapter"

    def get_communication_kinds(self):
        return [CommunicationKind(name="voicemail", label="Voicemail", icon="ti ti-voicemail",
                                  directions=("inbound",), has_duration=True,
                                  raw_schema_version=1)]
```

Fields: `name` (`[a-z][a-z0-9_]{0,31}`, persisted: never rename), `label`,
`icon` (`ti ti-*`), `directions` (from `inbound` / `outbound` / `internal`),
`has_duration`, `counts_as_activity` (False for sends such as marketing email
that must not reset an engagement's stale clock) and `raw_schema_version`.
A name defined twice, by two plugins or redefining a built-in or deployment
kind, raises `ImproperlyConfigured` naming both. A kind whose plugin is
uninstalled stays readable: `kinds.kind_or_unknown()` gives a generic entry.

Writes go through `communications.ingest.record(company, kind, *, direction,
occurred_at, participants, provider, external_id, envelope, ...)`:

- the envelope is the payroll one, `{schema_version, provider, payload}`;
  `record` calls `require_known_version` against the kind's
  `raw_schema_version` (or `known_max_version=` for a built-in kind);
- `external_id` upserts through `core.ExternalRef` (`source=provider`,
  `object_type` `call` / `meeting` / `message`), so re-imports are
  idempotent, and a payload that hashes the same is a no-op;
- the payload lands in the `CommunicationRaw` sidecar (readable only with
  `communications.view_raw_communication`), never on the row;
- participants (`communications.participants.Participant(role, address,
  display_name, party)`) are linked to a party only when exactly one party of
  the tenant holds the address; nothing creates a party;
- the engagement is linked only when passed, or when the participants'
  parties have exactly one open engagement.

## 6b. CRM export adapters

A CRM export adapter turns a provider's files (a file export or a recorded
API crawl) into the canonical CRM import format (`docs/crm/importing.md`);
the importer reads only that. It is a pure function of files: no database,
no network, and byte-identical output for the same input. The contract is
`core/plugins/crm_export.py`; the reference adapter is the in-tree HubSpot
add-on, `golden_erp/addons/hubspot_export/`.

```python
from core.plugins.base import BasePlugin
from core.plugins.crm_export import BaseCrmExportAdapter

class MyCrmExport(BaseCrmExportAdapter):
    provider_id = "mycrm"     # the prefix of the canonical source, "mycrm:<account>"
    schema_version = 1        # the canonical schema version it writes

    def export(self, input_dir, out_dir, *, mapping, time_zone):
        ...  # write the canonical files, manifest last; return the manifest

class MyCrmPlugin(BasePlugin):
    name = "my_crm_export"

    def get_crm_export_adapters(self):
        return {"mycrm": MyCrmExport}
```

Enable the plugin in `GOLDEN_PLUGINS`, then run
`manage.py crm_export_adapt --provider mycrm --input <export> --output <dir>`.
Contract rules (enforced by `assert_crm_export_contract`, which every adapter
ships as a test over its recorded fixtures):

1. The output validates with `crm_import.canonical.validate_canonical`;
   the manifest names the adapter's `provider_id` and `schema_version`;
   `export()` returns the manifest it wrote; a second run is byte-identical.
2. Every recorded input file is a versioned envelope, and the reader calls
   `require_known_version` on it before reading the payload. Raw provider
   objects that reach `CommunicationRaw` travel verbatim in the payroll
   envelope `{schema_version, provider, payload}`.
3. Coverage is honest: a type the adapter could not enumerate is `unknown`,
   one it knows is incomplete is `partial`, each with reasons from the closed
   list. A refusal (`AdapterError`, problem + cause + fix + docs link) writes
   no manifest.
4. Fixtures are invented (reserved `example.*` domains, `555-01xx` numbers),
   never a real or "anonymised" export; tests read them only (the suite's
   no-network guard fails any socket).
5. A provider id claimed by two plugins, a registered class that is not a
   `BaseCrmExportAdapter`, or a key that differs from the class's
   `provider_id` raises `ImproperlyConfigured` naming the plugin.

## 6c. List columns

A plugin adds columns to a back-office list with `get_list_columns(list_id)`
It is the route by which a deployment's one-to-one extension model (for a
field that must sort or aggregate, which a `metadata` sidecar field cannot)
reaches a list, the chooser and the CSV export.

```python
from core.plugins import BasePlugin
from core.plugins import ListColumn

class Qualification(BasePlugin):
    name = "qualification"

    def get_list_columns(self, list_id):
        if list_id != "crm.engagement":
            return []
        return [ListColumn(id="qualification.budget", label="Budget",
                           path="qualification__budget", format="money",
                           sortable=True, priority=3)]
```

List ids: `parties.party`, `crm.engagement`, `crm.task`, `crm.lead`, and for
every other list its model's `app_label.model` (`ERPListView.list_id`).

**The plugin names a value; it never runs a query.** Core reads the column in
the list's own SQL by annotating `F(path)`, so a column costs no extra query
per page and no plugin code runs per row. That is enforced when the list
first builds its columns; any of these is `ImproperlyConfigured` naming the
plugin:

- `id` not of the form `<plugin name>.<name>` (lower-case), or used twice;
- a `path` that is not model fields joined by `__` ending on a concrete
  field: lookups (`__icontains`), unknown names and a bare relation are
  refused;
- a hop through a reverse foreign key or a many-to-many (it would multiply
  rows), or into a credential model (`auth`, sessions, admin, content types,
  `core.ApiKey`, `core.UserIdentity`, `core.IntegrationAccount`,
  `core.IntegrationCredential`);
- `format` outside `text`, `integer`, `decimal`, `money`, `date`,
  `datetime`, `boolean`, `url`, or `priority` outside 1-3.

Forward foreign keys and one-to-one relations in either direction are
allowed. A column may be `sortable` (it is a real database column, which is
the point of an extension model); sidecar `custom.*` columns never are (F8).
`default=True` shows it to users who have not chosen their columns,
`priority` hides it below the `md` (2) or `lg` (3) breakpoint, `pii=True`
exports it only for users with `parties.export_pii`, and `export=False`
leaves it out of "All fields" exports. The CSV header is the column `id`;
values are canonical (ISO dates, plain decimals, `true`/`false`).

Columns from an uninstalled plugin disappear: a user's stored choice keeps
working, the unknown id is dropped on read.

## 6d. CRM adapters (live integrations)

A live CRM adapter connects one company to a provider (a task portal, a
mail service, a phone system): inbound by poll or webhook, outbound through
the outbox. The contract is `core/plugins/crm_adapter.py`; the reference is the invented
provider in `golden_erp/addons/fixture_crm/` (tests only, never installed in a
deployment); operator notes are `docs/crm/adapters.md`.

```python
from core.plugins.base import BasePlugin
from core.plugins.crm_adapter import BaseCrmAdapter, Capability, IngestOutcome, WebhookCheck
from core.integrations.ratelimit import RateLimit

class MyAdapter(BaseCrmAdapter):
    provider_id = "mycrm"                       # <= 32 chars: Communication.provider, ExternalRef.source
    schema_version = 1                          # newest envelope version the readers accept
    capabilities = frozenset({Capability.WEBHOOK, Capability.POLL})
    rate_limit = RateLimit(requests=5, per_seconds=1)
    base_url = "https://api.mycrm.example.com"
    settings_keys = frozenset({"list_id"})      # every IntegrationAccount.settings key it reads
    credential_kinds = frozenset({"api_key", "webhook_secret"})

    def verify_webhook(self, headers, body) -> WebhookCheck: ...   # constant-time, webhook_secret
    def poll(self, cursor, *, credential=None): ...                # -> PollPage(items, cursor, more)
    def ingest(self, envelope) -> list[IngestOutcome]: ...          # one item, any inbound path

class MyPlugin(BasePlugin):
    name = "mycrm"

    def get_crm_adapters(self):
        return {MyAdapter.provider_id: MyAdapter}
```

What core gives an adapter:

- **Accounts and secrets.** One `core.IntegrationAccount` per provider per
  company (settings JSON validated by `validate_settings`), and
  `core.IntegrationCredential` rows encrypted with `core.secrets`
  (`GOLDEN_SECRET_KEYS`, rotation, check `core.E040`). The adapter reads a
  secret with `self.secret(kind)`, which returns a `Revealed` that prints as
  `<redacted:kind>`; call `.get()` only where the header is built.
- **One HTTP path.** `self.http(method, path, ...)` goes through the injected
  `Transport` (`HttpxTransport` in production, `FixtureTransport` in tests),
  spends a rate-limit token, and applies the 2.9 policy: 429 pauses the
  account for `Retry-After` and raises `Throttled`, 401 pauses it and raises
  `CredentialsRejected`, 5xx and transport errors raise `Transient`, other
  4xx raise `ProviderRejected`; statuses in `expect=` are returned. Nothing
  sleeps. Never put a credential in a URL: httpx logs URLs.
- **Webhooks.** `POST /api/v1/integrations/<provider>/<account>/webhook/`
  verifies with `verify_webhook`, refuses a signed timestamp outside 300 s,
  dedupes by delivery id (or body hash) and enqueues `<provider>.inbound`,
  whose handler calls `ingest` inside `core.outbox.origin(<provider>)`.
- **Polling.** `manage.py integrations_poll` calls `poll` per enabled,
  unpaused account (per grant for `OAUTH_USER`), ingests each item in its own
  transaction and saves the cursor per page; the first sync runs inside
  `suppressed()` from `settings["initial_since"]` days back.
- **OAuth** (`OAUTH_USER`): set `oauth_authorize_url`, `oauth_token_url`,
  `oauth_scopes` and implement `oauth_subject(tokens)`. Core runs connect
  (PKCE, single-use state), stores the grant per user, refreshes it in
  `access_token(owner)`, and revokes only that user's grant on
  `invalid_grant`.
- **Outbound.** Return extra `OutboxConsumer`s from the classmethod
  `outbox_consumers()` (call `super()`), wrapping each handler with
  `account_handler(cls, func)`: it builds the adapter for the row's account,
  cancels rows of a disabled account, retries paused ones without calling
  the provider, and turns the errors above into `Retry` / `Dead` outcomes.
  `BasePlugin.get_outbox_consumers()` returns the adapters' consumers by
  default (section 5c); a plugin that overrides it extends `super()`. A create keys its result by `ExternalRef` and
  declares how a retried create finds a record a lost answer made
  (`push_dedupe`).

Contract rules (enforced by `assert_crm_adapter_contract`, which every
adapter ships as a test over its fixture cases):

1. `provider_id` matches `[a-z][a-z0-9_]{0,31}` and is unique across plugins
   (`ImproperlyConfigured` naming both); `schema_version` is a positive int;
   every declared capability's methods are overridden.
2. Every inbound item is an envelope `{schema_version, provider, payload}`
   read with `read_envelope` (which calls `require_known_version`); a
   `newer-schema` case is refused. `raw_envelope(item)` drops `request`
   before `communications.ingest.record`.
3. Every inbound path goes through `ingest`, and a second run of a case
   writes nothing (`changed=False`).
4. Every outbound create has a provider-side dedupe key (`push_dedupe`).
5. HTTP only through the transport: each case runs on a `FixtureTransport`
   and fails on any request without a recorded response; the recorded
   requests must equal `requests.expected.json`, secrets redacted.
6. Errors are `AdapterError` with problem + cause + fix + docs link, and
   never carry a secret.
7. No label reader (`core.labels`) anywhere in the add-on.

Fixture cases live in `golden_erp/addons/<provider>/fixtures/<case>/` (`case.json`,
`account.json`, `responses/`, `inbound/`, `requests.expected.json`,
`db.expected.json`); the format is documented in
`core/testing/integration_fixtures.py`. Author expected files with
`GOLDEN_FIXTURES_WRITE=1` and review the diff. Adapters add no models and no
migrations: provider ids live in `core.ExternalRef`, configuration and
secrets in the two core models.

## 7. Hook points

Shared apps (`core`, `products`, `pricing`, ...) never import an add-on. Where
they need add-on behaviour they ask `core/hook_registry.py` for a named hook
and degrade safely when nothing provides it, so golden boots and every shared
module imports with no add-on installed (`core/tests/test_forge_absent.py`
proves it).

The hook names are fixed in core. An unknown name in `register()`, `get()` or
`ERP_HOOKS` raises `ImproperlyConfigured`, so a typo fails at boot instead of
leaving a hook silently unwired.

| Hook | Signature | Called by | Without an implementation |
|---|---|---|---|
| `products.validate_structured_specs` | `(category_slug: str, specs: dict) -> list[str]` (warnings for specs that don't fit the category's schema) | `manage.py set_structured_specs` | specs are written unvalidated, with a warning |
| `products.staging_mpns_by_variant` | `() -> dict[variant_id, str]` (candidate MPNs for published variants from a staging source, latest wins) | `manage.py audit_mpn_coverage` | the report still runs; `--backfill` refuses with `CommandError` |

### Providing a hook

Register from the providing Django app's `AppConfig.ready()` (the app is
listed in `GOLDEN_PLUGIN_APPS`, section 2):

```python
# forge/apps.py (a deployment's add-on)
from django.apps import AppConfig


class ForgeConfig(AppConfig):
    name = "forge"

    def ready(self):
        from core import hook_registry

        from .hooks import staging_mpns_by_variant, validate_structured_specs

        hook_registry.register("products.validate_structured_specs", validate_structured_specs)
        hook_registry.register("products.staging_mpns_by_variant", staging_mpns_by_variant)
```

Registering the same callable twice is a no-op (`ready()` can run more than
once in tests); a second, *different* implementation of one hook raises
`ImproperlyConfigured` naming the first provider.

### Overriding or disabling a hook: `settings.ERP_HOOKS`

Instance config beats registration, so a deployment can swap or switch off an
implementation without touching the add-on:

```python
ERP_HOOKS = {
    "products.validate_structured_specs": "my_specs.validators.validate",  # swap
    "products.staging_mpns_by_variant": None,                              # off
}
```

`ERP_HOOKS` is validated at import: it must be a dict, every key a known hook,
every value a dotted path or `None`. Whether a dotted path actually imports is
checked on first use (`hook_registry.get`), which raises `ImproperlyConfigured`
naming the binding.

### Calling a hook from shared code

```python
from core import hook_registry

validate = hook_registry.get("products.validate_structured_specs")
warnings = validate(category, specs) if validate is not None else []
```

Adding a hook means adding its name and contract to `HOOKS` in
`core/hook_registry.py`, a degraded path at the call site, and a row in the
table above.

## 8. API token scopes

Bearer JWTs (`core/authentication.py`) carry a `scope` claim. Core ships one
scope, `"api"`: full access, and also what a token minted before scopes
existed (no claim) is treated as. A plugin adds narrowed scopes by returning
`TokenScope`s from `get_token_scopes()`:

```python
from core.plugins import BasePlugin, TokenScope
from rest_framework.exceptions import AuthenticationFailed


def _check_intake(request, payload):
    # Runs on every authentication with this scope. Return True to accept;
    # anything else refuses (raise AuthenticationFailed to set the message).
    if payload.get("aud") != current_destination():
        raise AuthenticationFailed("Token is bound to another destination.")
    return True


class ForgePlugin(BasePlugin):
    name = "forge"

    def get_token_scopes(self):
        return [
            TokenScope(name="forge-agent", path_prefix="/api/v1/forge/"),
            TokenScope(
                name="forge-intake",
                path_prefix="/api/v1/forge/intake/",          # authenticates only here
                max_lifetime_s=30 * 86400,                    # 60..2592000 (30 days)
                claims=lambda: {"aud": current_destination()},  # added at issuance
                check=_check_intake,
                verifies_aud=True,                            # _check_intake owns aud
            ),
        ]
```

Clients request one from `POST /api/v1/core/auth/token/` with
`{"scope": "forge-agent"}`; code mints one with
`issue_api_jwt(scope="forge-agent", extra_claims={...})`.

**Non-full scopes are path-bound by construction.** A scoped token
authenticates as its subject's service account, which for the legacy key is
the full-access `api-service` superuser, and no core view knows about plugin
scopes, so a scope without a prefix would be a full-access token on every
core endpoint. `path_prefix` is therefore required for every scope except
`"api"`, and the token authenticates nowhere outside it (checked on the raw
and the normalized `path_info`, so `..` cannot escape). The only way to widen
a scope is an explicit, broader prefix.

What else core enforces:

- **Unknown scopes are refused**, at issuance (`400` listing the registered
  scopes, also for a non-string `scope`; `ValueError` from `issue_api_jwt`)
  and at authentication. Because the registry is the installed plugins,
  uninstalling a plugin makes its tokens stop authenticating instead of
  quietly becoming full scope.
- **Duplicate names fail startup** (`ImproperlyConfigured` naming both owners),
  including a plugin redefining `"api"`. So does a malformed `TokenScope`:
  name not `[a-z0-9][a-z0-9_-]*`, missing or malformed `path_prefix` (must be
  `/…/`), `max_lifetime_s` outside 60..2592000 (30 days), `claims`/`check` not callable,
  or `verifies_aud=True` without a `check`.
- `check` must return exactly `True`; `False`, `None` or anything else refuses.
- `aud` is never verified by core. A token carrying `aud` is refused unless
  its scope sets `verifies_aud=True` (promising its `check` verifies it).
- The token endpoint's default lifetime (1h) is lowered to a shorter scope
  maximum; a requested lifetime is clamped to it. A scope's `max_lifetime_s`
  defaults to 24h and may be raised to 30 days, because a plugin scope is
  path-bound; core's full `"api"` scope stays capped at 24h. Raise it only for
  a client that cannot refresh (a paired browser extension). Over 24h, startup
  also requires a `check` (keep it strict, e.g. refuse once the minting user is
  no longer active staff) and a `path_prefix` of at least three segments
  (`/api/v1/<plugin>/`). To revoke one early, mint it from a per-integration
  key and revoke the key: its tokens carry the key's prefix (`akp`) and die
  with it. A token minted from the legacy `API_KEY` can only be revoked by
  rotating or removing `API_KEY` (its tokens carry a fingerprint of it, `akh`).
- Storefront tokens are always `"api"`. `extra_claims` and `claims()` may not
  set `iss`, `iat`, `exp`, `jti`, `sub`, `scope`, `akp` or `akh`.

**Per-integration keys and scopes** (`docs/identity-unification.md` §2.6). Prefer a key per integration to the shared
`API_KEY`:

```bash
manage.py create_api_key --name forge-agent --role forge-agent --scope forge-agent --expires-in 90d
```

The key acts as `svc-forge-agent`, a service account that may do exactly what
its `kind="service"` roles' groups permit (declare `forge-agent` in
`GOLDEN_ROLES` with the model permissions your views need). `--scope` binds the
key to the scope's `path_prefix` and lets it mint only that scope; a scope with
a `check` is bearer-only (the raw key has no claims to check), so the client
first POSTs the key to `/api/v1/core/auth/token/`. Bearers are signed with
`API_JWT_SIGNING_KEY`, never with `API_KEY`. A scope's `check` receives the
payload with `sub` (`api-service` or `svc-<name>`) and, for per-key tokens,
`akp`.

A plugin whose URLs use the default staff gate (`url_auth`, `require_staff`)
admits no service account: the gate runs before DRF, sees a non-staff
`svc-*` user (Api-Key) or an anonymous one (bearer), and refuses. Serve
service-facing endpoints from DRF views with `url_auth="public"` and let
`PrincipalPermission` (plus `token_scope`) decide.

To narrow further *inside* the prefix, use `token_scope(request)` in a
permission class. It returns the scope authentication accepted (for an Api-Key
request, the key's scope), full scope for the legacy key, session and
anonymous requests, and
`UNVERIFIED_SCOPE` (matching nothing) for any bearer authentication did not
accept. Compare for equality with what you allow, never with `!=`:

```python
from rest_framework.permissions import BasePermission
from core.authentication import FULL_API_SCOPE, token_scope


class RequireFullApiScope(BasePermission):
    def has_permission(self, request, view):
        return token_scope(request) == FULL_API_SCOPE
```

**Why a plugin method and not a settings registry.** A scope's meaning lives
in the plugin's code (its paths, its permission classes, the `check`
callable), so it has to exist exactly when the plugin does. A
`GOLDEN_TOKEN_SCOPES` setting could outlive the plugin and keep minting tokens
for API paths nothing serves or narrows any more. Tying scopes to
`get_token_scopes()` also gets the plugin registry's existing duplicate
detection and boot-time validation for free.
