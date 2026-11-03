# Field inventories

The format and tooling for a CRM field audit: before you import from a CRM
provider, you list every property it holds and decide what golden does with
each one. An inventory lists one provider's properties, how much each is used
and what golden does with it. It is where every promotion decision starts.

## What lives where

| Here, in golden | In the deployment's repo |
|---|---|
| this README (the format) | `crm-fields/<provider>.csv`: the tenant's real inventory, custom properties and counts included |
| `<provider>-standard.csv`: each provider's documented **standard** properties with golden's default decision | the deployment's `GOLDEN_FIELD_DEFINITIONS` |
| `manage.py check_field_inventory`, `manage.py field_inventory` (code in `crm/fields/`) | |

Golden's repo never holds a tenant's property names or counts. A deployment
starts its inventory as a copy of the standard file, adds its custom
properties (`field_inventory` appends them) and fills the counts and usage.

Files today: `hubspot-standard.csv` (contacts, companies, deals, tickets);
`clickup-`, `fathom-`, `aircall-`, `google_ads-` and `klaviyo-standard.csv`
are header-only stubs until someone writes their standard rows.

## Format

UTF-8 CSV, one row per property, these columns (any order; extra columns
are kept and ignored):

| Column | Values |
|---|---|
| `provider` | the `ExternalRef.source` prefix: `hubspot`, `clickup`, `fathom`, `aircall`, `google_ads`, `klaviyo`, … (`[a-z][a-z0-9_]{0,31}`, never `custom`) |
| `object` | the provider's object: `contact`, `company`, `deal`, `ticket`, `task`, … |
| `source_id` | the provider's **stable** property id (HubSpot internal name, ClickUp field id), never the display label |
| `source_label` | the display label today |
| `source_type` | the provider's type (`string`, `enumeration`, `number`, `date`, `datetime`, `bool`, …) |
| `options` | enumerations: `value=count|value=count` |
| `populated`, `total` | record counts; blank = not counted yet |
| `filtered`, `sorted`, `reported` | `yes` / `no` / `unknown` |
| `evidence` | where each `yes` came from: `view:<id>`, `report:<id>`, `workflow:<id>`, `consumer:<name>`, `interview:<date>`. A provider `403` is `unknown (403)`: coverage unknown, not "unused" |
| `pii` | `yes` / `no` |
| `decision` | `map` / `promote` / `sidecar` / `drop` / `defer` (see [Decisions](#decisions)) |
| `target` | see below |
| `notes` | the reasoning; required for a `drop` of a populated property |

## Decisions

| Decision | Meaning | Example |
|---|---|---|
| `map` | Golden already has the concept as a typed field. **Not a promotion**: no new column | `email` → `PartyEmail`; `amount` → `Engagement.expected_value`; `dealstage` → `Stage` |
| `promote` | A new golden column, because the checklist below says so | `lifecyclestage` → `Party.lifecycle_stage` |
| `sidecar` | Kept in `metadata` under the target key; displayable and filterable when it has a field definition | `package` → `custom.package` |
| `drop` | Not imported. The raw value stays in the import batch's staged rows (`unify.StagedRow.raw`), so a later change of mind is a reprocess, not a re-fetch | provider-computed counters, derived dates |
| `defer` | The target object does not exist in golden yet; the later work that adds it decides | ticket properties |

### Choosing a decision

Run in order; the first rule that decides, decides.

1. **Is it already a golden field?** → `map`. Contact points, names, owner,
   stage, amount, close date, UTM tags and click ids (`Touchpoint`), consent
   and opt-out (`ConsentRecord` / `Suppression`), the marketing flag
   (`Party.marketable`).
2. **Is it derived by the provider?** Counters (`num_associated_deals`),
   "last activity" dates, rollups, scores computed from other fields →
   `drop`. Golden computes its own from its own records.
3. **Is it history-shaped?** Per-stage entry dates, "became X on" dates →
   the matching history table if one exists (`EngagementStageChange`), else
   `sidecar` under the provider namespace as provenance.
4. **Does another system own it?** A ClickUp task's fields, a Klaviyo list
   membership, a meeting's transcript → `ExternalRef` plus the adapter
   (`docs/crm/adapters.md`) or the raw sidecar (`CommunicationRaw`), never a
   column.
5. **Is it used?** `populated/total` under 1% *and* no `yes` in the usage
   columns → `drop` (with the count in `notes`). Under 1% with a `yes` →
   keep going.
6. **Is it personal data?** It must `map` to a typed contact point, or be
   `sidecar` with a field definition flagged `pii` (so erasure finds it), or
   `drop`. Never an undefined provider key.
7. **Is it filtered, sorted or reported on?** No → `sidecar` (with a field
   definition if staff need to see or edit it; without one if it is
   provenance only).
8. **Would a second deployment want the same column?** Would golden's
   default list or report show it to a tenant that has never heard of yours?
   Yes → `promote`. No → `sidecar` with a field definition, `filterable` if
   it is filtered. If it is **sorted or reported on** and not generic, the
   deployment adds its own one-to-one extension model; golden adds nothing.

A promotion is a schema change with a migration, so rule 8 is deliberately
strict. A sidecar field with a definition looks the same to a user in every
place except sorting. Sidecar keys and definitions: `docs/fields.md`.

### Targets by decision

| Decision | Target | Examples |
|---|---|---|
| `map` | golden's existing field(s): `Model`, `Model.field` or `Model(qualifier)`, several joined by ` + ` | `Party.given_name`, `PartyPhone(line_type=mobile)`, `ConsentRecord(withdrawn) + Suppression` |
| `promote` | the new column, `Model.field` (the model must exist; the column may not yet) | `Party.lifecycle_stage` |
| `sidecar` | the `metadata` key: `custom.<name>` (needs a FieldDefinition) or `<provider>.<name>` (provenance, import-only) | `custom.package`, `hubspot.hs_lifecyclestage_lead_date` |
| `defer` | a label for the later work that owns it; the validator requires it to start with `Phase <n>` | `Phase 5: the partner-relationship object` |
| `drop` | blank | |

A provider key defaults to the `source_id` lower-cased with anything
outside `[a-z0-9_]` mapped to `_` (`crm.fields.inventory.provider_key`);
when that is lossy, the `target` records the key actually used.

## `check_field_inventory`

```sh
python manage.py check_field_inventory                      # golden's docs/crm/fields/*.csv
python manage.py check_field_inventory crm-fields/          # a deployment's inventories
python manage.py check_field_inventory crm-fields/hubspot.csv --no-models
```

Prints `<file>:<line>: [<code>] <message>` for every refusal and exits 1 if
there is any. It refuses:

| Code | When |
|---|---|
| `missing_column` / `duplicate_column` | a required column is absent, or appears twice |
| `duplicate_row` | the same `(provider, object, source_id)` twice |
| `bad_decision` | a decision outside the five (blank included: undecided rows fail) |
| `bad_target` | a target that does not parse for its decision |
| `unknown_target` | a `map`/`promote` model that golden does not have, or a `map` field that does not exist (a new column is `promote`); skipped with `--no-models` |
| `bad_sidecar_key` | a key that breaks the key rule (`docs/fields.md`), an undotted (system) key, or a namespace other than `custom` or the row's provider |
| `duplicate_target` | two `sidecar` rows of one provider/object on the same key (a lossy `source_id` mapping would make one overwrite the other) |
| `pii_provider_key` | `pii=yes` with a `<provider>.*` key: personal data needs a typed contact point, a `custom.*` key with a `pii` definition, or `drop` |
| `personal_key` | a key or column name containing `@` or seven or more digits in a row |
| `drop_needs_reason` | a populated property dropped with empty `notes` |
| `bad_encoding` | the file is not UTF-8 (Excel's default "CSV" is not; save as "CSV UTF-8") |
| `bad_provider`, `bad_object`, `missing_source_id`, `bad_count`, `bad_usage`, `bad_pii`, `bad_options`, `bad_row` | malformed cells (`populated > total`, a usage value outside yes/no/unknown, more cells than columns, …) |

## `field_inventory`

Fills the counts from local export files. It never calls a provider API
(the CRM importer, `docs/crm/importing.md`, owns credentials and rate limits) and writes no record
values except enumeration options.

```sh
python manage.py field_inventory --provider hubspot --object deal \
    --definitions deal-properties.json --records deals.csv \
    --inventory crm-fields/hubspot.csv --output crm-fields/hubspot.csv
```

- `--definitions`: a JSON list, `{"results": [...]}` (HubSpot's properties
  endpoint) or JSONL, flat or in the importer's canonical envelope
  (`property_defs.jsonl`, the definition under `data`). Each has `name`,
  `label`, `type`, optionally `options` (`[{value, label}]`) and
  `object_type`; definitions for another object are skipped.
- `--records`: a CSV export whose headers are property names or labels.
  Multi-select values split on `;`. Option counts are written only for
  properties whose definition lists its options; an enumeration without a
  list (an owner picker) gets counts but no values, since those could be
  people's names.
- It refreshes `populated`, `total`, `source_label`, `source_type` and
  `options` on that provider/object's rows and appends a row per defined
  property the inventory lacks, with usage `unknown` and **no decision**,
  so `check_field_inventory` fails until a person decides. The usage,
  `evidence`, `pii`, `decision`, `target` and `notes` columns are never
  touched: they come from the provider's saved views, reports and
  workflows and from the consumers' code, which only a person can read.

## Not in the standard file

Properties whose names embed a tenant's own ids cannot be listed in golden:
HubSpot's `hs_date_entered_<stage id>` / `hs_time_in_<stage id>` (one per
deal stage; map to `EngagementStageChange` rows, rule 3) and custom
properties. They appear in the deployment's inventory once `field_inventory`
runs against the tenant's definitions.
