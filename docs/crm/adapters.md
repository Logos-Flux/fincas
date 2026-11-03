# Connected accounts: operator notes

Operator notes for the five adapters (ClickUp, Klaviyo, Gmail, Aircall, Google
Ads) and the machinery they share: what to configure, what each adapter
owns, and what to do when something does not finish.

To connect real accounts step by step (secrets, the outbox worker, scheduler
lines, webhook ingress, enabling, the first week), follow
[wiring.md](wiring.md).

## What lives where

| Thing | Where | Notes |
|---|---|---|
| Which adapters exist | `GOLDEN_PLUGINS` | an adapter is a `BasePlugin` add-on returning `get_crm_adapters()` |
| Encryption keys | `GOLDEN_SECRET_KEYS` (environment) | Fernet keys, comma-separated, **newest first** |
| A company's account for a provider | `core.IntegrationAccount` | one per provider per company; disabled until a staff user enables it |
| Its settings (list ids, maps) | `IntegrationAccount.settings` | JSON, no secrets, validated by the adapter |
| Its secrets | `core.IntegrationCredential` | Fernet ciphertext; never shown, serialised or logged |
| A user's OAuth grant (Gmail) | `IntegrationCredential` with an `owner` | per profile, revocable on its own |

Nothing else about a provider is a setting. Client ids, tokens, list ids and
webhook secrets are per company, entered under **Admin → Integrations →
Connected accounts** (`/erp/integrations/#connected-accounts`) or with
`manage.py integration_connect`.

## Keys

Generate a key:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Set `GOLDEN_SECRET_KEYS=<key>` in the environment. With no key, no
credential can be stored (the page says so); there is **no fallback derived
from `SECRET_KEY`**, unlike `llmhub`'s `LLMHUB_FERNET_KEY`.

Rotate:

1. `GOLDEN_SECRET_KEYS=<new>,<old>` and restart every process (web, outbox
   worker, scheduler).
2. `manage.py rotate_secrets` re-encrypts every credential, revoked ones
   included, under `<new>`. It prints counts only; exit 1 means some rows
   were written under a key that is not configured.
3. `GOLDEN_SECRET_KEYS=<new>` and restart. `manage.py check` confirms: it
   fails with **`core.E040`** while any active credential was written under
   a key that is no longer configured (and `core.E041` on a malformed key).

Threat model: the keys protect a database dump, a replica or a backup. They
do not protect against anyone who can read the process environment.

## Permissions

- Viewing the Integrations page: staff.
- Creating an account, editing settings, storing a credential, enabling,
  resuming: `core.change_integrationaccount` (not part of the built-in
  `staff` role; grant it to the people who wire integrations).
- Connecting or disconnecting **your own** OAuth grant: staff. Disconnecting
  someone else's: `core.change_integrationaccount`.

Every view works inside the caller's own company. An account of another
company is a 404.

## Enabling an account

1. Add the account (it starts disabled), paste its settings JSON, store its
   credentials (write-only fields).
2. **Test connection.** The result is stored for this company only and is
   redacted against every secret of the account.
3. Enable it. Polling providers are picked up by the next
   `integrations_poll`; webhook providers need the account's webhook URL
   registered at the provider:
   `https://<host>/api/v1/integrations/<provider>/<account id>/webhook/`.

## Scheduler lines

    */5 * * * *  manage.py integrations_poll --max-seconds 240
    * * * * *    manage.py process_outbox --once --max-seconds 50   # or the compose outbox service
    0 * * * *    manage.py check_erasure                            # exit 1: an erasure stuck at a provider

`integrations_poll` skips disabled and paused accounts. Webhooks and pushes
run in `process_outbox` (docs/deployment.md section 8).

## When something stops

| Symptom on the page | Meaning | Fix |
|---|---|---|
| `paused until …`, `rate_limited` | the provider answered 429; golden waits `Retry-After` (at most 6 h) | nothing; **Resume now** to retry earlier |
| `paused until …`, `credentials_rejected` | the provider answered 401 | store a new credential (that lifts the pause) |
| a user's grant disappeared | the provider answered `invalid_grant` on refresh | that user connects again; other users are unaffected |
| `account_disabled` cancelled rows in `manage.py outbox list --state cancelled` | work reached a disabled account | enable it; cancelled rows are final (`outbox requeue` moves `dead` rows only), so redo the work that still matters (Klaviyo: `integrations_sync klaviyo --reconcile --full`) |
| `check` fails with `core.E040` | a key left `GOLDEN_SECRET_KEYS` too early | put it back after the new one, `rotate_secrets`, remove it |

## The fixture provider

`golden_erp/addons/fixture_crm/` registers two invented providers, `fixture` and
`fixture_oauth`, against `*.fixture.example.com`. They exist for golden's
tests and as the pattern each real adapter copies (layout, fixture cases,
contract test). **Never list `golden_erp.addons.fixture_crm` in a deployment's
`GOLDEN_PLUGINS`.**

## Erasure

Erasing a person (`manage.py erase_party`, the ERP danger zone, or the
parties API) reaches every connected provider.

**What happens.** Inside the erasure transaction, golden reads the provider
ids it wrote about the person, while they are still in clear, and writes one
`<provider>.erase` outbox row per provider that holds something. Nothing calls
a provider during the erasure: the local erasure completes even when a
provider is down. `process_outbox` then does the provider work:

| Provider | What golden wrote there | Operation at the provider | Tombstone that blocks re-sync |
|---|---|---|---|
| ClickUp | tasks it created or mirrors | `PUT /api/v2/task/<id>` renames the task `Erased` and clears its description; `DELETE /api/v2/task/<id>/field/<engagement field>` clears the engagement number. A create golden sent but never recorded is first searched for by the golden id field. The task is never deleted | the task's ref keeps its id with `metadata.erased`; inbound for it is skipped |
| Klaviyo | the profile | `POST /api/data-privacy-deletion-jobs/`, one per profile id (Klaviyo deletes the profile and its history) | the profile ref is hashed and flagged; the address has an erasure suppression |
| Gmail | nothing | none (no row) | the address has an erasure suppression; scrubbed messages are never rewritten |
| Aircall | nothing | none (no row) | call refs flagged; the number has an erasure suppression |
| Google Ads | conversions carrying a click id and an engagement number, no personal data | none (no row); uploads still pending or dead for the person are deleted (`purge_parties`) | touchpoint click ids are scrubbed and touchpoint refs flagged; the handler cancels `erased` for a row about an erased party |

Every account of the tenant is swept, enabled or not: a disabled account's
provider still holds what golden wrote while it was on.

Queued work about the person (a profile sync, a task push, a pending upload)
is deleted, found by the party and, in the same tenant, by every task,
communication and touchpoint erasure scrubbed (a `clickup.task` row has no
party; it is found by its task). Finished rows keep only `{"erased": true}`.
Then each installed adapter's `purge_parties` hook drops queued work it keeps
elsewhere.

**Watching it.** Run `manage.py check_erasure` from the scheduler next to
`check_outbox`. It lists every unfinished erase row (provider, party id,
provider records left, attempts, error code; never a provider id) and exits 1
when one is dead, has no handler, or is older than `--max-age-hours`
(default 24).

**When a provider erasure does not finish.**

- *Provider down, throttled, credentials rejected:* the row retries with the
  outbox backoff; it goes `dead` after 20 attempts. Fix the cause, then
  `manage.py outbox requeue <id>`. Records already done are not sent again.
- *Account disabled or deleted:* the row is not cancelled; it waits
  (`account_disabled` / `account_missing`) and retries hourly. For a disabled
  account, enable it and requeue if the row went dead meanwhile. Disabling an
  account does not erase anything at the provider. A row whose account was
  deleted ends `dead` with `account_changed` once another account of that
  provider exists (requeueing it ends the same way): erase the records by hand
  at the provider, then `manage.py outbox cancel <id>`.
- *Adapter uninstalled:* the row has no handler (`check_outbox` reports it
  orphaned). Erase the records by hand at the provider, then
  `manage.py outbox cancel <id>`.
- *Deleting an account:* refused while golden still holds records of that
  provider in the tenant (or an erase row is unfinished); the admin shows no
  delete button. Disable the account instead. Never point an existing account
  at another workspace: records written under the old one would read as
  already deleted.
- *`collector_failed` in the report:* the adapter's collector raised during
  the erasure; the row holds every ref of that provider on the person's rows
  instead. Read the ERROR log line, fix the adapter, and let the row run.

What a provider holds that golden never sent is not reachable from golden;
the deployment handles it with each provider:

| Provider | What golden does on erasure | What stays at the provider (handle it with them) |
|---|---|---|
| Aircall | nothing to send (golden writes nothing there); later calls from the erased number are skipped | call logs, recordings, voicemails |
| Gmail | nothing to send; later mail involving the erased address is never ingested | the mail itself, in every connected mailbox |
| ClickUp | renames each task golden scrubbed to `Erased` and clears its description and engagement field; never deletes the task | the task itself, its comments, attachments and activity history |
| Klaviyo | files a data-privacy deletion job per profile golden holds a ref for | anything Klaviyo keeps outside the deleted profile (its own event history) |
| Google Ads | deletes the person's pending and dead upload rows; sends nothing (no personal data was sent) | conversions already uploaded (click id, order id, value) |

## ClickUp

`golden_erp/addons/clickup/`. Install with `GOLDEN_PLUGINS = ["golden_erp.addons.clickup.plugin.ClickUpPlugin"]`.

**Who owns what.** ClickUp owns every task it holds: title, description,
status, due date, priority and assignee. Golden owns the links ClickUp does
not know (engagement, party, kind, communication) and never pushes them. The
engagement custom field is read once, when golden first mirrors a task; a
later ClickUp value that disagrees with golden's link is **refused**: golden
keeps its link, the task gets the flag `refused_engagement` in its
`clickup.flags`, and a warning is logged. A golden-id field pointing at
another task is refused the same way (`refused_golden_id`).

**Credentials** (Connected accounts): `api_key` (a ClickUp API token, sent
as the `Authorization` header as is) and `webhook_secret` (the secret ClickUp
returns when the webhook is created).

**Settings:**

```json
{
  "lists": {
    "<ClickUp list id>": {
      "golden_id_field": "<text custom field id>",
      "engagement_field": "<text custom field id>",
      "status_map": {"to do": "open", "in progress": "in_progress", "complete": "done"},
      "push_status": {"cancelled": "cancelled"},
      "kind": "todo"
    }
  },
  "push_rules": {"call": "<ClickUp list id>"}
}
```

| Key | Meaning |
|---|---|
| `lists` | the lists golden pushes to and mirrors from; anything else in ClickUp is ignored |
| `golden_id_field` | **required**: a text custom field on the list; golden writes its task id there on create and finds a lost create by it |
| `engagement_field` | a text custom field holding an engagement number (`EN-00042`); a ClickUp task carrying one is mirrored into golden on that engagement |
| `status_map` | ClickUp status name → golden status. Unmapped statuses fall back by type: open → `open`, custom → `in_progress`, done/closed → `done` |
| `push_status` | golden status → ClickUp status name, when `status_map` has no single entry for it. A golden status with neither dead-letters the push (`status_unmapped`) |
| `kind` | the golden task kind of tasks mirrored from this list (default `todo`) |
| `push_rules` | task kind → list: golden tasks of that kind are pushed there |

A golden task is pushed when its `metadata["clickup.list_id"]` names a mapped
list, or a push rule matches its kind. Tasks in a mapped list are mirrored
into golden only if they carry golden's id or an engagement number.

**Webhook.** Create one ClickUp webhook **per mapped list** (scoped to the
list, not the workspace) for the events `taskCreated`, `taskUpdated`,
`taskDeleted`, `taskStatusUpdated`, `taskAssigneeUpdated`,
`taskDueDateUpdated`, `taskPriorityUpdated` and `taskMoved`, pointing at the
account's webhook URL. Each task event costs one `GET` of the task against
the account's rate limit (100 a minute by default), so a workspace-wide
webhook spends it on tasks golden ignores. ClickUp signs no timestamp, so a
replay is caught by the body hash. `integrations_poll` reads the mapped
lists every run as the backstop.

A task moved in ClickUp to a list golden does not map pauses its sync
(`clickup.left_at`) until it comes back. Removing a list from `lists` stops
its pushes the same way; golden never archives a ClickUp task.

**Task flags** (`metadata["clickup.flags"]`, recomputed on every inbound copy):

| Flag | Meaning |
|---|---|
| `refused_engagement`, `refused_golden_id` | ClickUp changed a golden-owned field; golden kept its value |
| `engagement_unresolved` | the engagement number matched no engagement of this company |
| `assignee_unmatched` | no ClickUp assignee is active staff here (by email); the golden task is unassigned |
| `title_truncated` | the ClickUp name was longer than 255 characters |
| `status_unmapped` | neither `status_map` nor the status type gave a golden status |
| `status_refused` | ClickUp moved a done task to a cancelled status; golden keeps the completion |

A task deleted in ClickUp is cancelled in golden (a done one stays done),
gets `clickup.deleted_at`, and stops syncing both ways.

**Erasure.** The `clickup.erase` row renames the ClickUp task to `Erased`
and clears its description and engagement field; it never deletes the task.
That covers the person's own tasks and tasks whose text named them. A task
created in ClickUp after the erasure that names the erased person's deal
number is skipped (`erased_party`), never mirrored. See [Erasure](#erasure).

**Dead-letter codes** (`manage.py outbox list --state dead`): `status_unmapped` (add the status
to `status_map` or `push_status`, then requeue; the other values were
already sent), `list_not_mapped` (the list left `lists`), `duplicate_task`
(two creates raced: delete the duplicate in ClickUp). A disabled account's rows end `cancelled`, not dead.

## Klaviyo

`golden_erp/addons/klaviyo/`. **Golden owns consent**: it
tells Klaviyo who may receive marketing and pulls back who stopped wanting
it. A subscribe made in Klaviyo is never golden consent.

    GOLDEN_PLUGINS = [..., "golden_erp.addons.klaviyo.plugin.KlaviyoPlugin"]

| Setting | Meaning |
|---|---|
| `list_id` | the Klaviyo list golden subscribes consenting people to (required before anyone is pushed; a sync without it dead-letters `no_list_id`) |
| `segment_lists` | optional, `{"saved_filter:<uuid>": "<list id>"}`: a **tenant** saved filter on the parties list mapped to a Klaviyo list. Members with a grant join it; leavers are removed from it |
| `log_sends` | default `true`: log each received campaign email as a `marketing_email` communication. `false` keeps the timeline free of sends (unsubscribes, bounces and complaints are still pulled) |
| `initial_since`, `rate_limit` | core keys (first-sync horizon in days, default 90; requests per period, default 3 per second) |

| Credential | Meaning |
|---|---|
| `api_key` | a Klaviyo private API key with read access to events and write access to profiles, lists and subscriptions, and data-privacy deletion |

Scheduler line (beside the core ones above; the core `integrations_poll`
line already polls Klaviyo, so do not add a `--provider klaviyo` poll next to
it):

    17 * * * *   manage.py integrations_sync klaviyo --reconcile

The reconcile is what pushes consent and suppression changes (those rows send
no signal); it only enqueues, `process_outbox` delivers. **Nothing is pushed
until the account's first poll has run** (Klaviyo's opt-outs come in first):
enable polling with the account. Pushes before it are cancelled
(`waiting_for_first_poll`, not an alarm), and the first reconcile after it
is a full one, so nothing is lost. The events poll re-reads the hour before
its cursor; events surfacing later than that are missed. After importing
people, run `integrations_sync klaviyo --reconcile --full` once: imports run
silent and push nothing on their own. Its output reports **`provider subscribes
ignored`**, a running total of subscribes made in Klaviyo without a golden
grant. Record a real grant for them (portal, ERP form) if they should get
marketing; golden will not infer one.

**Provider facts to confirm before enabling** (golden built this offline;
all are in `golden_erp/addons/klaviyo/values.py`): the API revision `2024-10-15` and the
`Klaviyo-API-Key` auth header; the endpoints `profile-import`,
`profile-subscription-bulk-create-jobs` (subscribe with `consented_at` and
`historical_import: true`: golden sends its own consent time),
`profile-subscription-bulk-delete-jobs` (unsubscribe by email, and **by profile id
alone** for a profile of an old address: if Klaviyo needs an email there, the
refusal is recorded on that ref as `unsub_failed` and the profile stays
subscribed; the rest of the party's sync still runs), `profiles/<id>`
(`additional-fields[profile]=subscriptions`, read once when golden adopts an
existing profile, and its `consent`, `timestamp` and `suppression[].reason`
values),
`profile-suppression-bulk-create-jobs`, `lists/<id>/relationships/profiles`
(POST and DELETE: segment membership without touching consent), `events` (filter, `include=metric,profile`, `page[cursor]`) and
`data-privacy-deletion-jobs`; the metric names `Received Email`, `Opened
Email`, `Clicked Email`, `Unsubscribed`, `Bounced Email` (with a `Bounce Type`
property of `hard`), `Marked Email as Spam`, `Subscribed to Email Marketing`;
the `$message` and `Subject` event properties; and the plan's rate limit
(raise `rate_limit` to it).

**Erasure.** The `klaviyo.erase` row files one data-privacy deletion per
profile golden wrote (a 404 counts as already gone); a retry after a rate
limit sends only the profiles still to do. What Klaviyo holds that golden
never sent (its own event history beyond the profile deletion) is the
deployment's to handle with Klaviyo. See [Erasure](#erasure).

## Gmail

`golden_erp.addons.gmail.plugin.GmailPlugin`. Each staff user
connects **their own** mailbox with a read-only grant; golden logs the mail
that involves a known contact as email on the contact's timeline.

**What it logs.** A message is logged only when someone in From, To or Cc —
not the mailbox owner, not another staff member — is a known contact
(exactly one contact holds the address). Bcc never counts. Everything else
(personal mail, newsletters, internal threads) is never read past its
headers and never stored. Also skipped: drafts, spam, trash, chats and promotions (or the
labels you list), anything a user labels `golden-private`, golden's own
document emails Bcc'd back to sales, and anything involving an erased
person. One email in several staff mailboxes is logged once.

**Setting it up:**

1. In Google Cloud: an OAuth client (web application) with the redirect URI
   `https://<host>/erp/integrations/accounts/<account id>/callback/` and the
   `gmail.readonly` scope.
2. Add the `gmail` account under Connected accounts and store its
   `oauth_client` credential as
   `{"client_id": "...", "client_secret": "..."}`.
3. Settings (all optional):

   | Key | Default | Meaning |
   |---|---|---|
   | `exclude_labels` | `["CATEGORY_PROMOTIONS"]` | label ids or names never read; a list replaces the default (drafts, spam, trash and chats are never read, whatever this says) |
   | `private_label` | `golden-private` | the label a user puts on mail golden must not read |
   | `pubsub_service_account` | — | the push subscription's service account; **required** for push: pushes from any other identity are refused |
   | `initial_since` | 90 | days the first sync of a mailbox reaches back |

4. Enable it. Each user then clicks **Connect** and signs in with the Google
   account whose address is their own login email (or a verified login of
   theirs); any other mailbox is refused.
5. Optional, for near-real-time mail: a Pub/Sub topic that Gmail `watch`
   publishes to, and a push subscription to
   `https://<host>/api/v1/integrations/gmail/<account id>/webhook/` with
   authentication on, its **audience** set to a long random value and a
   dedicated service account. Store the audience as the account's
   `webhook_secret` and the service account's email as
   `pubsub_service_account`. Without push, the
   five-minute `integrations_poll` picks mail up.

**When something stops.**

| Symptom | Meaning | Fix |
|---|---|---|
| a user's grant is no longer listed under the account (the account's error may read `GrantRevoked`) | Google answered `invalid_grant` (the user revoked access, or the grant expired), or the user disconnected | that user clicks **Connect my account** again; other users are unaffected |
| "that mailbox is not yours" on connect | the Google account is not the user's own address | connect with the user's own mailbox |
| a mailbox stopped after a user left | grants of deactivated users, or of users moved to another company, are not polled | the new owner connects their own mailbox |
| pushes refused (401) | `pubsub_service_account` unset, the token's audience is not the `webhook_secret`, or it is not from that service account | fix the subscription or the settings |

Erasure: golden writes nothing into Gmail, so erasing a person has nothing
to delete there; the mail itself stays in the users' mailboxes, which is the
deployment's to handle with Google.

## Aircall

`golden_erp/addons/aircall/` (`GOLDEN_PLUGINS += ["golden_erp.addons.aircall.plugin.AircallPlugin"]`).
Inbound only: each Aircall call becomes one `call` communication.

**Account.** Settings: none of its own (`initial_since` and `rate_limit` as
for every account). Credentials:

| Kind | Value |
|---|---|
| `api_key` | `<api_id>:<api_token>` from Aircall's API keys page, joined by a colon (sent as HTTP Basic auth) |
| `webhook_secret` | the token Aircall shows for the webhook you create |

**Webhook.** Create one Aircall webhook per company pointing at the
account's URL (`/api/v1/integrations/aircall/<account id>/webhook/`) with the
events `call.ended` and `call.commented`. Aircall sends its token in the JSON
body, not a header: golden compares it in constant time and drops it before
storing the event. There is no signed timestamp, so a replay is caught by the
outbox key (`call.ended:<call id>`, `call.commented:<call id>:<newest comment id>`)
and, after the inbound row is pruned, by `ingest` itself (same call, same
payload: nothing written). Other events are acknowledged and skipped.

**Poll.** `integrations_poll` reads `GET /v1/calls` oldest first from the
cursor, as the backstop for missed webhooks. The first poll goes back
`initial_since` days (default 90) without announcing anything. A call still
in progress holds the cursor (at most 24 h) so the next poll re-reads it.

**What a call becomes.**

| Aircall | golden |
|---|---|
| answered | `status=completed`, `metadata.aircall.outcome=answered`; moves the linked engagement's last activity |
| missed, abandoned, voicemail, unanswered outbound | `status=failed`, outcome `missed` / `abandoned` / `voicemail` / `not_answered` |
| external number | a participant in E.164; the party is whoever alone holds that number. An unknown number stays party-less (no party is created) |
| the Aircall line | the other participant |
| `user.email` | `performed_by`: the one **active** staff profile of the company with that email; otherwise empty with `metadata.aircall.agent_unmatched` |
| comments, tags | `summary`, `metadata.aircall.tags` |
| recording, voicemail, asset | **raw payload only** (`communications.view_raw_communication`). Golden never downloads or opens them; the links expire at Aircall |

**Erasure.** Golden writes nothing to Aircall, so erasing a person sends
nothing there. A call from an erased person's number, or a call erasure
already scrubbed, is skipped without writing anything. Recordings and call
logs held by Aircall are the deployment's to delete with Aircall.

**Facts to confirm at wiring time** (`golden_erp/addons/aircall/values.py`): the token
field name, the event names, the calls listing's `from` / `order` / paging
parameters (and that `from` filters on the call's start), its 10,000-record cap, the
60 requests per minute limit, and
the app URL used for "open in Aircall" (`ExternalRef.source_url`).

## Google Ads offline conversions

`golden_erp.addons.google_ads.plugin.GoogleAdsPlugin`. One account per company, provider `google_ads`.

- **Settings:** `{"customer_id": "123-456-7890", "conversion_actions":
  {"sales": "<conversion action id>"}}`, optionally `login_customer_id` (a
  manager account), `click_window_days` (1–90, default 90) and
  `ad_user_data_consent` (`UNSPECIFIED` by default; `GRANTED`, `DENIED` or
  `UNKNOWN` only when the deployment's own consent basis says so).
- **Credentials:** `api_key` = the developer token, `oauth_client` =
  `{"client_id", "client_secret"}`, `oauth_refresh` = the offline refresh
  token of a user with access to the customer.
- **Check first:** `manage.py ads_conversions --account <uuid> --dry-run`
  prints what the pending rows would send and why the others send nothing.
- **Visits:** give the site's server or tag manager a service account with
  `crm.add_touchpoint`, pinned to the company, and an `attribution`-scoped
  key (`manage.py create_api_key … --scope attribution`); it can post
  `POST /api/v1/crm/touchpoints/` and nothing else.
- **In `manage.py outbox list --consumer google_ads.conversion`:** cancelled rows carry the
  reason (`no_click`, `click_too_old`, `pipeline_unmapped`, `needs_review`,
  `not_won`, `erased`); dead rows carry Google's error code. A
  `needs_review` row is a re-win after a retraction: upload it by hand in
  Google Ads if the account's rules allow.
