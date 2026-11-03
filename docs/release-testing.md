# Release testing: the back-office harness

The harness drives the back office end to end in a real browser, one
*journey* (lead to cash, procure to stock, returns, the books, ...) at a
time, on a freshly migrated and seeded Postgres database. It is how a
release candidate is checked before it is tagged, and you can run it on
your own checkout against any PostgreSQL 17 server.

It lives in golden's project shell, not in the wheel: `config/settings/harness.py`,
`scripts/harness/` and `storefront/scripts/erp-harness/`. Run it from a clone
of this repository; a deployment that installs the `fincas` package does not
need it.

From a fresh seed to the report, `make harness JOURNEY=R1` takes about two
minutes and `make harness` a few minutes more, depending on the machine and
the latency to the database.

## Requirements

- A clone of the repository with its virtualenv (`.venv`, see `CONTRIBUTING.md`).
- `bun` (for the Playwright journeys) and the PostgreSQL client tools on `PATH`:
  `psql`, `createdb`, `dropdb`, `pg_dump`, `pg_restore`, plus `curl`. The runner
  checks for them and exits 2 if one is missing.
- A PostgreSQL 17 server and a role that may create databases (`CREATEDB`).
  The harness never runs on SQLite.

`make harness-setup` installs the rest once per checkout (the Python dev
requirements, `bun install`, Playwright's Chromium); it is idempotent.

## Pointing it at a database

Give the runner a **server URL without a database name** in `HARNESS_PG_URL`,
either in the environment or in `.runs/harness.env` (gitignored; keep it mode
0600, it holds a password):

```bash
mkdir -p .runs && umask 077
echo 'HARNESS_PG_URL=postgres://harness:<password>@localhost:5432' > .runs/harness.env
```

Each run creates `golden_harness_a_<timestamp>` on that server, migrates,
seeds and drops it on success; a failed run keeps it and the report's repro
line names it. `HARNESS_DB_PREFIX` renames that prefix (`--cleanup` drops by
the same prefix) and `HARNESS_PORT` moves the server, so several runs can share
one Postgres at once
(`HARNESS_DB_PREFIX=golden_harness_j1 HARNESS_PORT=8790 make harness`).

In CI, set `DATABASE_URL` instead: the runner then uses that database as is
(the workflow's Postgres service) and never creates or drops it.

## Running the harness

```bash
make harness-setup                 # once per checkout: pip, bun install, Playwright Chromium (idempotent)
make harness                       # every journey on a fresh seed (R11 needs --synth, below)
make harness JOURNEY=R1            # one journey (R0-R5, R7-R10, or a comma list)
make harness JOURNEY=R1 STEP=7 DB=golden_harness_a_261017025440   # one step, on a kept database
make harness JOURNEY=R1 FROM_STEP=7 DB=...                        # from a step on
make harness KEEP_DB=1             # keep the database after a passing run
make harness-storefront            # R6: not built yet (exit 2)
make r12                           # R12: docs/getting-started.md, timed, to a first invoice
make harness-clean                 # drop every golden_harness_a_* database
scripts/harness/run.sh --help      # the flags, and the J1-J4 -> R mapping
```

Flags are the interface of `scripts/harness/run.sh` (`--journey`, `--step`,
`--from-step`, `--db`, `--synth`, `--provenance-dir`, `--keep-db`,
`--retries`, `--port`, `--runs-dir`, `--cleanup`); the make variables are
their spelling (`JOURNEY`, `STEP`, `FROM_STEP`, `DB`, `KEEP_DB`,
`HARNESS_RETRIES`, `SYNTH_DB`, `PROVENANCE_DIR`). The env aliases exist for
CI only.

**What a run does.** `seed_release_test` (one company with the fixed id
`core.release_test.HARNESS_COMPANY_ID`, a login per staff role, the
journey fixtures, including R5's paid and shipped order `SO-HRN-PAID-01`
from `core.release_test.seed_paid_shipped_order`, whose invoice and
payment are posted; passwords only in `.runs/<sha>/logins.txt`, mode 0600),
`demo_roll_dates --force` (the seed is written for a fixed `--as-of` day
and rolled to today), a service key for the API steps
(`create_api_key --role lead-intake`, in `.runs/<sha>/api-key.txt`), then
golden's server on `config.settings.harness` (`GOLDEN_HARNESS=1`,
loopback only, request and exception log at `.runs/<sha>/server.log`),
then the Playwright journeys, then `report.md`. `parties.W001`
("GOLDEN_PORTAL_COMPANY names no Company") during `migrate` is expected:
the company is seeded right after.

**Exit code.** 0 when no step failed that is not a known finding (see
below); 1 when one did; 2 when the harness could not start (a missing tool,
the settings module, no Postgres). The report still says `FAIL (known: ...)`
for a journey whose only failures are known findings: the journey is not
done until they are fixed, but a run that reproduces exactly the known gaps
is not a regression.

## Journeys

One file per journey under `storefront/scripts/erp-harness/journeys/`.

| ID | Journey | Steps |
|---|---|---|
| R0 | Server-error capture self-test | 3 |
| R1 | Lead to cash | 19: a lead through the form and the API on one engagement → a call and a task → the deal moved to won → a quote from the engagement (two shipments, a person on it), emailed once despite a double-click → converted to one order → a vendor on the drop-ship shipment and a PO from it → a PO from Purchase Orders > New → pick ticket, shipped → invoice, emailed → partial then full payment → trial balance → the engagement's thread → editing the order at 360 px |
| R2 | Losing and reopening | 8: quote and its engagement → lost with a reason → `sales` sees no Reopen → a direct reopen is refused with the styled 403 → `sales-manager` reopens where it was → a revision → won → the funnel report counts it once |
| R3 | Several quotes, one deal | 5: quote A → quote B on the same deal → A converted → B listed with its own status → nothing auto-cancelled |
| R4 | Procure to stock | 8: supplier → PO → partial receipt → stock on hand → landed cost → PO closed → PO from an order's shipment → line locked to its shipment |
| R5 | Returns and refunds | 4: a seeded paid, shipped order → refund → stock unchanged and payment status → books in balance, trial balance agrees |
| R7 | Import and undo | 5 (below) |
| R8 | People data | 8 (below) |
| R9 | Books | 8: trial balance in balance → every posting path balances → order with a draft invoice → close the period → emailing the invoice into it is refused with a message → a payment into it is refused → reopen → the invoice posts |
| R10 | Setup | 10 (below) |
| R11 | Returning user | 5, on a copy of an existing database only (below) |
| R6 | Storefront journey | not built yet |
| R12 | Fresh install to first invoice | `make r12` (`scripts/release/r12_fresh_install.sh`): runs `docs/getting-started.md` in a clean directory and fresh venv against a throwaway Postgres 17 (or `R12_FLAGS="--db sqlite"`), within a 300 s budget, then creates a first invoice in the browser |

### The report: `.runs/<sha>/report.md`

SHA, database and provenance, time; pass/fail per journey; the known
findings reproduced; then the **first failing step** with its error, the
page's own messages, the server-log entries inside its window, screenshot,
trace (`bunx playwright show-trace <path>`), video and a one-line repro
command; every step collapsed under the journey. Beside it: `steps.jsonl`
(every step's checks as JSON lines), `server.log`, `steps/` (screenshots
and trace chunks of failed steps), `video/<journey>/` (kept only when the
journey failed), `playwright-report/`.

### What every step checks

After the journey's own assertions (`lib/harness.ts`, `runStep`):

- the page answered without a 4xx or 5xx on main-frame navigations, and the
  console is clean (`console.error`, page errors; `favicon.ico` excluded);
- **server-error capture**: the server log, sliced by the step's time
  window (250 ms before, 750 ms after), holds no `ERROR` record, traceback
  or 5xx request line, even when the page looked fine. R0 proves it on
  `/harness/boom/` (a redirect to a view that raises), both fetched from
  the page with the result swallowed and navigated to;
- axe has no serious or critical finding (`storefront/scripts/lib/erp-checks.mjs`,
  the same helper the theme sweep uses);
- the page fits at 360 px (no horizontal page scroll).

`axe: false` / `fit360: false` on a step skips those two for a page that
is not a back-office screen (an API call, a print preview).

### Known gaps: `knownFinding` and `blocked`

A step whose failure is a product gap already recorded (in your issue
tracker, under a short key) carries
`{ knownFinding: "<key>" }`: it is recorded as failed-known, the journey
**continues** (so every gap in a chain surfaces in one run) and the report
lists it under "Known findings reproduced". A known finding that stops
reproducing **fails the step** ("did not reproduce: ... drop knownFinding"), the same both-directions rule the access
matrix uses. Known axe findings ride on `axeKnown` (axe rule id -> finding
key), per journey (`journey(..., { axeKnown })`) or per step; a listed rule
is recorded and does not fail the step, every other rule still does.

A step that an earlier gap leaves nothing to work on (no purchase order
was saved, so nothing can be received) calls
`blocked("<key>", "why")` from `lib/harness.ts`: it is recorded as
failed-known under that finding without having run, and simply runs once
the finding is fixed. R4 was built this way: the steps after the unsavable
PO were written as if it saved, each guarded by `blocked`, so fixing that
gap exercised receiving, stock, landed cost and close in the same run.

No step in the current journeys carries `knownFinding` except R7.01 (see
below).

## Journeys R7, R8, R10 and R11 (people and setup)

R7, R8 and R10 run on the fresh seed
(R7 stages a second tenant's worth of people, R10 needs the fixture
provider); R11 runs only on a clone of an existing database (`--synth`, below)
and defines no steps elsewhere. They sign in as **`crm-admin`**, a harness-only role
(`config/settings/harness.py`) holding `parties.merge_party`,
`parties.erase_party`, `crm_import.apply_import`, `core.manage_teams`,
`crm.manage_pipelines`, `core.change_integrationaccount`, `auth.change_user`
(the Users & roles screen) and `core.change_labeloverride` (the Labels
screen): the built-in
`staff` role carries none of them, and a superuser resolves to no company
(`core.views.request_company` fails closed), so it cannot use the
company-scoped screens at all. Reruns of these journeys need
`HARNESS_ROLE=crm-admin` (the runner signs the rerun in as `staff` by
default). Shared helpers live in `lib/erp-ui.ts`: `manage()` runs a
`manage.py` command on the harness database from a step, `searchList`
drives a list page's search box, `count` reads a model's row count,
`apiJson` makes a session-authenticated API call, `receiver` reads the local
webhook receiver.

| ID | Steps |
|---|---|
| R7 Import and undo | 1 stage the HubSpot `crawl-small` fixture (`crm_export_adapt`, `check_canonical_export`, `crm_import stage`: the screen has no upload) → 2 review every proposed row from the job's queues (owners, pipelines with stage outcomes, engagements with an outcome) → 3 apply each step from the overview in order → 4 an imported person and an imported engagement reachable from Parties and Engagements → 5 "Undo the whole job": 17 business tables back to their baseline |
| R8 People data | 1 a lead from the second Dana Whitfield lands on her engagement with the intake task (through the form); then an API lead followed by a form lead (the numbering-collision regression) → 2 a quote for the first Dana with her as the person on it → 3 the merge review reached from the Parties list's Duplicates button → 4 scan, keep the first, Merge, Apply approved → 5 the engagement, its task, the person on the quote and the employment relationship follow the survivor; the loser's page says "Merged into" → 6 Undo: both back, every link restored → 7 merge again, erase the survivor from the party's danger zone; a PII sweep (party, emails, phones, addresses, customers, lead, engagement, tasks, communications, quote, people on it) finds no name, address or phone → 8 lists, the old quote and the engagement show a label, never the tombstone |
| R10 Setup | 1 a user from Users (listed, in the `staff` group) → 2 the `sales` role granted from the user's Roles screen (listed on the row, `is_staff` kept) → 3 a team with a member → 4 a pipeline with a stage (fits 360 px) → 5 a rewording saved and cleared on Admin → Labels (`crm.engagement` → Deal; the sidebar follows) → 6 a theme from the navbar, kept across a reload → 7 the fixture provider as a connected account, a credential stored, the connection tested against the offline stub → 8 a webhook subscription added from Integrations (secret shown once) and a test delivery from the screen, with `X-Golden-Delivery-Id` → 9 the subscription through the API, a ping (its `delivery_id` is the header the receiver saw), a new party from the back office, `process_outbox --once`, one `party.created` delivery at the receiver with `X-Golden-Delivery-Id` matching its `WebhookDeliveryLog`, no repeats → 10 a login with no staff role: `/erp/` answers the styled 403 |
| R11 Returning user | 1 find a legacy quote: an engagement whose history was reconstructed (`source=backfill`; the runner's `reconcile_engagements --fix` after migrate relabels the clone's old actor-less `system` rows) → 2 Quotes list, the quote, its engagement by clicking → 3 the history lists every change and marks reconstructed rows ("reconstructed · backfill") → 4 the engagement's next-action card, then the Tasks list → 5 the stage report's footnote discloses the reconstructed history |

R7.01 is a known finding by design (`R7-no-upload-ui`): CRM exports hold
personal data and are staged on the server with `manage.py crm_import stage`,
so the imports screen has no upload. Every other step passes.

Observations recorded by the steps, not failures: the Engagements list
opens on the "Unassigned" (or "Mine") chip and carries it as a hidden
input of its search form, so a number typed from the default view finds
nothing when the engagement is owned or closed (`searchList` drops the
carried chip); the webhook ping (`POST .../ping/`) carries no
`X-Golden-Delivery-Id`, only outbox deliveries do, so the receiver cannot
dedupe a repeated ping; a lead from a known customer's address lands on the
customer's existing person party (the party sync matches by email), and a
business customer's `Customer.party` is the organisation, the person being
the party that holds the customer's email.

### R11 on a copy of your own data

`--synth SOURCE` (make: `make harness-synth SYNTH_DB=<source database>`) clones
`SOURCE` from the same server (`pg_dump -Fc | pg_restore --no-owner --no-acl`;
`createdb -T` fails while other sessions sit on the source), migrates, runs
`reconcile_engagements --fix` (the sequence a real upgrade runs after
`migrate`), runs `seed_release_test --overlay` (role logins and the additive
fixtures: no second company, no clock pin, no fiscal periods), runs the
journeys and drops the clone. Use a database with personal data scrubbed: the
run directory then holds screenshots and traces of it. `--provenance-dir`
(`PROVENANCE_DIR`) names a directory whose SHA256 the report records, e.g. the
scripts that scrubbed the copy. Journeys needing two tenants or the import
fixture (R6, R7, R10) run on the fresh seed only; R11 runs only on a clone. The
overlay's tenant is the clone's own company: the runner reads the
`GOLDEN_PORTAL_COMPANY=` line the seed prints and names it for the API key and
the server.

## Adding a journey

1. Create `storefront/scripts/erp-harness/journeys/<Rn>.spec.ts`. One file
   is one journey (`--journey Rn` filters by file name).
2. `journey("Rn", "Title", (step) => { step(1, "name", async (ctx) => { ... }, opts) })`.
   Steps share one page and the `ctx.state` bag, persisted to
   `.runs/<sha>/state-<Rn>.json`, so `--step n` and `--from-step n` rerun
   against a kept database. A rerun starts signed in on the dashboard.
   Write steps so they tolerate a rerun: assert deltas, take ids from the
   URL you landed on, pick an existing record when the previous attempt
   made one.
3. `ctx.login(role)` signs in as the seeded login of that role
   (`staff`, `sales`, `sales-manager`, `warehouse`, `purchasing`,
   `bookkeeper`, `superuser`). `ctx.db("app.Model", { field: value })`
   and `ctx.dbOne` read rows through `/harness/state/<app.Model>/`
   (ORM lookups, `_order`, `_limit`); `ctx.gl(companyId)` the trial
   balance (totals, `balanced`, per-account and per-source sums, the
   last 50 entries, `entry_count`, and `unbalanced`: entries whose own
   legs differ); `ctx.apiKey` the service key; `ctx.csrf()` the CSRF
   cookie for `fetch()` POSTs made from the page.
4. Reach every screen by clicking (the sidebar helper in R1: `clickNav`,
   which opens the collapsed sidebar at narrow widths). A screen a user
   cannot reach by clicking is a finding, not a `page.goto`.
5. A product gap gets `knownFinding` (or `axeKnown`) and an issue; the step's comment says what was found and when. A step
   starved by an earlier gap calls `blocked(key, why)`.
6. Shared helpers live in `lib/erp.ts`: `clickNav`, `pickCustomer` (the
   editor's customer or supplier search widget), `pickSku`, `addLine`,
   `card` (shipment group cards), `doubleSubmit` (the double-click
   pattern), `postFromPage` (one POST with the session, redirects not
   followed: what typing an action's URL gets), `alerts` (the page's
   visible messages), and the `docs/formatting.md` shapes `MONEY_RE`,
   `DATE_MJY_RE`, `DATE_ISO_RE`, `expectMoney`, `moneyValue`. R1 keeps
   its own copies (it predates the file); R2 to R5 and R9 import them.
   R9 closes and reopens the current fiscal period, so it must run last
   (journeys run in file-name order).

`/harness/state/` takes ORM lookups as strings, so `__isnull=True` and
`pk__in=a,b` are refused (400 / 500): fetch the rows and filter in the step
(`rows.filter((r) => r.undone_at == null)`), or query per id. A step that
needs a management command (staging an export, `process_outbox --once`, a
fixture) calls `manage()` from `lib/erp-ui.ts`, which runs `manage.py`
with the runner's environment and the request log unset. A step that must
read an expected 4xx (R10's 403) uses `page.request` instead of navigating,
so the runner's own status check does not fire on the status under test.

Selectors that bit: a sidebar link's accessible name includes its icon
glyph, so match `.nav-link-title` text, not `getByRole("link", { name: /^Leads$/ })`;
the line editor's item name is a `textarea` (`[name$='-item_name']`);
`LogoutView` is POST-only, so sign out by clearing cookies; the People
card's pick list has no placeholder (index 0 is the first candidate);
`selectOption` takes a label string, not a regex.

## The J1-J4 API journeys and the R journeys they second

`scripts/j1..j4_*.py` are scripted API journeys, run against a server with
an API key. Read from their headers:

| script | journey | UI journey it seconds |
|---|---|---|
| J1 `j1_procure_to_sell.py` | supplier -> PO -> receive -> stock -> quote -> order -> payment -> ship -> invoice | R4 (procure to stock) and R1 (quote to invoice) |
| J2 `j2_returns_refund.py` | paid order -> refund -> stock restored | R5 (returns and refunds) |
| J3 `j3_promo_combine.py` | coupon + tier price on an order | R1 (pricing on the order; no journey of its own) |
| J4 `j4_period_close.py` | closing a period blocks backdated posts | R9 (books) |

`scripts/harness/run.sh --help` prints the same table.

## Common failures

- **A live backend on :8000 or :8001.** The harness server is on 8771 and
  ignores them, but a storefront e2e run (`storefront/tests/e2e`) renders
  the offline demo catalog and fails six journey tests when a Django dev
  server answers on :8000. Point the storefront at a dead API
  (`VITE_API_BASE_URL=http://127.0.0.1:59999/api`) for those. `make harness` refuses to start when
  something already answers on its own port: pick `--port`.
- **"No tests found".** `--journey X` filters by file name
  `journeys/X.spec.ts`; the runner refuses an unknown journey with the
  path it looked for.
- **Stale Vite after new files.** Only the storefront journey (R6, not built
  yet) uses Vite; it misses Tailwind classes in files created
  while it runs. Restart it after adding a route or component.
- **Colour transitions.** `body` and many links transition `color`, so
  axe can read the unthemed fallback right after a navigation. The shared
  helper waits for every running `CSSTransition` before axe runs; a step
  that checks colours itself must do the same (`awaitTransitions`).
- **Rerun artefacts.** `--step`/`--from-step` on a kept database see what
  the previous attempt wrote (a second quote, a second email). Steps
  assert deltas and take ids from the URL; a count assertion that is off
  by exactly one attempt is this.
- **A 5xx in the window that is not the page's.** The capture slices the
  server log by time, so a background request (another tab, a webhook
  retry) inside the window fails the step; its request line is in the
  report. Serial journeys on one page keep this rare.
- **`parties.W001` during migrate**, and the two key-derivation warnings
  (`core.W110`, `llmhub.W001`) when running `manage.py` by hand under the
  harness settings: expected; the runner sets dedicated keys.
- **Edited `run.sh` while a run was going.** bash reads a script as it
  executes, so an edit mid-run lands in the running copy ("syntax error
  near unexpected token", a wrong `cd`, no report). The journeys' own
  results are intact in `steps.jsonl`; rerun for the report.
- **"Cannot resolve keyword" / 500 from `/harness/state/`.** The endpoint
  takes lookups as strings: no `__isnull`, no `__in` lists, and reverse
  names must be the model's (`core.UserProfile?team=<pk>` for a team's
  members). Filter in the step instead.
- **The known-finding step passed.** Good news: close the gap's issue,
  then drop `knownFinding` from the step, and expect the step's page
  checks (axe, 360 px, server log) to run for the first time: a step that
  had always failed before them never exercised them.
- **A rerun hits what the previous attempt made.** A step that creates a
  record from a control that disappears once the record exists (Create PO
  on a shipment group, Record Payment on a paid invoice) must look for the
  record first; R1.11 and R1.16 show the pattern.
- **`make harness-clean` drops every `golden_harness_a_*` database on the
  server, another lane's kept repro included.** Drop your own by name
  (`psql "$HARNESS_PG_URL/postgres" -c 'drop database "golden_harness_a_<ts>" with (force)'`).

## Running it in CI: the nightly

`.github/workflows/nightly.yml` is a worked example. It runs every journey
on a fresh seed at 03:15 UTC on `main`, and on `workflow_dispatch` with `ref`
(a branch, tag or SHA, e.g. a release candidate) and `full_sweep` (the
back-office theme sweep, sharded by theme). The parts worth copying:

- a Postgres 17 service, with `DATABASE_URL` pointed at it, so the runner
  neither creates nor drops a database;
- `bash scripts/harness/run.sh --journey all --retries 1`: one retry per
  failed step;
- the harness capped at 50 minutes inside a 60-minute job, so a timeout is
  reported as **infra**, like a preflight failure (exit 2), not as a journey
  failure (exit 1);
- `.runs/` uploaded as an artifact without `logins.txt` and `api-key.txt`.
  Upload only runs on synthetic data, never a clone of real data.

## Access matrix

`core/tests/test_access_matrix.py` drives every API viewset, every `/erp/`
model view and every other route as each principal the deployment knows,
and checks the answer against an independent source of expectations. It
runs with the normal suite (about 30 s on SQLite):

```sh
.venv/bin/python manage.py test core.tests.test_access_matrix --settings=config.settings.test
```

### What it covers

| Group | Enumerated by | Actions |
|---|---|---|
| (a) API viewsets with a model | `core.access_matrix.api_viewsets()` | `list`, `retrieve`, `create`, `update`, `partial_update`, `destroy`, every `@action` |
| (b) `/erp/` class-based views with a declared `model` | `core.access_matrix.erp_model_views()` | `list`, `export` (`?export=csv`), `detail`, `create`, `edit`, `delete`; anything else is `custom:<url name>` |
| (c) every other route (function views, model-less views, auth, portal, token endpoints, plugin routes) | `core.access_matrix.other_routes()` | GET and, where the view has one, POST |

Principals: `anonymous`, `superuser`, `staff`, one `role:<name>` per further
`staff=True` role in `GOLDEN_ROLES` (the test adds `clerk`, a staff role that
declares no permission), two portal customers (`portal:a`, `portal:b`), the
`storefront` identity, a pinned service key (`service:matrix-reader`, role
declares `purchasing.view_supplier` only) and the `legacy-key` (`API_KEY`,
API only).

Object ownership: each portal customer is refused the other's order, quote and
address and never sees the other's invoice or order in a list; the payment link
needs the invoice's token, not its id. A scoped staff role is refused the
`perm:` gates it does not declare (teams, erasure) and gets the CRM CSV export
without the personal-data columns.


Not covered on purpose: cross-company isolation (one instance per company is
the design; see `docs/deployment.md`) and the Django admin (its own auth).

### Where the expectations come from

`core/tests/access_matrix_expectations.py`, never the permission classes:

1. `ROLES_MD` encodes `docs/roles.md` once: a `staff=True` role reaches the
   back office and the staff API, a `portal=True` role the portal API, a
   `kind="service"` role exactly the model permissions it declares (view /
   add / change / delete; custom actions never; writes need a pinned company,
   except the storefront's `add`). A superuser passes every staff gate.
2. `MODEL_OVERRIDES` refines that per model and action where roles.md is too
   coarse (`perm:crm.manage_pipelines` on pipeline writes, public catalog
   reads, ...).
3. `ROUTE_CLASSES` classifies every (c) route.

A spec is `"<class>[;human][;leak:<principal>:<issue>][;skip:<reason>]"`:

| Class | Allowed |
|---|---|
| `public` | everyone |
| `staff` | superuser, every `staff=True` role; service accounts by contract (API only) |
| `superuser` | superuser only |
| `perm:<app.codename>` | superuser, and roles whose `GOLDEN_ROLES` entry declares the permission |
| `portal` | portal customers only |

`human` refuses every machine principal (service accounts, legacy key) on top
of the class.

### A failure

```
crm.Stage api:create (POST /api/v1/crm/stages/) as staff: got 403 (denied), expected allowed
(docs/roles.md 'Built-in roles': ...; MODEL_OVERRIDES['crm.Stage'] keeps the default) - see docs/release-testing.md#access-matrix
```

Read it as: the server contradicted the cited row. Either the code is wrong
(fix it, with a test) or the expectation is (edit the cited dict entry, with a
comment saying which doc the refinement comes from). A `denied` that should be
`allowed` for a staff principal is usually a finer permission gate the dict
does not know yet; an `allowed` that should be `denied` is a leak: report it as a security
issue (`SECURITY.md`) before anything else.

A 5xx or an exception on a path the principal is *allowed* to reach is not an
access failure: the view ran. The test prints those after the run
("access matrix: N server error(s) on allowed paths"); each is a bug to file.
On a path the principal must not reach, an exception is a leak and fails.

Outcome rules: `denied` is 401, 403 or a redirect to `/erp/login/?next=`;
`error:<status>` is 5xx; anything else (200, 201, 204, 302 elsewhere, 400,
404, 405) is `allowed`, i.e. the gate let the request through.

### Classifying a new route

The test fails with the entry to paste:

```
1 route(s) are not classified. Add to ROUTE_CLASSES in core/tests/access_matrix_expectations.py ...:
    "erp-widget-report": "staff",  # /erp/widgets/report/ (widgets.erp_views.WidgetReportView)
```

Pick the class from the table above. If GET and POST differ, use a dict:
`{"get": "staff", "post": "perm:widgets.manage_widget"}`. `ROUTE_OPTIONS`
tunes a route: `methods` (keep a POST that would call a provider off the
matrix), `row` (`{"pk": "app.Model"}` when the view 404s or raises on an
unknown id before its gate), `params` (fixed values), `fresh_client` (the
route ends the session). A route whose answer the matrix cannot judge (a
credential exchange, a bare redirect) gets `skip:<reason>`.

### Adding a model override

A new viewset or model view fails until its model is listed:

```
    "widgets.Widget": {"row": "seed"},
```

`{"row": "seed"}` keeps the roles.md default; add `"api:<action>"` /
`"erp:<action>"` entries (or `"api"` / `"erp"` for a whole surface) where the
model's own rules differ, with a comment citing the doc they come from. Then
give `seed()` in the test one row of the model (a bare `objects.create` with
the required fields; every write the matrix sends is rolled back, so one row
serves every principal). If a row cannot be made cheaply, set
`"row": "skip:<reason>"`; the detail actions are then skipped and counted.

### Marking a known leak

```
    "widgets.Widget": {"row": "seed", "api:list": "staff;leak:portal:a:ISSUE-61"},
```

The matrix then *expects* the listed principal to get through, and fails when
the gap closes ("the gap has closed, remove the leak entry and close ISSUE-61").
A leak entry always names the issue that tracks it.
