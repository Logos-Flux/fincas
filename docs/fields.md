# Fields: deployment data in the `metadata` sidecar

Parties, engagements and tasks carry a `metadata` JSON column. A deployment
keeps its own fields there (a deal's package, a contact's job title) instead
of asking golden for a column. A field becomes a golden column only when a
second deployment would want the same column. Inventories of provider fields:
[`docs/crm/fields/README.md`](crm/fields/README.md).

## Keys

| Key | Example | Who writes it |
|---|---|---|
| no dot: **system** | `lead`, `backfill`, `name_inferred` | golden code only. Each key is registered per model in `core.sidecar.SYSTEM_KEYS`; a test fails on an unregistered one |
| `custom.<name>` | `custom.package` | staff, the API, imports. Needs a definition (below) |
| `<provider>.<name>` | `hubspot.hs_next_step` | imports and plugins only (`source` starting `import` or `plugin:`) |

A dotted key is `[a-z][a-z0-9_]{0,31}.[a-z0-9][a-z0-9_]{0,63}`. A key name
never holds personal data (`@`, or 7 or more digits in a row).

Values are a string (at most 2,000 characters), an integer (within ±2^53), a
boolean, or a list of up to 100 of those. No nested objects, no floats. A
`null` (or a blank string or list) removes the key: absent means unknown. An
object holds at most 200 keys and 32 KB.

## Defining fields

```python
GOLDEN_FIELD_DEFINITIONS = {
    "engagement": [
        {"key": "custom.package", "label": "Package", "type": "choice",
         "choices": [["accelerator", "Accelerator"], ["other", "Other"]],
         "group": "Qualification", "list": True, "filterable": True,
         "help": "The programme the prospect is evaluating."},
        {"key": "custom.budget", "label": "Budget", "type": "decimal",
         "group": "Qualification"},
    ],
    "party": [
        {"key": "custom.job_title", "label": "Job title", "type": "string",
         "group": "Profile", "pii": True,
         "companies": ["6f1c2c0e-8d0e-4f7b-9a43-3f8f1f0a0001"]},
    ],
    "task": [],
}
```

Objects: `party`, `engagement`, `task`. Attributes:

| Attribute | Default | Meaning |
|---|---|---|
| `key` | required | a `custom.*` key, or a provider key (then display-only) |
| `label` | required | at most 60 characters |
| `type` | required | `string` (one line), `text`, `integer`, `decimal`, `date`, `datetime`, `boolean`, `choice`, `multi_choice`, `url` |
| `choices` | `[]` | `[value, label]` pairs for the two choice types; values are lower-case keys |
| `group` | `"Details"` | the form and detail section |
| `list`, `list_default` | `False` | offered in the column chooser / shown by default |
| `filterable` | `False` | an equality filter; refused for `text`, `decimal`, `date`, `datetime` |
| `export` | `True` | in "export all fields" |
| `pii` | `False` | erasure scrubs it, audit rows omit its values; not with `list_default` |
| `required` | `False` | staff forms only, never imports |
| `companies` | `None` | restrict to these Company ids; `None` = every tenant |
| `help` | `""` | form help text |

The setting is validated when Django starts: an unknown object or attribute, a
bad key, a key defined twice on one object, choices on a non-choice type, a
filterable range type and the like raise `ImproperlyConfigured`. A company id
that does not exist is system check `core.W113` (run by `migrate` and
`check --database default`).

Stored encodings, which the API and CSV use: `decimal` as a string
(`"1250.00"`), `date` as `YYYY-MM-DD`, `datetime` as UTC with `Z`,
`multi_choice` as a list in definition order.

**Changing a definition.** Adding one is free. Changing its type, or removing
a choice that has data, strands rows: run `manage.py check_sidecar` and fix
them (or revert) before deploying. Removing a definition leaves the data in
place.

## Code

```python
from core import sidecar

sidecar.set_fields(engagement, {"custom.package": "accelerator", "custom.budget": "1250.00"},
                   source="staff", by=request.user)      # validates, merges, audits
sidecar.read(engagement, "custom.budget")                 # Decimal("1250.00")
sidecar.fields_for(engagement, group="Qualification")     # [(definition, value), …]
Engagement.objects.filter(sidecar.filter_q(Engagement, "custom.package", "accelerator"))
sidecar.strip(engagement, ["custom.budget"])              # erasure, promotion
```

`set_fields` raises `sidecar.SidecarError` (a Django `ValidationError`) naming
every refused key with a code: `metadata_key_invalid`, `metadata_key_protected`
(a system key), `metadata_key_provider` (a provider key from a non-import
source), `metadata_key_undefined`, `metadata_key_promoted`,
`metadata_value_invalid`, `metadata_too_large`. Golden code that owns a system
key writes it directly, never through `set_fields`, and registers it in
`SYSTEM_KEYS`.

Filters are **equality only**. Sorting, ranges and report aggregation on a
sidecar key are not offered: needing one means the field should be promoted
to a column.

## API

Party, engagement and task responses carry `custom_fields`: the defined
`custom.*` keys present, for the row's company. Write them there:

```http
PATCH /api/v1/crm/engagements/<id>/
{"custom_fields": {"custom.package": "accelerator", "custom.budget": null}}
```

`metadata` stays in responses. Writing it is deprecated and merges:
`custom.*` keys in it go through the same validation, changing a system key is
a 400 `metadata_key_protected`, and any other key is ignored and named in a
`Warning: 299` response header. Sending back an unchanged value is always
accepted. `metadata` becomes read-only one minor release after this change.

## Checking the data

```bash
python manage.py check_sidecar                     # exit 1 on any finding
python manage.py check_sidecar --model engagement --stats
python manage.py check_sidecar --inventory fields/hubspot.csv
```

It reports values a definition refuses, undefined `custom.*` keys, provider
keys no inventory maps (with `--inventory` or `GOLDEN_FIELD_INVENTORIES`),
email- or phone-shaped values under keys that are not `pii`, unregistered
system keys, malformed keys and values, and oversize sidecars.
