# Labels: renaming nouns in the staff UI

The label layer lets a deployment, or one tenant (Company) inside it, call a
noun something else in the back office without touching code, data or
contracts. One deployment might call a deal an "Idea"; another tenant might call
a supplier a "Partner".

## Settings

```python
# Deployment layer: every user of this instance sees these.
GOLDEN_LABEL_OVERRIDES = {
    "crm.engagement": {"singular": "Idea", "plural": "Ideas", "article": "an"},
    "purchasing.supplier": {"singular": "Partner", "plural": "Partners"},
}

# Tenant layer: keyed by Company id (UUID). Wins over the deployment layer
# for users whose UserProfile belongs to that company.
GOLDEN_LABEL_TENANT_OVERRIDES = {
    "6f1c2c0e-8d0e-4f7b-9a43-3f8f1f0a0001": {
        "orders.quote": {"singular": "Proposal"},
    },
}
```

Each override is a dict with `singular` (required), `plural` (default
`singular + "s"`) and `article` (`"a"`, `"an"` or `""`; default from the first
letter). Both settings are validated when Django starts: an unknown key, a
missing `singular`, a non-string, an unknown field or a tenant key that is not
a UUID raises `ImproperlyConfigured`. A typo fails the boot rather than
silently doing nothing.

**Resolution:** tenant override, then deployment override, then the golden
default. A user with no profile (a cross-company superuser, say) sees the
deployment layer; a superuser who has a profile sees that company's labels.

The tenant layer has two sources. `GOLDEN_LABEL_TENANT_OVERRIDES` (above) is
the settings file; **`/erp/settings/labels/`** (Admin → Labels, since
v0.4.0) stores a tenant's rewordings as `core.LabelOverride` rows, and a
row wins over the setting for its key. Every staff user reads the screen
(each key, its golden default, what is shown here and where that word comes
from); saving or clearing a rewording needs `core.change_labeloverride`. An
empty singular removes the row; a missing plural is the singular + "s"; an
empty article is derived from the word. Rows are read through
`core.label_registry.tenant_layer` (one cached entry per company plus a
cached "any rows" flag, invalidated by the model's save/delete receivers),
so a deployment with no rows pays one `EXISTS` per process, never a query
per request. Not built yet: a `core/labels/` API endpoint and a per-tenant
default skin (`Company.erp_theme`).

## Keys

Built in (`core.label_registry.GOLDEN_LABELS`; a deployment can reword them but
never add or remove one):

| Key | Default |
|---|---|
| `crm.engagement` | Engagement / Engagements |
| `crm.pipeline`, `crm.stage`, `crm.task`, `crm.lead`, `crm.touchpoint` | Pipeline, Stage, Task, Lead, Touchpoint |
| `communications.communication` | Communication |
| `parties.party`, `parties.organisation`, `parties.person` | Party, Organization, Person / People |
| `customers.customer` | Customer |
| `orders.quote`, `orders.order`, `orders.sales_invoice` | Quote, Sales Order, Invoice |
| `purchasing.supplier` | Supplier |

Plus one key per registry entry, defaulting to the entry's `label`:

| Key | Registry |
|---|---|
| `party_role.<name>` | `GOLDEN_PARTY_ROLES` (built-ins, `client` included, and your own) |
| `lifecycle_stage.<name>` | `GOLDEN_LIFECYCLE_STAGES` (built-ins `subscriber` … `other`, and your own) |
| `contact_type.<name>` | `GOLDEN_CONTACT_TYPES` (no built-ins; only your own) |

A contact type's `export_value` is not a label: it is the exact string a
segment publisher emits and never changes with a label override. `loss_reason.*`, `engagement_source.*`, `task_kind.*` and
`communication_kind.*` join when those registries land.

## Using labels

Templates:

```django
{% load labels %}
<h1>{% label "crm.engagement" plural=True %}</h1>
<a class="btn">New {% label "crm.engagement" case="lower" %}</a>
<p>{% label "crm.engagement" article=True %} needs a next action.</p>
{% label "orders.quote" plural=True as quotes_label %}
{{ labels.crm.engagement }} / {{ labels.crm.engagement.plural }}
{{ "crm.stage"|label }}  {{ "crm.stage"|label:"plural" }}
```

`case` is `as_is` (default), `lower` or `title` (Python's `str.title()`, so
"PO's" becomes "Po'S"). `plural` and `article` are truthiness: pass `True`,
not the string `"False"`. A literal unknown key is a `TemplateSyntaxError`
when the template compiles; a key held in a variable that turns out unknown
raises `core.labels.UnknownLabel` at render (a 500, deliberately loud). The
tag's output is HTML-escaped. `labels` comes from the
`core.context_processors.labels` context processor.

**The filter cannot see the request**, so it resolves deployment then default
only, never the tenant layer. Use the tag or `labels` wherever a tenant may
have reworded the noun.

Python (views, forms, admin verbose names on staff screens):

```python
from core.labels import label
label("crm.engagement")                       # "Engagement"
label("crm.engagement", True)                 # "Engagements"
label("crm.engagement", article=True)         # "an Engagement"
label("orders.quote", company=request_company)
```

ERP views: set `entity_label_key` and, for a title from it,
`page_title_format` (`"{plural}"`, `"New {singular}"`; empty keeps
`page_title`):

```python
class QuoteListView(ERPListView):
    entity_name = "Quote"              # canonical: CSV file name, chat context
    entity_label_key = "orders.quote"  # display: "New …", search box
    page_title_format = "{plural}"     # title: the resolved plural
```

Sidebar: give an `ERP_NAV` item a `label_key` and it shows that noun's plural;
`label` stays the fallback. Unknown keys fail at import.

Today the sidebar (Quotes, Sales Orders, Invoices, Customers, Suppliers), the
quote and customer list, create and edit titles, and the customer profile use
labels.

## Where labels never apply

Canonical names, always, in: API field names, values, errors and `choices`;
CSV exports (headers, values and file names); PDFs; transactional emails;
signal keywords and webhook payloads; URL paths and route names; permission
codenames; `erp_assist` tools and output; `AuditLog`; log lines. These are
contracts with machines, auditors and customers: a CSV whose header changes
when a tenant renames a noun breaks every re-import.

A deployment that wants "Proposal" on its quote PDF or email overrides that
template, not a label.

`core/tests/test_labels_never_leak.py` enforces this: PDF, print, email and
plain-text templates may not load the `labels` library; serializers, APIs,
signals, services, webhooks, `erp_assist` and management commands may not
import the label reader; and with every key overridden at both layers, a staff
API response and the list views' CSV exports are byte-identical.
