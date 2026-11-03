# Importing CRM data: the canonical format and the runbook

This page covers the canonical format and its validator
(`check_canonical_export`), the export-adapter contract, staging and matching
(`crm_import stage`, section 9), the appliers and undo (sections 10–12), the
HubSpot export adapter (`crm_export_adapt`, section 8), the runner, dry run
and report (`crm_import run / retry / status / undo / cancel`, sections 7 and
13) and the review screens (section 14).

The importer has two halves joined by files. A per-provider
**export adapter** turns a provider's files into the **canonical format**
described here; the importer reads only canonical files. A second provider is
a new adapter, not a new importer.

Code: `crm_import/canonical/` (`schema.py` declares the format, `validate.py`
checks it, `io.py` reads and writes it). Adapter contract:
`core/plugins/crm_export.py`. Fixtures: `crm_import/tests/fixtures/canonical/`.

## 1. A canonical directory

```
export-2026-10-01/
  manifest.json          written last; the importer refuses a directory without it
  owners.jsonl           owner
  pipelines.jsonl        pipeline (stages inside)
  companies.jsonl        company
  contacts.jsonl         contact
  deals.jsonl            deal
  associations.jsonl     association
  relationships.jsonl    relationship (tickets)
  tasks.jsonl            task
  communications.jsonl   communication (notes, meetings, calls, emails, SMS)
  consent.jsonl          consent (consent and suppression facts)
  property_defs.jsonl    property_def
```

File names are fixed per type. Every file is UTF-8 JSONL: one JSON object per
line, no blank lines, no `NaN`/`Infinity`. Records are **sorted by
`source_id`** (plain string order) and serialised with sorted keys and
compact separators (`crm_import.canonical.dump_record`), so two runs over the
same input are byte-identical and diffable. Use `write_records` and
`write_manifest` rather than writing files by hand.

## 2. The record envelope

```json
{"schema_version": 1, "object_type": "contact", "source": "hubspot:1234567",
 "source_id": "51", "source_object_type": "contact", "aliases": ["18"],
 "archived": false, "created_at": "2021-03-04T10:15:00Z", "updated_at": "2026-09-30T08:00:00Z",
 "data": {"...": "canonical fields"}, "properties": {"lead_type": "Patent Attorney"},
 "history": [{"property": "lifecyclestage", "value": "lead", "at": "2021-03-04T10:15:00Z", "source": "FORM"}],
 "raw_ref": "crawl/contacts/page-0004.json#/results/17"}
```

| Key | Required | Type | Meaning |
|---|---|---|---|
| `schema_version` | yes | int | the canonical schema version (1). Newer than the reader: refused, never misread |
| `object_type` | yes | one of the 11 types | must match the file |
| `source` | yes | `"<provider>:<account id>"`, at most 64 chars | equals the manifest's `source`; becomes `ExternalRef.source` |
| `source_id` | yes | non-empty string, at most 255 | unique in its file (aliases included); becomes `ExternalRef.source_id` |
| `source_object_type` | yes | string, at most 64 | the provider's own type name (`ticket`, `note`, …); becomes `ExternalRef.object_type` |
| `aliases` | no (default `[]`) | list of strings | earlier ids of a record merged inside the provider. References may use them |
| `archived` | no (default `false`) | bool | archived in the source |
| `created_at`, `updated_at` | no | UTC instant or null | |
| `data` | yes | object | **only** the canonical fields of section 3, every one present |
| `properties` | no | object | every other source property, verbatim, under the provider's names |
| `history` | no | list of `{property, value, at, source}` | absent means "not exported" (the manifest says which properties have history), not "no changes" |
| `raw_ref` | no | string or null | where in the provider files the record came from |

Value formats used below:

- **instant**: UTC ISO 8601 with `Z` (`2026-01-31T09:00:00Z`, optional
  fraction). **date**: `YYYY-MM-DD`; date-only values stay dates.
- **decimal**: a string (`"1200.50"`), never a JSON number. A missing amount
  is `null`, never `"0"`.
- **ref**: `{"object_type": "<type>", "source_id": "<id>"}`. References are
  resolved through `ExternalRef` at apply time, never by name. A stage is
  referenced as `{"object_type": "stage", "source_id": <stage source_id>}`.
- **key**: `[a-z][a-z0-9_]{0,31}` (pipeline and stage `key`; a numeric
  provider id gets a prefix such as `hs_`).

## 3. `data` per object type

Every key listed is required in `data` (`null` where the source has nothing,
`[]` for an empty list). A key not listed is an `unknown_key` error: it
belongs in `properties`. "?" marks a nullable value.

| Type | Fields |
|---|---|
| `owner` | `email` str?, `given_name` str?, `family_name` str?, `display_name` str, `active` bool, `teams` [`{source_id, name, primary}`] |
| `pipeline` | `applies_to` `deal`/`ticket`, `key` key, `name` str, `sort_order` int, `active` bool, `stages` [`{source_id, key, name, sort_order, probability` int 0–100?`, outcome` `""`/`won`/`lost`/`closed}`]. Stage ids are unique within a pipeline only; a stage ref resolves within the record's `pipeline_ref` |
| `company` | `name` str?, `legal_name` str?, `domains` [str] (first is primary), `phones` [phone], `addresses` [address], `owner_ref` ref(owner)?, `registration_number` str?, `tax_id` str? |
| `contact` | `given_name` str?, `family_name` str?, `emails` [`{address, primary}`], `phones` [phone], `addresses` [address], `owner_ref` ref(owner)?, `company_refs` [ref(company)] (primary first), `marketable` bool?, `job_title` str? |
| `deal` | `title` str?, `pipeline_ref` ref(pipeline), `stage_ref` ref(stage), `status` `open`/`won`/`lost`?, `closed_at` instant?, `amount` decimal?, `currency` ISO 4217?, `expected_close_date` date?, `owner_ref` ref(owner)?, `company_refs` [ref(company)], `contact_refs` [ref(contact)], `loss_reason_raw` str?, `follow_up` [`{field, value?}`] |
| `association` | `from_ref`, `to_ref` ref(contact/company/deal/relationship/task/communication), `label` str?, `primary` bool |
| `relationship` | tickets: `pipeline_ref` ref(pipeline), `stage_ref` ref(stage), `state` `open`/`closed`, `subject` str?, `description` str?, `opened_at` instant?, `closed_at` instant?, `owner_ref` ref(owner)?, `contact_refs` [ref(contact)], `company_refs` [ref(company)] |
| `task` | `title` str?, `body_text` str?, `kind` name (`todo`, `call`, …), `status` `open`/`in_progress`/`waiting`/`done`, `priority` `low`/`normal`/`high`?, `due_at` instant or date?, `due_is_date` bool, `completed_at` instant?, `owner_ref` ref(owner)?, `target_refs` [ref(contact/company/deal/relationship)] |
| `communication` | `kind` `note`/`meeting`/`call`/`email`/`sms`, `direction` `inbound`/`outbound`/`internal`?, `status` str?, `subject` str?, `body_text` str?, `body_html` str?, `summary` str?, `occurred_at` instant, `ended_at` instant?, `duration_seconds` int?, `owner_ref` ref(owner)?, `participants` [`{role, address?, name?}`], `target_refs` [ref(contact/company/deal/relationship)], `raw` `{schema_version, provider, payload}` |
| `consent` | `address` str, `channel` `email`/`sms`/`phone`/`post`, `purpose` `marketing`/`transactional`?, `state` `granted`/`withdrawn`?, `suppress` `opt_out`/`hard_bounce`/`complaint`? (state, suppress or both; a suppression-only fact such as a hard bounce has `state` and `purpose` null), `occurred_at` instant?, `subscription` `{id, name?}`?, `evidence` str? |
| `property_def` | `object_type` (the type it describes), `name` str, `label` str, `type` str, `field_type` str?, `options` [`{value, label}`], `group` str?, `provider_defined` bool |

Shared shapes: **phone** `{raw, kind (phone/mobile/fax/other), primary}`
(the number exactly as the source holds it; normalisation happens at apply
time with `parties.normalize`); **address** `{purpose
(billing/shipping/mailing/registered/other), line_1?, line_2?, city?,
region?, postal_code?, country?, primary}`; participant `role` is one of
`communications.CommunicationParticipant.Role` (`from`, `to`, `cc`, `bcc`,
`organizer`, `attendee`, `caller`, `callee`).

Communications carry `raw`, the provider object in the payroll envelope
(`{schema_version, provider, payload}`), because golden keeps every provider
payload in `CommunicationRaw` and `communications.ingest.record` calls
`require_known_version` on it. Other types need no raw copy: their
`properties` already are the provider's values.

## 4. The manifest and coverage

```json
{
  "schema_version": 1,
  "provider": "hubspot",
  "source": "hubspot:1234567",
  "adapter": {"id": "hubspot_export", "version": "0.1.0", "schema_version": 1},
  "exported_at": "2026-10-01T09:00:00Z",
  "input": {"kind": "crawl", "time_zone": null},
  "objects": {
    "contact": {"file": "contacts.jsonl", "rows": 10005, "coverage": "complete",
                "declared_total": 10005, "archived": "enumerated", "history": ["lifecyclestage"]},
    "relationship": {"file": "relationships.jsonl", "rows": 0, "coverage": "unknown",
                     "reasons": [{"code": "forbidden", "detail": "403 on the tickets endpoint"}]}
  },
  "files": {"contacts.jsonl": "sha256:…"}
}
```

- `provider` is the prefix of `source`; `adapter.schema_version` equals
  `schema_version`; `input.time_zone` is an IANA name or null.
- **Every canonical type has an `objects` entry** with `file`, `rows` and
  `coverage`, plus optional `declared_total` (the source's own count of
  **live** records, as a count probe reports it; archived rows are not in it),
  `archived` (`enumerated` / `not_enumerated` / `not_applicable`), `history`
  (the properties whose history was exported) and `reasons`.
- `coverage` is `complete`, `partial` or `unknown`. `partial` and `unknown`
  need at least one reason; `complete` has none, its `declared_total` (when
  given) equals the exported non-archived rows, and its `archived` is not
  `not_enumerated`. A 403 is `unknown`, never zero.
- Reason codes are a closed list: `forbidden`, `search_cap`, `page_missing`,
  `count_mismatch`, `archived_not_enumerated`, `history_not_exported`,
  `pair_not_exported`, `file_absent`, `truncated_export`.
- A type the source has none of may have no file only when its coverage is
  `unknown` with `rows: 0` (reason `file_absent`). Every file present is in
  `files` with its sha256; `rows` equals the file's line count.

## 5. Validation

```python
from crm_import.canonical import validate_canonical

result = validate_canonical("/srv/imports/export-2026-10-01")
result.ok            # False if any error
result.errors        # [Issue(code, file, line, field, message)], line is 1-based
result.warnings      # unresolved references into types whose coverage is not complete
result.raise_for_errors()   # CanonicalFormatError (an AdapterError) naming the first errors
```

```
manage.py check_canonical_export <dir> [--json] [--max-errors N]
```

prints the coverage table and every issue, and exits non-zero on any error.
It reads files only (no database). The validator streams: it keeps ids, not
records, so a 75k-record export is fine. After `--max-errors` errors (default
1,000) the rest are counted, not listed.

### Issue codes

| Code | Rule |
|---|---|
| `manifest_missing` | no `manifest.json` (the adapter did not finish) |
| `file_missing` | the manifest lists a file the directory does not have |
| `unlisted_file` | a `.jsonl` file the manifest does not describe |
| `invalid_encoding`, `invalid_json`, `blank_line`, `not_an_object` | a line that is not one UTF-8 JSON object |
| `missing_key`, `unknown_key` | a required key absent, or a key outside the schema (envelope, `data`, nested objects, manifest) |
| `wrong_type` | e.g. an amount as a number, `null` where not nullable, `true` as an int |
| `invalid_value` | a value of the right type outside its rule: enum, slug, currency, instant/date format, range, empty string |
| `unknown_version` | `schema_version` newer than the reader (`require_known_version`); the manifest stops validation, a record stops its file |
| `version_mismatch` | a record's or the adapter's version differs from the manifest's |
| `wrong_object_type` | a record in another type's file |
| `source_mismatch` | a record's `source` differs from the manifest's, or the manifest's `source` does not start with its `provider` |
| `duplicate_source_id` | a `source_id` or alias used twice in a file (stage ids: twice in one pipeline) |
| `duplicate_key` | two pipelines with one `key`, a stage `key` twice in a pipeline, a property defined twice |
| `unsorted` | a file not sorted by `source_id` |
| `wrong_ref_type` | a reference to a type the field does not take (an `owner_ref` to a contact) |
| `dangling_ref` | a reference to a record that is not in the export while that type's coverage is `complete` |
| `unresolved_ref` (warning) | the same, while coverage is `partial` or `unknown`: the row waits for it at apply time |
| `stage_not_in_pipeline` | a deal or ticket whose stage belongs to another pipeline |
| `pipeline_kind_mismatch` | a deal in a ticket pipeline, or a ticket in a deal pipeline |
| `object_type_missing` | a canonical type with no `objects` entry |
| `row_count_mismatch`, `hash_mismatch` | the file disagrees with the manifest (edited after export) |
| `coverage_reason_missing`, `unknown_reason_code` | partial/unknown coverage without a reason, or a reason outside the list |
| `inconsistent` | cross-field: two primaries in one list, a done task without `completed_at`, a suppression with a granted state, a consent fact with neither state nor suppression (or a state without purpose), `complete` coverage with reasons, a disagreeing `declared_total` or unenumerated archived records |

Each rule has a refusing fixture under
`crm_import/tests/fixtures/canonical/invalid/<case>/` (an overlay on
`valid-small/`, with `validation.expected.json`), and a test fails if an
issue code has none.

## 6. Writing an export adapter

```python
from core.plugins.crm_export import BaseCrmExportAdapter
from crm_import.canonical import make_record, write_records, write_manifest

class MyCrmExport(BaseCrmExportAdapter):
    provider_id = "mycrm"
    schema_version = 1

    def export(self, input_dir, out_dir, *, mapping, time_zone):
        write_records(out_dir, "contact", [make_record("contact", source="mycrm:42", source_id=..., data={...})])
        ...
        return write_manifest(out_dir, provider="mycrm", source="mycrm:42", adapter={...},
                              exported_at=..., input={"kind": "export", "time_zone": time_zone},
                              objects={"contact": {"coverage": "complete"}, ...})
```

The adapter is a pure function of its input files: no database, no network.
It registers through its plugin's `get_crm_export_adapters()` (docs/plugins.md
section 6b) and ships `assert_crm_export_contract(MyCrmExport, [fixture_dir, ...])` as a
test: the output validates, the manifest names the provider and schema
version, `export()` returns the manifest it wrote, and a second run is
byte-identical. Fixtures are synthetic (reserved `example.*` domains,
`555-01xx` numbers); no real or "anonymised" export is ever committed.

**No live sockets in tests.** `config.settings.test` runs the suite with
`core.testing.no_network.NoNetworkRunner`: any connection to a non-loopback
host, or resolution of a non-local name, raises `LiveSocketError`, and the
run fails even if the code under test caught that error. Mock the client or
replay a recorded fixture. A test that truly needs the network
wraps the call in `core.testing.no_network.allow_network()`.

## 7. Operator runbook

Run on the deployment's server by an operator with shell access, never from
CI, and never with an export in a repository. Reviewers decide rows in the
back office (section 14). `<job>` is the id `crm_import stage` prints.

1. **Export.** Copy the provider export (or a recorded crawl) to the
   server's import directory (mode 0700, outside media and static).
2. **Convert.** `manage.py crm_export_adapt --provider hubspot --input
   <export> --output <canonical> [--mapping mapping.toml] [--time-zone
   <IANA>]` (section 8). It prints the coverage table with each reason and
   its fix.
3. **Check.** `manage.py check_canonical_export <canonical>`. Every
   `partial` or `unknown` line is a decision: re-export the missing scope,
   or accept it and write down why. Fix every error before going on.
4. **Stage.** `manage.py crm_import stage --company <id> --dir <canonical>
   --mapping mapping.toml` (section 9). It validates, stages, matches and
   auto-approves the unflagged rows at 0.95 or more, then prints the counts.
   A re-export of a source imported before: add `--previous-job <job>`.
5. **Dry run.** `manage.py crm_import run --job <job> --dry-run
   --report-dir <dir>`. Everything a real run does happens inside one
   transaction that is then rolled back: the database is exactly as before,
   and `<dir>/import-report.dry-run.json` says what the run would do. Read
   it (section 13): coverage first, then per step what would be created,
   linked, updated, skipped and why, what needs a reviewer, what would fail,
   and the consent line. Adjust the mapping file and stage again (a new
   job; `crm_import cancel --job <old>` frees the source) until the report
   is what the business expects.
6. **Run.** `manage.py crm_import run --job <job> --report-dir <dir>`
   applies every approved row, step by step in the order of section 13.
   Rows that still need a reviewer are left alone; anything that points at
   them fails `dependency_missing` and is retried later. The report goes to
   `<dir>/import-report.json` and to `/erp/imports/crm/<job>/report/`.
7. **Review, then run again.** Reviewers decide the open rows in step
   order. `crm_import run --job <job>` again applies the new decisions (it
   skips the steps that are complete); `crm_import retry --job <job>
   [--code dependency_missing]` re-applies failed rows once their cause is
   fixed. `crm_import status --job <job>` shows where every step is.
8. **Close.** Queue duplicate candidates with `manage.py
   party_dedupe_propose` and work them, with the import's duplicate hints, in
   the merge review (`/erp/parties/merge-review/`). Run `check_external_refs`, `check_tenancy --app parties`,
   `check_engagements`, `reconcile_engagements --check` and
   `resolve_participants`.
9. **If the run was wrong.** `manage.py crm_import undo --job <job>` undoes
   the whole job newest write first; `--batch <step>` undoes one step once
   nothing later points at it. Rows staff have edited since refuse and are
   listed; that is the correct answer (section 10). The undone rows go back
   to *approved*, so `run` re-applies them.
10. **Clean up.** Delete the export, canonical and report files once the
    job is closed; the staged rows remain as the audit trail.

## 8. The HubSpot adapter (`golden_erp/addons/hubspot_export/`)

The in-tree reference adapter. Enable it with
`GOLDEN_PLUGINS = ["golden_erp.addons.hubspot_export.plugin.HubSpotExportPlugin"]`, then:

```
manage.py crm_export_adapt --provider hubspot --input <dir> --output <new dir>
                           [--mapping mapping.toml] [--time-zone America/New_York]
```

The output directory must be new or empty (the adapter itself refuses a
directory holding anything but canonical files). The command validates what the
adapter wrote (the same check as `check_canonical_export`) and exits non-zero
on a refusal or an invalid output. A refusal writes no manifest, so the
importer refuses the directory too.

### 8.1 Inputs

`--input` holds `crawl/` (a recorded API crawl), `export/` (a file export),
or both, and optionally `mapping.toml`. Every JSON file is a **recorder
envelope**, checked with `require_known_version` before its payload is read
(a newer recorder is refused, never misread):

```json
{"schema_version": 1, "provider": "hubspot",
 "request": {"method": "GET", "path": "/crm/v3/objects/contacts", "query": {"limit": "100"}, "body": null},
 "payload": {"results": [...], "paging": {"next": {"after": "305"}}}}
```

| Path | What it is (HubSpot endpoint, recorded verbatim in `payload`) |
|---|---|
| `crawl/recording.json` | `payload.recorded_at` (UTC) and `payload.account` = `GET /account-info/v3/details` (`portalId`, `companyCurrency`) |
| `crawl/errors.json` | `payload.results`: every non-2xx response, `{method, path, query, status}` |
| `crawl/<object>/page-NNNN.json` | `GET /crm/v3/objects/<object>` (or `POST …/search`), with `associations=` to embed associations |
| `crawl/<object>/archived/page-NNNN.json` | the same with `archived=true` |
| `crawl/<object>/count.json` | a count probe: `POST …/search` with `limit=1`; its `total` is the live count |
| `crawl/history/<object>/page-NNNN.json` | the list endpoint with `propertiesWithHistory=…` |
| `crawl/associations/<from>-<to>/page-NNNN.json` | `POST /crm/v4/associations/<from>/<to>/batch/read` (labels, primary) |
| `crawl/owners.json`, `crawl/owners-archived.json` | `GET /crm/v3/owners` (and `archived=true`) |
| `crawl/pipelines-deals.json`, `crawl/pipelines-tickets.json` | `GET /crm/v3/pipelines/<object>` |
| `crawl/properties-<object>.json` | `GET /crm/v3/properties/<object>` |
| `crawl/subscriptions/page-NNNN.json` | communication-preferences statuses, one `{recipient, subscriptionStatuses}` per address |
| `export/export.json` | `payload.exported_at` (UTC), optional `payload.account` |
| `export/<object>.csv` | "export all properties" of contacts, companies, deals, tickets, tasks |
| `export/properties-<object>.json`, `pipelines-*.json`, `owners*.json` | as in the crawl; the property lists are required (they map CSV labels back) |

Objects: contacts, companies, deals, tickets, tasks, notes, meetings, calls,
emails (numeric object type ids such as `0-1` are accepted in paths). Notes,
meetings, calls and emails are read from a crawl only. Where both inputs are
given, the crawl wins **per object**: an object the crawl enumerated is not
read from the export (field-by-field merging is not built).

**What a CSV export is assumed to look like** (each line is a fixture in
`fixtures/export-csv/`): UTF-8 (a BOM is fine); a header row of property
**labels** (or internal names), resolved through the property list, an
unknown header refused; enumeration and boolean cells hold option **labels**,
mapped back to internal values (a label two options share is kept verbatim;
a stage label resolves within the row's pipeline); owner columns hold the
owner's **name**, resolved through `owners.json` (a name two owners share is
refused; a name in neither owners list stays as `hubspot_owner_name`, with no
owner reference); blank lines are skipped; a repeated Record ID is refused;
date-time cells have **no
offset** and are refused without `--time-zone` (resolved with `fold=0`);
date-only cells stay dates; associations only as `Associated <Object> IDs`
columns (`;`-separated, no labels, no primary flag beyond
`associatedcompanyid`); no archived records, no history.

### 8.2 What it writes

| HubSpot | Canonical | Notes |
|---|---|---|
| owners (active + archived) | `owner` | an owner with no `userId` is kept (it never had a login); teams primary first |
| deal / ticket pipelines | `pipeline` | key = mapping, else the slugged id (`hs_` before a digit); deal stage `isClosed` + probability 1.0 / 0.0 → `won` / `lost`, any other closed probability → `closed`; ticket `CLOSED` → `closed` |
| companies, contacts | `company`, `contact` | `hs_merged_object_ids` → `aliases`; `associatedcompanyid` marks the primary company |
| deals | `deal` | status from `hs_is_closed` / `hs_is_closed_won` (both needed to say won or lost), else the stage outcome, else null; `closedate` is `closed_at` for a closed deal and, for an open one, `expected_close_date` as a date in `--time-zone`, else the portal's `timeZone`, else UTC; blank amount → null (an amount with a comma other than a thousands separator is null too); currency = the deal's, else the portal's `companyCurrency`, else the mapping file's `default_currency`, else null |
| tickets | `relationship` | never a deal; state from the stage's ticket state |
| tasks | `task` | type, status and priority mapped; a completed task with no completion date takes its last-modified time |
| notes, meetings, calls, emails | `communication` | `raw` = `{schema_version: 1, provider: "hubspot", payload: <the recorded object, verbatim>}`; HTML bodies → text, the HTML in `body_html` |
| contact `hs_email_optout`, hard bounce, subscription statuses | `consent` | source ids hold a hash of the address, never the address |
| v4 association pages, embedded associations, CSV id columns | `association` | contact↔company, deal↔contact, deal↔company, ticket↔contact, ticket↔company; activity links become `target_refs` |
| property lists | `property_def` | notes, meetings, calls and emails share names: the first definition is kept |

Every property not read into `data` stays in `properties` under its internal
name (unset properties are omitted). The mapping file's `drop` and `sidecar`
targets are the importer's: the adapter drops nothing.

### 8.3 Coverage, and the fixture for each

| Limit | Fixture (`fixtures/cases/`) | Manifest result |
|---|---|---|
| a search crawl at the 10,000 cap | `search-capped` | `partial`, `search_cap` |
| a 403 on an object | `forbidden` | `unknown`, `forbidden` (never `rows: 0` complete) |
| a 403 on an archived list | `archived-forbidden` | `partial`, `forbidden` |
| a 403 on an association pair | `pair-forbidden` | association `partial`, `forbidden` |
| an object with no pages | `file-absent` | `file_absent` (`unknown` alone, `partial` among the four activity objects) |
| no `archived/` pages | `archived-not-enumerated` | `partial`, `archived_not_enumerated` |
| no history pages, a 403 on them, or pages that miss a live record | `history-absent` | stays `complete`; the manifest's `history` omits the property, and no record claims an empty history |
| no association pages for a needed pair, or a batch read whose inputs miss a live record | `pair-not-exported` | association `partial`, `pair_not_exported` |
| `paging.next` without the next page | `page-missing` | `partial`, `page_missing` |
| more embedded associations than a page held | `association-paged` | association `partial`, `page_missing` |
| a count probe that disagrees | `count-mismatch` | `partial`, `count_mismatch` |
| a reference to a record in no listing (a deleted owner) | `unknown-owner` | the target type `partial`, `count_mismatch` |
| a CSV cut off mid-row, or at the export row limit | `export-truncated` | `partial`, `truncated_export` |
| only a truncated body preview | `body-truncated` | communication `partial`, `truncated_export` |
| CSV date-times without `--time-zone` | `export-no-time-zone` | refused |
| a recording newer than the adapter | `newer-schema` | refused (`require_known_version`) |
| a 401, 429 or 5xx in `errors.json` | `crawl-failed` | refused; no manifest |
| a crawl plus a CSV export | `mixed` | `input.kind = "crawl+export"` |

Consent coverage follows the contacts' (opt-outs and bounces are contact
properties) plus the subscription statuses: none recorded (always so for a
file export) is `partial`, `file_absent`; statuses recorded for only some
live contact addresses is `partial`, `count_mismatch`. A 403's detail never
copies the status endpoint's path, which names an address. An address read
twice is one fact (two different statuses for it are refused).

### 8.4 The mapping file

TOML, `<input>/mapping.toml` or `--mapping`. The adapter
validates the whole file (an unknown key, an unknown property name, a bad
target or key, a pipeline or stage id the export does not hold fails) and uses: `source`, `default_currency`,
`[pipelines.<id>] key` and `[pipelines.<id>.stages]`, `follow_up` targets on
deal properties (they join `data.follow_up`), and `[task.kinds]`. The rest
is for the importer, including `[communication] default_direction`
(`inbound` / `outbound`, for calls and emails HubSpot exports without a
direction; without it such a record fails `invalid_value`).

### 8.5 Tests and fixtures

`golden_erp/addons/hubspot_export/tests/`. The contract test runs on both bases and
every converting case; each base's output equals its `expected/` byte for
byte; each case's manifest equals `expected/manifest.expected.json` and adds
exactly the reason codes `case.json` names. Regenerate after a deliberate
change with `HUBSPOT_EXPORT_UPDATE_EXPECTED=1 manage.py test
golden_erp.addons.hubspot_export --settings=config.settings.test`, and review the diff.
All data is invented; a test refuses any address outside `example.*` and any
phone number outside `555-01xx`.

## 9. Staging and matching

```
manage.py crm_import stage --company <id> --dir <canonical> [--previous-job <job id>]
                           [--mapping mapping.toml] [--include-archived]
                           [--tickets-as-engagements] [--ai-hints] [--json]
manage.py crm_import stage --resume <job id> --dir <canonical>
```

`stage` creates an `ImportJob` and runs, in order:

1. **Validate.** The same checks as `check_canonical_export`, then that the
   mapping file (the one the adapter converted with; it checked the file's
   contents) is for this export's `source`. Any error: the job is `invalid`, the
   issues are in `job.report["validation"]` / `["mapping"]`, nothing is
   staged, and the command exits non-zero.
2. **Stage.** One `unify.ImportBatch` per object type, in the apply order
   (owners, pipelines, companies, contacts, deals, associations,
   relationships, tasks, communications, consent). Every record becomes one
   `StagedRow` (`raw` = the record). Files are streamed, 1,000 rows per
   insert; there is no row cap. A second staging of the same source refuses
   while another job of it is staging or applying.
3. **Match** every row (below) and write the decision to the row.
4. **Auto-approve** every row at or above 0.95 that carries no flag.

The job ends in `reviewing`. Its `report["staging"]` counts rows per type by
state, decision and N/A reason. If the process dies, `--resume <job>` with
the same directory continues where the last committed insert ended; matching
and approval only touch rows not decided yet. A resume refuses while another
process is still staging the job (on Postgres; a crashed process releases it
by itself).

### Decisions

| Decision | Meaning | Confidence |
|---|---|---|
| `create` | nothing in golden matches: a new record | 1.0 |
| `link` | the record is an existing golden record (a party by email, phone, name and postcode, organisation name or domain; an owner by staff email; a pipeline by key) | the rule's: 0.95 (email), 0.85 (phone + family name), 0.60 (phone), 0.50 (name + postcode), 0.70 (organisation name), 0.30 (domain) |
| `update` | the record was imported before (ExternalRef) and only the source changed | 1.0 |
| `conflict` | the record was imported before and staff changed a field the source also changed | 0.0 |
| `na` | not imported, with a reason: `unchanged`, `erased`, `archived_in_source`, `duplicate_in_export`, `rejected` | — |
| none | several equally good candidates: a reviewer picks one or chooses `create` | 0.0 |

The order of evidence is: an **erased** source record, or any record naming
an erased address (a contact, an owner, a consent fact, a communication's
participant): the row becomes N/A and its data is dropped at once, because
golden never brings an erased person back (a company only loses that phone,
flagged); the record twice in
the file; archived in the source (archived owners are kept: they are the
names on old deals); an
existing `ExternalRef` (re-import); then the matchers. Deals, associations,
tickets, tasks, communications and consent facts are only ever `create` or
re-imported by `ExternalRef`: they have no natural key.

Several imported people may link to one golden person (a duplicate inside
the provider). Two imported records that look like each other are both
created; the later one carries `possible_duplicate_of`, and the merge
review queue (`/erp/parties/merge-review/`) handles them after the import.

### Flags and auto-approval

A flag keeps a row out of auto-approval and out of the bulk "approve all"
action; a reviewer can still approve it. `phone_only`, `name_mismatch`
(linked by email, but the names disagree: often a shared mailbox),
`role_address` (linked by `info@`, `sales@` and the like),
`erased_contact_point_removed`,
`competing_candidate` (another party has evidence too), `party_already_linked`,
`shared_target` (two imported records, one golden party),
`needs_individual_review` (staff, a login or an account holder),
`ambiguous_match`, `archived_owner`, `ref_target_missing`,
`stage_keys_differ`, `stage_outcome_closed`. A domain-only (R6) link is
never auto-approved.

Auto-approval approves `create`, `link` and `update` rows at or above 0.95
with no flag, recorded as `system:crm-auto-approve(<job>, 0.950)`. In
practice that is every clean new record, every clean re-import update, and
an email match to exactly one person with nothing pointing elsewhere.

### Re-import

Stage a newer export with `--previous-job <the last job of this source>`.
For a record imported before:

- **unchanged** in the source: N/A, nothing is written;
- **changed in the source only**: `update`;
- **changed by staff since the import** (and changed in the source):
  `conflict`, sorted first in the queue, settled field by field (keep
  golden's value or take the source's);
- **new emails or phones** are added; **ones the source dropped** are
  counted, never deleted;
- **records missing from the new export** are counted
  (`missing_in_source`), never deleted or deactivated.

Without `--previous-job` a record imported before still resolves through its
`ExternalRef`, but every field golden holds differently (or cannot show, such
as a reference) becomes a conflict, because there is no record of what the
last import wrote.

## 10. Applying and undoing

Approved rows are written by **appliers**, one per object type, through the
same approval runs every import uses, driven by `manage.py crm_import run
--job <id>` in step order (section 13); the generic import screen and
`unify_apply` refuse CRM batches. Applying or undoing a CRM batch needs the
`crm_import.apply_import` permission. A row whose dependency is not applied
yet fails with `dependency_missing` and is retried with `crm_import retry`.

What each type writes so far:

| Type | `create` | `link` (matched to golden) | `update` (re-import) |
|---|---|---|---|
| owner | a profile with **no login** (it shows as archived, keeps the name on history), in its primary team (created if needed) | references only; the staff member's profile is not touched | as link |
| pipeline | the pipeline (never the default) and its stages, keys as exported | the stages the existing pipeline lacks; existing stages are never edited | name / active flag, new stages |
| company, contact | the party, its emails, phones and addresses, mapped roles, the sidecar | the contact points it lacks, mapped roles, the sidecar; never a column | the changed fields the reviewer did not keep; contact points are added, never removed |
| association | contact↔company: an `employment` relationship (or the mapped kind); deal↔contact: an engagement contact; deal↔company and ticket pairs: nothing but the reference (section 11) | an existing open relationship or contact is reused | as link |
| deal | an engagement in its final state, its history, at most one follow-up task (section 11) | — (deals are never matched) | changed columns; a changed outcome or stage through the services |
| relationship (ticket) | a partner relationship to your own company, and, opted in, an engagement (section 11) | an open relationship of the same pair is reused | the ticket's facts on its reference |
| task | a task in its final state (section 12) | — | changed title, description, priority, due, status |
| communication | a note, meeting, call or email through the same path live logging uses (section 12) | — | the changed communication, updated in place |
| consent | withdrawals and suppressions; a grant only with a recorded time and basis (section 12) | an existing suppression is reused, never lifted | a new record for the changed fact |

Every row also gets one `ExternalRef` per source id and alias, and every
long-tail property lands in the record's sidecar as
`metadata["<provider>.<name>"]` (the name lower-cased, other characters
`_`), values exactly as exported (`Patent Attorney` stays `Patent
Attorney`). Values the importer adds itself start with `import_`
(`import_domains`, `import_unparsed_emails`, `import_job_title`,
`import_owner_unresolved`). Pipelines and owners keep what has no column on
their `ExternalRef.metadata` (stage labels and order; teams and properties).

Nothing is sent while an import applies or undoes: no "party created"
webhooks, no notifications.

A refused row writes nothing and records why; the message starts with a
code:

| Code | Meaning | Fix |
|---|---|---|
| `dependency_missing` | a record it points at is not imported yet | apply that batch, then retry |
| `constraint` | the database refused it (the source id is already linked to another record; a concurrent write) | review the row |
| `conflict_stale` | golden changed since the decision (staff edited a field the update would overwrite, the matched party was merged or belongs elsewhere) | re-run matching and decide again |
| `invalid_value` | the mapping or a value cannot be stored (a system role such as `customer`, a nested or over-long sidecar value, a closed stage with no won/lost pick) | fix the mapping, or pick on the review screen |
| `erased` | the record belongs to a person erased in golden | none: it is never imported again |

### Undo

Every write is logged (`crm_import.ImportWriteLog`: what the row was
before an update, and a fingerprint of what the import left). Undo walks
the log newest first and:

- deletes what the import created, **only** if it is exactly as the import
  left it and nothing else points at it;
- writes back what the import updated, under the same check;
- removes the references the import added.

A row that staff edited since, that something outside the import now
references (a quote, a relationship someone added), or that a later import
wrote again, **refuses** and stays; undo continues with the rest, and a row
that depends on a refused one refuses in turn. Undo never brings back an
erased person: erasure wipes the log's copy of their rows, and an erased
party or reference always refuses. Undone rows go back to *approved*, so
applying again is one run.

Undo the whole job with `manage.py crm_import undo --job <id>`, or one step
with `--batch <step>` once no later step points at it (section 13). Undo is a safety net
for the days after a bad run: once staff work on the data, most rows will
refuse, which is the correct answer; fix a stale import with a corrective
re-import instead.

## 11. Deals and tickets

**Deals become engagements.** An engagement is a sales opportunity, so
importing a deal never creates a quote or an order, won or not. Each deal is
written once, in its final state. Nothing about it is announced: no
notifications, no webhooks, no "engagement created" events about deals from
years ago.

- **Won, lost or open** comes from the deal's closed and won flags. When the
  export has neither, the stage decides: a won or lost stage closes it, and a
  working stage leaves it open. When the flags and the stage disagree (a deal
  marked won sitting in "Closed lost", or an open deal in a closed stage), the
  row is flagged `outcome_conflict` and waits for a reviewer, who picks the
  outcome. The import never guesses it.
- **The stage a closed deal keeps** is the one it was lost or won at
  ("lost at Negotiation"): the last working stage from its stage history.
  A deal with no history that sits in a won/lost stage has no stage, and the
  closed stage's key is kept in its sidecar (`import_closed_stage`).
- **The close date** is the deal's. If it is missing, it is the time of the
  last stage change, else the deal's last update, and the row is flagged
  `closed_at_inferred`. It is never the day of the import.
- **Stage history** becomes the engagement's history, with the source's
  timestamps, so reports show when deals really moved. Imported history is
  never timed in the stage-duration report, because it was reconstructed,
  not observed. A history entry naming a stage the export no longer has is
  skipped and listed in `import_unmapped_history`.
- **Amounts.** A missing amount stays empty, never zero; reports show "at
  least" totals. A negative amount is refused. More than two decimals is
  rounded and flagged. A deal with no currency takes the mapping file's
  `default_currency`; if the file names none, staging refuses the export,
  because the import will not assume a currency.
- **Lost reasons** map through `[deal.loss_reasons]`. Any other text becomes
  `unknown`, with the text kept in the loss notes.
- **Counterparty.** The deal's primary company, else its only contact, else
  the contact its primary association names. With several candidates and no
  primary, the engagement has no counterparty and is flagged
  `party_ambiguous`; it then shows in the "no counterparty" queue. Other
  companies on the deal are listed in `import_other_companies`.
- **Owner.** The imported owner. One missing from the export leaves the
  engagement unassigned, flagged `owner_unresolved`.
- **Follow-up.** An open deal with a next step or a next-activity date gets
  **one** open task, never one per reminder. It is titled by the next step
  (else "Follow up") and due at the latest date. A task that is already due
  is marked as notified, so the first reminder run does not send hundreds of
  overdue alerts. A closed deal gets no task.
- **Contacts on the deal.** Each deal↔contact association becomes an
  engagement contact. Its role comes from `[associations.deal_contact]`
  (`"Billing contact" = "billing"`), or the label when it names a role in
  any case ("Decision maker", "champion"), else `other`. The counterparty
  person is never listed twice. An association to a record that was not
  imported (archived, skipped) is applied without writing anything.

Flagged rows (`outcome_conflict`, `party_ambiguous`, `closed_at_inferred`,
`owner_unresolved`) are never auto-approved. A reviewer approves them, with
an outcome where one is needed. A row that failed `outcome_conflict` at
apply can be decided again the same way.

**Tickets become partner relationships**, not support cases. Each ticket
links its partner to your own company. The partner is the ticket's primary
contact, else its company (the primary one, else the first), else its first
contact.

- **Kind.** `[tickets] relationship_kind`, default `partnership`.
- **Start.** The ticket's open date.
- **End.** Only if its stage is one of `[tickets] ended_stages`. A closed
  ticket in any other stage usually means an active partnership.
- **Linking.** A second ticket for the same partner links to the same open
  relationship.
- **What is kept.** The ticket's pipeline, stage and subject are kept on the
  relationship. Every ticket's full details (description and stage history
  too) are kept on its own reference.

The job option `--tickets-as-engagements` (default off) *also* makes each
ticket an engagement in the ticket pipeline. It then needs
`default_currency` in the mapping file, and won/lost picks for the ticket
pipeline's closed stages.

**Undo** removes everything above exactly: the engagement, its history, its
follow-up task, its contacts and its references. An engagement that staff
have moved, or that something outside the import now points at, stays.
Engagement numbers that were used are not reused.

**After the import** run `manage.py check_engagements` and
`manage.py reconcile_engagements --check`. Both should be clean: every
imported engagement's next action matches its tasks.

## 12. Tasks, communications and consent

These three apply last (steps 8–10 of the order), after the people and
deals they point at.

**Links are explicit.** A task or communication belongs to the deal it was
associated with in the source, and to the first associated contact or
company. Nothing is guessed: a note associated only with a person stays on
that person's timeline, even if they have exactly one open deal. A record
whose deal is still in review waits (`dependency_missing`); one about an
erased person is never imported.

**Tasks.**

- **Status.** `open`, `in_progress`, `waiting` or `done`, as exported
  (HubSpot's *Deferred* is waiting). A done task keeps its completion time
  and never shows on My day.
- **Due.** A date is due at the end of that day in the assignee's time
  zone; a time is exact.
- **No alarm storm.** An open task that is already overdue at the import
  is marked as already notified, so `crm_emit_due` sends nothing for it. A
  task due later notifies once, when it falls due, like any other.
- **History.** The task is dated by the source: its creation, and its
  completion if done. No reschedule is invented.
- **Kind.** A task type golden does not know becomes a to-do, with the
  original kept in its sidecar.

**Communications** (notes, meetings, calls, emails) are written the same way
live email and call logging writes them. They are records, not editable
notes; the full provider object is kept separately, visible only with the
*view raw communication* permission.

- **Re-importing is safe.** An unchanged record is recognised at staging
  and skipped. A record whose content did not change writes nothing, and a
  changed one is updated in place.
- **Participants.** Each is linked to the person who holds the address,
  if exactly one does.
- **Last activity.** A deal's last activity moves forward to its newest
  completed communication or task, never back.
- **Missing direction.** A call or email without a direction is refused
  unless the mapping file names `[communication] default_direction`.

**Consent** follows golden's rules for every import:

- **An opt-out** ("unsubscribed from all email") becomes a withdrawal *and*
  a suppression of the address. It is never a grant.
- **A hard bounce** becomes a suppression only.
- **A subscription withdrawal** becomes a withdrawal for that address.
- **A subscription grant** is recorded only if the source says when it was
  given and on what basis. The address must also belong to exactly one
  person and must not be suppressed. Otherwise nothing is recorded.
- **Suppressions.** One that already exists is reused. A suppression is
  never lifted by an import.
- **"Marketing contact"** status in the source is never consent.
- **Postal preferences** are not imported.
- **Erased addresses.** An address golden erased gets nothing.

What that means, stated in the job report (`report["consent"]
["consequence"]`): **a contact imported without an explicit subscription
grant cannot be sent marketing email from golden**, even if the source CRM
counted them as a marketing contact. That is the correct default. If your
business relies on another lawful basis, record it as a deliberate,
documented bulk action after the import. The report counts the withdrawals,
suppressions and grants, and how many contacts will have no marketing
consent.

**Undo** removes what these appliers wrote, exactly. It refuses in these
cases:

- **Tasks.** A task staff have since changed stays.
- **Communications.** One erasure has since scrubbed stays.
- **Consent.** A record stays while a newer record exists for the same
  address.

## 13. Running a job: run, dry run, retry, undo, the report

```
manage.py crm_import run    --job <id> [--dry-run] [--through <step>] [--report-dir <dir>] [--no-report-file]
manage.py crm_import retry  --job <id> [--code <error code> ...] [--dry-run] [--report-dir <dir>]
manage.py crm_import status --job <id> [--json]
manage.py crm_import undo   --job <id> [--batch <step>]
manage.py crm_import cancel --job <id>
```

Steps are named by object type and always run in this order. A step starts
only when every step it needs has no approved row left unapplied (the
**step-order gate**); otherwise the run stops there and names what it waits
for. Rows still in review upstream do not hold a step back.

| # | Step | Needs |
|---|---|---|
| 1 | `owner` | — |
| 2 | `pipeline` | — |
| 3 | `company` | owner |
| 4 | `contact` | owner, company |
| 5 | `deal` | pipeline, owner, company, contact |
| 6 | `association` | company, contact, deal |
| 7 | `relationship` | contact, company, owner (+ pipeline with `--tickets-as-engagements`) |
| 8 | `task` | deal, contact, company, owner (+ relationship, idem) |
| 9 | `communication` | deal, contact, company, owner (+ relationship, idem) |
| 10 | `consent` | contact |

- **Resume.** A step with nothing left to apply is skipped, so running a
  job again continues after the last complete step. If the process dies
  mid-run, the job stays *applying*; `run` again finishes the interrupted
  batch first and applies nothing twice.
- **One at a time.** While a job stages, runs or undoes, no second process
  can run it (on Postgres; SQLite is for development), and no other job of
  the same source can stage, run or undo. Use a direct database connection,
  not a transaction-pooling proxy.
- **Dry run.** `--dry-run` runs the same code inside one transaction and
  rolls it back: nothing changes, the job's state included. Its report is
  only the file.
- **Retry** applies the failed rows only (all of them, or those with the
  `--code`s named), once their cause is fixed.
- **Undo.** Section 10's rules; `undo --batch <step>` refuses while a
  later step that depends on it still has applied rows. The job ends
  *undone*, or *partially applied* when rows refused.
- **Cancel** a job nothing was applied from (it frees the source).
- The generic import screen (`/erp/imports/<batch>/`) and `unify_apply
  --batch` refuse to apply or undo CRM batches: use these commands.

**Job states:** *reviewing* (rows undecided or approved but not applied) →
*applying* → *applied* (every row applied or N/A) or *partially applied*
(failed rows remain) → *undoing* → *undone*. `status` prints them per step
with the coverage.

### The report

`import-report.json` (`import-report.dry-run.json` for a dry run), the
same data on `/erp/imports/crm/<job>/report/` (the old
`/erp/imports/<job>/report/` redirects; staff of the job's company
only), and `status --json`. It holds **counts, codes and names only**: no
address, name, number, note or field value, so it can sit next to the
export. Per step:

| Key | What |
|---|---|
| `coverage` | the manifest's verdict, reason codes, and `exact`: when false, every count of the type is "at least" |
| `applied` | `created`, `linked` (matched to an existing record), `updated` (re-import) |
| `unchanged` | re-imported and identical |
| `skipped` | not imported, by reason (`archived_in_source`, `duplicate_in_export`, `erased`, `rejected`, …) |
| `need_review` | undecided, by proposed decision |
| `approved_pending` | decided, not applied yet |
| `failed` | by error code (section 10's table) |
| `dependency_pending` | dry run only: waiting for a row a human must decide first |
| `flags`, `fields` | review flags, and the field names updates and conflicts touch |
| `properties` | source properties kept in the sidecar without a mapping line, and those dropped |
| `state`, `blocked_by` | `applied`, `ready`, `blocked` (the gate), `in_review`, `failed`, `empty` |
| `undo` | applied rows, whether undo is available, the later steps to undo first |

Then `deals` (outcomes as exported, closed deals without stage history,
deals without a company or contact, amounts unknown, known amounts per
currency as "at least"), `consent` (withdrawals, suppressions by reason,
grants with and without a recorded basis) and **`consequence`**: how many
imported contacts will have no marketing consent in golden, with the
reason (without an explicit subscription grant, golden sends them no
marketing email).

## 14. Reviewing a job in the back office

Staging leaves a job `reviewing`; the screens under **Data & Migration →
CRM Imports** (`/erp/imports/crm/`) are where people decide what the
importer could not.

**Permissions.** Any staff user can look. Deciding rows, bulk actions,
claims, apply, retry, undo and cancel need **Can apply and undo CRM imports**
(`crm_import.apply_import`). Full source values (addresses, numbers,
postcodes, job titles, field diffs, the raw record) need **Can view unmasked
values in CRM import reports** (`crm_import.view_import_pii`); without it they
are masked or hidden, and names stay visible.

**The job page** (`/erp/imports/crm/<job>/`) has a card per object type in
apply order: coverage (a warning when it is not complete; 0 rows under
unknown coverage does not mean the source has none), rows by state, and the
step's buttons: **Review**, **Apply** (once every earlier step it needs is
applied), **Retry failed**, **Undo** (newest step first). The job's own
buttons are **Report**, **Undo the whole job** and **Cancel job**. Apply and
undo run in the request through the same runner as `crm_import run` / `undo`;
a step with more than 5,000 rows to apply is refused with the command to run
on the server instead. While an apply run is open, from the screen or the
CLI, the page shows "Applying contacts: chunk 23 of 51" and refreshes.

**The queue** (`/erp/imports/crm/<job>/<type>/`) lists undecided rows,
**least certain first** (conflicts at 0.0, then weak matches), 50 at a time.
Narrow it with the state menu and the chips (decision, confidence band,
flag, "Assigned to me"); every chip shows how many undecided rows it holds.
Open a row to see the source record next to the golden record it would link
to, the field changes of an update or conflict (choose **Keep golden** or
**Take source** for each conflicting field), the candidates, and the
outcome or stage choices deals and pipelines sometimes need. Then
**Approve**, **Pick** a candidate, **Create new instead**, or **Skip (N/A)**
with a reason.

| Key | Does |
|---|---|
| `j` / `↓`, `k` / `↑` | next / previous row |
| `Enter` | open or close the row |
| `a` | approve the proposal |
| `r` | reject: create new instead of the proposed link, otherwise skip |
| `c` | create new |
| `s`, `n` | skip (N/A) with the row's reason |
| `1`–`5` | pick that candidate |
| `x` | select the row for a bulk action |

After a decision the header updates and the next undecided row opens.

**Bulk actions.** **Approve N rows** approves every row of the current view
at 0.95 or above that has no flag, is not a domain-only (R6) link, not a
conflict and not claimed by someone else; the button says how many. Tick
rows for **Approve selected** (flagged rows ask for confirmation first),
**Create new for selected**, **Skip selected** (choose a reason) or
**Re-run matching** (after a merge or a fix elsewhere; undecided rows only).
Every bulk action is recorded in the audit log with its counts.

**Splitting a queue between reviewers.** **Claim next 50** reserves the next
50 undecided rows of your view for an hour and switches to "Assigned to
me". Others see "Claimed by you-name until 14:05" on them, cannot decide
them, and their bulk actions leave them alone; the claim lapses after the
hour or when you **Release my claims**. The header's "Your progress" line
shows decisions this session and today, yours and everyone's.

When a queue has nothing undecided it says so ("All 10,005 people are
decided. 9,880 approved, 125 skipped.") and offers **Apply**. After the last
step, the job page links to the merge queue when imported people look like
duplicates of each other.

## 15. AI suggestions (optional)

```
manage.py crm_import stage ... --ai-hints
manage.py crm_import ai --job <id> [--enable] [--force] [--json]
```

The rules decide every row. AI suggestions are a second opinion for the
rows the rules could not settle; they never decide anything.

**When they run.** Only when both are true:

- the job opted in: `stage --ai-hints`, or `crm_import ai --job <id> --enable`
  for a job already in review;
- this deployment routes the two `llmhub` purposes, `crm_match_propose` and
  `crm_dedupe_hint` (default lane: `LLMHUB_UNIFY_LANE`, or a lane set on
  `/erp/llm/`). The route is named in the job report (`report["ai"]["route"]`)
  before anything is sent; with no route nothing is sent and the report says
  why.

**What is asked.**

- `crm_match_propose`, once per contact or company row that is still
  proposed, below 0.95, with at least one candidate, and linked or undecided:
  which candidate the record is, or `create`.
- `crm_dedupe_hint`, for pairs of incoming records of one type that share an
  email, a phone, an organisation name or domain, or a name and postcode (up
  to five earlier records per record, 20 pairs per call): whether each pair
  is the same person or organisation.

**What the model sees: comparisons, never data.** Per candidate (or pair):
whether the e-mail addresses, e-mail domains, phone numbers, last four phone
digits, family names, postcodes and countries are the same (true, false, or
null when a side has none), whether the given names are the same, an initial
or different, a bucketed similarity of the names and of the e-mail local
parts (`exact`, `high`, `mid`, `low`, `none`), for organisations whether the
name and domain match, whether the golden party needs individual review, the
rule and its confidence, and the row's flags. Candidates are handles (`c1`
to `c5`, pairs `p1` to `p20`). No name, address, e-mail, phone, postcode,
domain, id, note, custom property or any other text leaves golden. Every
payload is checked against that schema before it is sent; one that fails is
not sent, that type's pass stops, and the report and the command say so. The payload for one row is the
same bytes on every run.

**What happens with the answer.**

- A suggestion is stored on the row (`proposed_mapping["ai"]`: the suggested
  party or `create`, a confidence, a short reason, the model) and shown next
  to the rule's decision. The decision, its confidence, the candidates, the
  queue's order and auto-approval are unchanged.
- A suggestion that differs from the rule's link adds the flag
  **`ai_disagrees`** (the "AI disagrees" chip). Like every flag, it keeps the
  row out of "Approve all"; a reviewer decides.
- A "same" verdict on a pair adds the earlier record to the later one's
  `possible_duplicate_of` (`proposed_mapping["ai_duplicates"]` keeps every
  verdict). It is a hint for the merge queue after the import, never a merge.
  A "not the same" verdict never removes a rule's hint.
- Output that is not the expected JSON, a confidence outside 0 to 1, or a
  handle the model was not given leaves the row as it was; the counts are in
  the report (`malformed`, `dropped`, `failed`).
- Only one AI run per job at a time; a second one refuses.
- A row a reviewer decided while its call was out is left alone (`changed`).

**In the back office.** With the job opted in and a route set, a contacts or
companies queue shows each suggestion (the AI's pick listed first), the "AI
disagrees" chip, and **Run AI suggestions**, which sends up to 10 undecided
rows of the current view per click and shows the budget left first. The
AI's pick is marked; the candidates stay in the rules' order. For more rows,
run `crm_import ai` on the server.

**Budget.** Each type's batch may spend `GOLDEN_CRM_IMPORT["ai_budget_per_batch"]`
calls (default 500; env `GOLDEN_CRM_IMPORT_AI_BUDGET`), set when the job is
staged: one per row, one per 20 pairs. At the limit the run stops; the rows
past it have no suggestion, `report["ai"][<type>]["budget_exhausted"]` is
true, and `crm_import ai` exits non-zero. Three gateway failures in a row
stop the pass too (`stopped: gateway_failing`): try again when the gateway
is back. A spent budget stays spent; `--force`
asks again for rows that already have a suggestion, within what is left.
Matching spends first, so an exhausted budget leaves no duplicate hints. A
job staged before this feature has 25 calls per type.
