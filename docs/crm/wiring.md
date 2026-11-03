# Wiring the CRM adapters in a deployment

This guide is for a deployment that connects real provider accounts to the
CRM adapters that ship with Fincas (Aircall, Gmail, ClickUp, Klaviyo, Google
Ads offline conversions). Every adapter is built and tested offline, against
invented fixtures; Fincas holds no provider credential. Connecting real
accounts is the deployment's job, in the order below.

What this guide does not repeat, and where to find it:

| Topic | Where |
|---|---|
| The Connected accounts page, keys and rotation, permissions, the "when something stops" table, and one section per adapter with its full field mapping | [adapters.md](adapters.md) (the operator notes) |
| The outbox consumer and adapter contracts, for anyone writing an adapter | [plugins.md](../plugins.md), sections 5c and 6d |
| Scheduled jobs and the outbox worker in general | [deployment.md](../deployment.md#scheduled-jobs) |

When this guide and the code disagree, the code wins: report the doc bug.

Contents: [Before you start](#before-you-start) ·
[1 Checklist](#1-per-deployment-checklist) ·
[2 Adapters](#2-per-adapter-wiring) ·
[3 Erasure](#3-erasure-expectations) ·
[4 NL reports](#4-nl-report-tools-in-the-assistant) ·
[5 First week](#5-first-week-runbook) ·
[6 Settings](#6-settings)

## Before you start

**Adapters are off by default.** A deployment that configures none of this
sees no integration traffic:

- **No adapter loads unless it is listed.** Each adapter is an add-on under
  `golden_erp/addons/` that loads only when its plugin class is in `GOLDEN_PLUGINS`.
- **An installed adapter does nothing without an enabled account.**
  `IntegrationAccount.enabled` defaults to `False`. A disabled account is not
  polled, its webhook URL answers 404, its adapter queues no new outbound work,
  and any of its rows already in the outbox end `cancelled`
  (`account_disabled`).
- **No key, no credential.** With `GOLDEN_SECRET_KEYS` empty nothing can be
  stored (the page and `integration_connect` refuse), and `manage.py check`
  passes as long as no credential exists.
- **The NL report API** (`/api/v1/crm/reports/…`) and its assistant tools are
  read-only and need no configuration beyond a key that may read them.

**Outbound webhooks are delivered from the outbox, never in the request.** If
your settings set `WEBHOOKS_DELIVER_ON_SIGNALS = True` (the packaged default is
`False`), **the outbox worker (1.2) is required**, adapters or not: without it,
webhook deliveries wait in the outbox and nobody receives them. `crm_emit_due`
sends `task_due`; its `task.due` webhook goes through the outbox like the
others.

When upgrading from a release before 0.4, a plain `migrate` adds the outbox
and integration-account tables (`core.0015_outboxevent`,
`webhooks.0003_webhookdeliverylog_delivery_id`, `core.0016_integration_accounts`).
The adapters add no migrations of their own.

## 1. Per-deployment checklist

Do these in order, once per deployment. 1.1 to 1.5 are safe before any adapter
is chosen and change nothing visible, with one exception: if your settings set
`WEBHOOKS_DELIVER_ON_SIGNALS = True`, starting the worker (1.2) delivers every
webhook that has waited in the outbox. 1.6 creates accounts, disabled. 1.7 has a
gate.

### 1.1 `GOLDEN_SECRET_KEYS` as a platform secret

1. Generate a Fernet key:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   (`manage.py generate_secrets --only GOLDEN_SECRET_KEYS` prints one too.)
2. Store it in the platform's secret store (on Fly:
   `fly secrets set GOLDEN_SECRET_KEYS=<key> -a <app>`), never in the image or a committed
   env file. The packaged base settings read it from the environment
   (comma-separated, **newest first**), so your settings needs no line for it.
3. Give it to **every process that runs Fincas code**: web, the outbox worker
   and the scheduler. The worker is the process that decrypts most credentials
   (every push and every inbound webhook runs there).
4. `manage.py check` must pass. It fails with `core.E041` on a malformed key
   (named by its position in the list) and with `core.E040` when an active
   credential was written under a key that is no longer in the list.
5. Back the key up with the same care as the database backups, and keep it with
   them. A restore without the key that wrote its credentials is a restore with
   every integration disconnected: there is no fallback derived from
   `SECRET_KEY`.

Rotation ([adapters.md](adapters.md#keys)):

1. `GOLDEN_SECRET_KEYS=<new>,<old>`, restart every process.
2. `manage.py rotate_secrets --dry-run` to see the counts, then
   `manage.py rotate_secrets`. Exit 1 means some rows were written under a key
   that is not configured.
3. `GOLDEN_SECRET_KEYS=<new>`, restart. `manage.py check` confirms.

**Trap:** dropping the old key before `rotate_secrets` has run, or restarting
only web. `check` then fails with `core.E040` (and the worker cannot decrypt);
put the old key back after the new one and run `rotate_secrets`.

Threat model: the keys protect a database dump, a replica or a backup, not a
reader of the process environment.

### 1.2 The outbox worker

Every adapter push, every inbound webhook and every Fincas webhook delivery is
a row in `core.OutboxEvent`, delivered by `manage.py process_outbox` with no
transaction open ([deployment.md](../deployment.md#process_outbox-always-on-when-webhooks-or-integrations-are-enabled)
covers the worker in general). Run **one** of:

- **A long-running process** running `python manage.py process_outbox --loop`
  from the same image as web. The repository's `docker-compose.prod.yml` has it as the
  `outbox` service. On Fly, add a process group beside web in the app's
  `fly.toml` and scale it to one machine:

  ```toml
  [processes]
    web = "<the existing web command>"
    outbox = "python manage.py process_outbox --loop"
  ```

  It never migrates. Several replicas may run at once (each row is claimed with
  a conditional update and a lease), but one is enough to start: a handler makes
  about one provider call per row (Google Ads batches up to 2,000) and never sleeps, and every adapter is held to its
  per-account rate limit, so more workers do not make a throttled provider
  faster.
- **A scheduler line**, every minute:
  `python manage.py process_outbox --once --max-seconds 50`.

The worker needs the same environment as web: database, `GOLDEN_SECRET_KEYS`,
and your settings (so `GOLDEN_PLUGINS` lists the same adapters). **A worker whose
`GOLDEN_PLUGINS` differs from web's** leaves the missing adapters' rows
unclaimed. `check_outbox` run with web's settings reports them as overdue
pending rows ("is process_outbox running?") although a worker is running; run
with the worker's settings, as rows of an unregistered consumer.

Retention: run `python manage.py process_outbox --once --prune --max-seconds 50`
once an hour (or add `--prune` to the scheduler line once an hour). It deletes
`done` rows after `GOLDEN_OUTBOX["retain_done_days"]` (7), done `*.inbound`
rows (they hold provider bodies) after `retain_inbound_days` (1) and
`cancelled` rows after `retain_cancelled_days` (7). `dead` rows stay until
requeued or cancelled.

Health: run `python manage.py check_outbox` (`--max-age-minutes`, default 30)
from the scheduler or the monitoring system, and alert on exit 1. It prints
counts per consumer and state, and fails on a `dead` row, a due `pending` row
waiting longer than the threshold (no worker), a `running` row whose lease
expired over 5 minutes ago, or rows of a consumer no longer registered (a
removed plugin). **Do not use it as a container liveness probe:** one dead row
would restart the worker forever without fixing anything. Without a worker
nothing is lost: rows wait as `pending`, and this check says so.

### 1.3 Scheduler lines

Beside `crm_emit_due` ([deployment.md](../deployment.md#scheduled-jobs)):

```cron
*/5 * * * *  python manage.py integrations_poll --max-seconds 240
17  * * * *  python manage.py integrations_sync klaviyo --reconcile      # only with Klaviyo
5   * * * *  python manage.py process_outbox --once --prune --max-seconds 50
*/5 * * * *  python manage.py check_outbox                               # alert on exit 1
0   * * * *  python manage.py check_erasure                              # alert on exit 1
* * * * *    python manage.py process_outbox --once --max-seconds 50     # only without the worker process
```

`integrations_poll` polls every enabled, unpaused account whose adapter polls
(once per connected user for Gmail); `--provider` and `--account` narrow it.
Pass `--max-seconds` explicitly: the default is 50 s, which a five-minute line
under-uses. **One line for all adapters:** a second, provider-specific
`integrations_poll --provider …` line beside it polls that provider twice.

### 1.4 Webhook ingress

Inbound webhooks arrive at one core route per account:

    https://<erp-host>/api/v1/integrations/<provider>/<account id>/webhook/

- **Public reachability.** The path must reach web from the provider's network.
  If the back office sits behind an allow-list or an auth proxy, exempt this
  path: it is anonymous and CSRF-exempt by design, and authenticates by the
  provider's signature.
- **Body size.** The endpoint refuses bodies over 1 MB (413). A proxy limit
  below 1 MB rejects legitimate deliveries first.
- **Signature secret = a credential.** Each webhook adapter verifies against
  the account's `webhook_secret` credential, in constant time. Store what the
  provider gives you as that credential, never as a setting.
- **Replay window.** When a provider signs a timestamp, a delivery older than
  300 s, or more than 30 s in the future, is 401. Keep server clocks synced.
  Every accepted delivery is deduplicated by `<account id>:<delivery id>` (or the
  body's sha256 when the provider sends no id) for as long as its done inbound
  row is kept (1 day), and by the adapter's own idempotent `ingest` after that.
  The adapters wired here sign no timestamp (ClickUp, Aircall) or carry their
  expiry in a signed token (Gmail), so for them the dedupe is the replay guard.
- **Answers.** 200 `{"accepted": true, "duplicate": <bool>}` as soon as the row
  is written; 401 on a bad signature or a stale timestamp (logged with a reason
  code, never the body); 404 for an unknown or **disabled** account, an account
  of another provider, or an adapter that is not installed (the same body for
  each). The work happens later, in `process_outbox`.

**Trap: a webhook registered before the account is enabled answers 404**, and
some providers stop calling a URL that keeps failing. Register the webhook,
store its secret and enable the account in one sitting (1.7), then check the
provider's own delivery log. Missed deliveries are picked up by the adapter's
poll (every webhook adapter here also polls).

OAuth (Gmail) needs a second public URL, the per-account callback registered
with Google:

    https://<erp-host>/erp/integrations/accounts/<account id>/callback/

Fincas builds it from the request (`build_absolute_uri`), so web must see the
original scheme and host behind the proxy: the packaged production settings
set `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`; keep your
settings from undoing it, and keep the host in `ALLOWED_HOSTS`. A callback that
comes out as `http://` does not match the one registered with Google.

### 1.5 Install the adapters

Add each adapter's plugin class to `GOLDEN_PLUGINS` in your settings and deploy
web, worker and scheduler together:

| Adapter | `GOLDEN_PLUGINS` entry | Section |
|---|---|---|
| Aircall | `golden_erp.addons.aircall.plugin.AircallPlugin` | 2.1 |
| Gmail | `golden_erp.addons.gmail.plugin.GmailPlugin` | 2.2 |
| ClickUp | `golden_erp.addons.clickup.plugin.ClickUpPlugin` | 2.3 |
| Klaviyo | `golden_erp.addons.klaviyo.plugin.KlaviyoPlugin` | 2.4 |
| Google Ads conversions (and the `attribution` key scope) | `golden_erp.addons.google_ads.plugin.GoogleAdsPlugin` | 2.5 |

**Never list `golden_erp.addons.fixture_crm`**: its providers are invented, for the test
suite only. Installing an adapter starts nothing; it only lets accounts of that
provider be created.

**Trap: never remove an adapter from `GOLDEN_PLUGINS` while an account of its
provider exists.** Every later erasure that touches its records (Gmail and
Aircall included, through their message and call refs) writes an erase row with
no handler, and `check_erasure` and `check_outbox` turn red on it. Reinstall
the adapter, or handle each such row by hand ([adapters.md](adapters.md#erasure)).

### 1.6 Per-company accounts (created disabled)

One account per provider per company; a second one is refused. For each:

1. **Create the account**, disabled: **Admin → Integrations → Connected
   accounts** (`/erp/integrations/#connected-accounts`), or

   ```bash
   python manage.py integration_connect <provider> --company <company uuid> \
       --settings-file settings.json
   ```

   The settings are validated by the adapter on save: an unknown key or a
   malformed value is refused with the reason.
2. **Store each credential** the adapter lists (section 2), one kind per call.
   On the shell the secret is read from **stdin**, never an argument (it would
   land in the shell history and the process list). Only the **first line** is
   read, so a JSON credential (`oauth_client`) must be on one line:

   ```bash
   python manage.py integration_connect <provider> --company <company uuid> \
       --credential api_key --subject "<label shown to staff>" < secret.txt
   ```

   A new credential of a kind revokes the previous one; nothing is overwritten.
   Fincas accepts any known credential kind here, not only the ones the adapter
   reads, so check the kind against its section 2 table.
   The page's fields are write-only. Gmail's per-user grants are not stored
   here: each user connects their own mailbox (2.2).
3. **Test connection** on the page (it calls the provider and spends rate-limit
   tokens). The result is stored for this company only, redacted against the
   account's secrets.

Who can do it: creating, configuring, storing credentials, testing, enabling and
resuming need `core.change_integrationaccount`, which the built-in `staff` role
does not have. Grant it to the people who wire integrations (a `GOLDEN_ROLES`
entry in your settings; see [roles.md](../roles.md)). Each user connects or disconnects their own Gmail grant
as plain staff.

### 1.7 Enable (gate: watch erasure first)

**Gate: erasure must be watched before anything is enabled.** Erasing a
person reaches every connected provider (section 3), but only through the
outbox, so a stuck erasure is silent unless something checks for it. Fincas has
**no startup check** that refuses an enable; this gate is the operator's to
keep. Schedule `manage.py check_erasure` (1.3, section 3) and alert on it before
the first account is enabled; it stays green until an erasure is stuck at a
provider.

Then, per account:

1. Register the webhook at the provider, if the adapter takes one (section 2
   names the events), and store its secret as `webhook_secret`.
2. Enable: the page, or
   `python manage.py integration_connect <provider> --company <uuid> --enable`.
3. Follow the first-week runbook (section 5).

**Order across adapters.** One at a time, each after the previous one's first
week is clean. Suggested, least to most outward effect: Aircall (inbound only,
writes nothing at Aircall), Gmail (inbound only, per user), ClickUp (writes
tasks), Klaviyo (writes consent state to a marketing tool), Google Ads (sends
conversions to an ad platform; dry run first).

### 1.8 The NL report tools

Section 4. Independent of the adapters; it can go first.

## 2. Per-adapter wiring

Every adapter also accepts the two core settings keys: `initial_since` (days the
first sync reaches back, 1–3650, default 90) and `rate_limit`
(`{"requests": n, "per_seconds": s}`, to raise the conservative default to the
provider plan's figure). The tables below list each adapter's own keys and
credential kinds, exactly as its `settings_keys` and `credential_kinds`
declare them.

**Before enabling any account, confirm the provider facts its section lists
against the provider's live API documentation.** Every adapter was built
offline; each value that could not be verified is isolated in `golden_erp/addons/<provider>/values.py`.
If a fact differs, do not enable: report it as an issue, so the fix lands upstream.

The first poll of every account (empty cursor) is a silent backfill: it reaches
back `initial_since` days, fires no signal and no webhook, and the adapter
moves engagement activity itself. Later polls are live.

### 2.1 Aircall

Inbound only: each Aircall call becomes one `call` communication. Operator
notes: [adapters.md](adapters.md#aircall).

| Settings key | Meaning |
|---|---|
| (none of its own) | only `initial_since` and `rate_limit` |

| Credential kind | Value |
|---|---|
| `api_key` | `<api_id>:<api_token>` from Aircall's API keys page, joined by a colon (sent as HTTP Basic) |
| `webhook_secret` | the token Aircall shows for the webhook you create |

- **At Aircall:** an API key pair; one webhook per company to
  `/api/v1/integrations/aircall/<account id>/webhook/` with the events
  `call.ended` and `call.commented`. Aircall puts the token in the body, not a
  header; Fincas compares it in constant time and drops it before storing.
- **Poll:** `integrations_poll` reads `GET /v1/calls` oldest first, as the
  backstop for missed webhooks. Default limit 50 requests per 60 s.
- **Writes:** `call` communications (answered → `completed`; missed,
  abandoned, voicemail, unanswered → `failed`). The party is whoever alone
  holds the external number; an unknown number stays party-less; no party is
  ever created. The agent is the one active staff profile with Aircall's
  `user.email`.
- **Recordings stay at Aircall.** `recording`, `voicemail` and `asset` are kept
  verbatim in the raw payload only (readable with
  `communications.view_raw_communication`); Fincas never requests or stores
  audio, and those links expire at Aircall. "Open in Aircall" is built from the
  call id.
- **Confirm before enabling** (`golden_erp/addons/aircall/values.py`): the body token field
  name (`token`); the event names; the calls listing's `from` / `order` / paging
  parameters, and that `from` filters on the call's start; the 10,000-record
  listing cap; the 60 requests per minute limit; the "open in Aircall" app URL.

### 2.2 Gmail

Each staff user connects **their own** mailbox with a read-only grant; Fincas
logs only mail that involves a known contact. Operator notes:
[adapters.md](adapters.md#gmail).

| Settings key | Default | Meaning |
|---|---|---|
| `exclude_labels` | `["CATEGORY_PROMOTIONS"]` | label ids or names never read; a list replaces the default. `DRAFT`, `SPAM`, `TRASH`, `CHAT` are never read whatever it says |
| `private_label` | `golden-private` | the label a user puts on mail Fincas must not read |
| `pubsub_service_account` | — | the push subscription's service-account email; **required** for push (without it every push is 401) |

| Credential kind | Stored by | Value |
|---|---|---|
| `oauth_client` | the operator (1.6) | JSON `{"client_id": "...", "client_secret": "..."}` of the Google OAuth client |
| `oauth_refresh`, `oauth_access` | Fincas, per user, on **Connect** | never entered by hand |
| `webhook_secret` | the operator | the Pub/Sub push subscription's **audience**, a long random value (push only) |

**At Google (Cloud console):**

1. An OAuth client of type *web application*, with the redirect URI
   `https://<erp-host>/erp/integrations/accounts/<account id>/callback/` and the
   single scope `https://www.googleapis.com/auth/gmail.readonly`.
2. **The OAuth consent screen.** Google classes Gmail read scopes as
   *restricted*. Confirm with Google's current rules before going live: an app
   whose user type is *Internal* (users of one Workspace organization) is the
   usual route for staff mailboxes; an *External* app needs Google's
   verification for restricted scopes, and while it stays in *Testing* its grants
   expire after a short period, which shows up in Fincas as grants
   disappearing from the account's list (Google answers `invalid_grant`). Fill in the support contact and the authorized domain of
   `<erp-host>`.
3. Optional, for near-real-time mail: a Pub/Sub topic and a push subscription to
   `https://<erp-host>/api/v1/integrations/gmail/<account id>/webhook/`, with
   authentication on, a dedicated service account (its email is
   `pubsub_service_account`) and the audience set to the `webhook_secret`
   value. **Fincas does not call Gmail's `users.watch`:** a push only arrives
   for a mailbox someone has put under watch on that topic (and renewed, as
   Google requires). Without push, the five-minute `integrations_poll` picks
   mail up; that is the supported baseline.

**Connecting.** After the account is enabled, each user opens Connected
accounts, clicks **Connect my account** and signs in with the Google account whose address
is their own login email (or a verified login of theirs). Any other mailbox is
refused and the grant revoked at Google at once. Shared mailboxes cannot be
connected. A user who is deactivated or moves to another company stops being
polled at once.

- **Poll:** per grant, `history.list` from the stored history id; the first
  sync lists `initial_since` days of mail. Rate limit 10 requests per second per
  account, shared by all its grants.
- **Writes:** `email` communications, only when someone in From, To or Cc
  (not the mailbox owner, not other staff) is held by exactly one contact of
  the company. Bcc never qualifies a message. Everything else is never read
  past its headers and writes nothing. One email copied into several staff
  mailboxes is one communication. Fincas's own document emails Bcc'd back to
  staff are skipped (the `X-Golden-Email-Log` header). Body capped at 100 kB;
  attachments are never stored (a count only). Email is linked to an
  engagement only when its thread already is.
- **Confirm before enabling** (`golden_erp/addons/gmail/values.py`): that `history.list`
  answers 404 for an expired history id (Fincas then re-lists from the last
  successful read, at least 7 days back); the
  header set `messages.get?format=metadata` returns; the Pub/Sub push token's
  claims (`iss`, `aud`, `email`, `email_verified`) and where the audience is
  set; the per-user quota against the 10 requests per second default.

### 2.3 ClickUp

Fincas pushes tasks to mapped ClickUp lists and mirrors them back. Operator
notes: [adapters.md](adapters.md#clickup) (settings JSON, task flags,
dead-letter codes).

| Settings key | Meaning |
|---|---|
| `lists` | `{"<list id>": {...}}`: the lists Fincas pushes to and mirrors from. Per list: `golden_id_field` (**required**, a text custom field Fincas writes its task id into), `engagement_field`, `status_map`, `push_status`, `kind` |
| `push_rules` | task kind → list id: Fincas tasks of that kind are pushed there |

| Credential kind | Value |
|---|---|
| `api_key` | a ClickUp API token, sent as the `Authorization` header as is (no `Bearer`) |
| `webhook_secret` | the secret ClickUp returns when the webhook is created |

- **At ClickUp:** per mapped list, the two text custom fields, and **one webhook
  scoped to that list** (not the workspace) to
  `/api/v1/integrations/clickup/<account id>/webhook/` for `taskCreated`,
  `taskUpdated`, `taskDeleted`, `taskStatusUpdated`, `taskAssigneeUpdated`,
  `taskDueDateUpdated`, `taskPriorityUpdated`, `taskMoved`. Each task event
  costs one `GET` of the task against the account's rate limit (default 100
  per minute), so a workspace-wide webhook spends it on tasks Fincas ignores.
  Every `push_status` value must map back through `status_map`, or the
  settings are refused.
- **Poll:** every mapped list on every `integrations_poll` run, as the backstop.
- **Ownership.** ClickUp owns title, description, status, due date,
  priority and assignee of every task it holds. Fincas owns the engagement,
  party, kind and communication, and never pushes them. A later ClickUp value
  that disagrees with the engagement link in Fincas is refused and flagged
  (`refused_engagement`). A task deleted in ClickUp is cancelled in Fincas (a
  done one stays done). Fincas never archives or deletes a ClickUp task; a task
  moved to an unmapped list pauses its sync until it comes back.
- **Writes:** `crm.Task` mirrors (through the task services, so `TaskEvent`
  rows and lifecycle webhooks follow), and ClickUp tasks for Fincas tasks whose
  `metadata["clickup.list_id"]` names a mapped list or whose kind matches a push
  rule. The task form has no list picker yet: `push_rules` is the way to route
  tasks.
- **Confirm before enabling** (`golden_erp/addons/clickup/values.py`): the `Authorization`
  header with the raw token; the `x-signature` header (HMAC-SHA256, hex, over
  the raw body); the event names above; the 100 requests per minute limit of
  your plan (raise `rate_limit` to it); that `GET /api/v2/list/{id}/task` with
  `order_by=updated&reverse=true` returns newest first (the poll's no-skip
  argument depends on it); the priority ids (1 urgent … 4 low); and whether
  ClickUp returns a pushed `description` unchanged in `markdown_description`.

### 2.4 Klaviyo

Fincas tells Klaviyo who may receive marketing email and pulls back who
stopped wanting it. Operator notes: [adapters.md](adapters.md#klaviyo) (the full
endpoint list).

| Settings key | Meaning |
|---|---|
| `list_id` | the Klaviyo list Fincas subscribes consenting people to; without it a subscribe dead-letters `no_list_id` (unsubscribes and suppressions still go through) |
| `segment_lists` | optional `{"saved_filter:<uuid>": "<list id>"}`: a tenant-visible saved filter on the parties list mapped to a Klaviyo list (membership only; may not be `list_id`) |
| `log_sends` | default `true`: log each received campaign email as a `marketing_email` communication |

| Credential kind | Value |
|---|---|
| `api_key` | a Klaviyo private API key: read events; write profiles, lists, subscriptions; data-privacy deletion |

No webhook: Klaviyo is polled only (events, every `integrations_poll` run; it
re-reads the hour before its cursor). Default rate limit 3 requests per second.

**Consent mapping.**

- Fincas pushes only what `can_contact(party, "email", "marketing", …)` allows
  for the party's primary address: a grant subscribes (with Fincas's own consent
  time); no consent, a withdrawal or a suppression unsubscribes a known profile,
  once; a party with no grant and no profile sends **no request at all**. Hard
  bounces and complaints are also added to Klaviyo's suppressions.
- Inbound: an unsubscribe becomes a Fincas withdrawal plus an `opt_out`
  suppression; a hard bounce a `hard_bounce` suppression; a spam complaint a
  withdrawal plus a `complaint` suppression. Soft bounces write nothing.
- **A provider subscribe is never a grant.** "Subscribed to Email Marketing"
  in Klaviyo writes no `ConsentRecord`. `integrations_sync klaviyo --reconcile`
  reports them as `provider subscribes ignored` (a running total); record a real grant through
  the portal or the ERP form for anyone who should get marketing.

**Order of enabling (trap).** Nothing is pushed until the account's first poll
has run, so Klaviyo's existing opt-outs are in Fincas before Fincas subscribes
anyone; pushes before it are cancelled `waiting_for_first_poll` (not an alarm)
and the first reconcile after it is a full one. Keep `integrations_poll`
running when you enable. Consent and suppression rows send no signal, so the
hourly `integrations_sync klaviyo --reconcile` line (1.3) is what pushes them.
After any import of people, run
`python manage.py integrations_sync klaviyo --reconcile --full` once: imports
run silent and push nothing.

- **Confirm before enabling** (`golden_erp/addons/klaviyo/values.py`; the full list is in
  [adapters.md](adapters.md#klaviyo)): the API revision `2024-10-15` and the
  `Klaviyo-API-Key` header; the subscribe job's `consented_at` with
  `historical_import: true`; unsubscribe by profile id alone (for an old
  address); the profile `subscriptions` read; the suppression, list
  relationship, events and data-privacy deletion endpoints; the metric names;
  the `$message` and `Subject` properties; the plan's rate limit.

### 2.5 Google Ads offline conversions (and attribution)

Uploads one conversion per won engagement that came from a Google click.
Operator notes: [adapters.md](adapters.md#google-ads-offline-conversions).

| Settings key | Meaning |
|---|---|
| `customer_id` | the Google Ads customer the conversions belong to |
| `login_customer_id` | optional: the manager account to call through |
| `conversion_actions` | `{pipeline key: conversion action id}`; a win in an unmapped pipeline uploads nothing (`pipeline_unmapped`) |
| `click_window_days` | 1–90, default 90: older clicks upload nothing (`click_too_old`) |
| `ad_user_data_consent` | the `consent.adUserData` sent with every conversion; default `UNSPECIFIED` |

| Credential kind | Value |
|---|---|
| `api_key` | the Google Ads developer token |
| `oauth_client` | JSON `{"client_id": "...", "client_secret": "..."}` |
| `oauth_refresh` | the offline refresh token of a Google user with access to the customer, obtained outside Fincas and stored with `integration_connect --credential oauth_refresh` (account-level, not a per-user grant) |

- **`ad_user_data`.** Fincas records marketing-email consent, not
  consent to share ad-click data with Google, so by default it asserts nothing
  (`UNSPECIFIED`). Set `GRANTED`, `DENIED` or `UNKNOWN` only when the
  deployment's own consent basis for its market supports it. Fincas sends no
  name, email, phone or address: the click id, the conversion action, the time,
  the engagement number as order id, the currency and, when the engagement has
  an amount, its value (never zero, never converted).
- **No poll, no webhook.** Uploads run from the outbox, batched per company up
  to 2,000 per request; a reopen after an upload sends a retraction.
- **Dry run first:** `python manage.py ads_conversions --account <uuid>
  --dry-run` prints what the pending rows would send and why the others send
  nothing.
- **Clicks in.** A won engagement uploads only when its touchpoints carry a
  Google click id. Fincas captures it from the lead intake (`landing_url` /
  `click_id` on the lead API) and from visits posted to
  `POST /api/v1/crm/touchpoints/`. For the site's server or tag manager, create
  a service account with `crm.add_touchpoint`, pinned to the company, and a key
  in the `attribution` scope (registered by this plugin), which can call that
  path and nothing else:

  ```bash
  python manage.py create_api_key --name attribution --role <a service role with crm.add_touchpoint> \
      --scope attribution --company <company uuid>
  ```

- **Confirm before enabling** (`golden_erp/addons/google_ads/values.py`): the API version
  (`v25`; Google sunsets each major version about a year after release:
  re-check, then yearly); the 2,000 conversions per request limit; the consent
  enum (`UNSPECIFIED`, `UNKNOWN`, `GRANTED`, `DENIED`); the retryable error
  codes and the "already exists / already retracted" codes Fincas treats as
  done.

## 3. Erasure expectations

Erasing a person (`manage.py erase_party`, the ERP danger zone, or the parties
API) reaches every connected provider. The operator view, with the per-provider
operations and what to do when one does not finish, is [adapters.md](adapters.md#erasure); What to expect:

- **The local erasure never waits for a provider.** Inside the erasure
  transaction Fincas reads the provider ids it wrote (still in clear), deletes
  or tombstones queued work about the person (found by party and, in the same
  tenant, by every task, communication and touchpoint it scrubbed), runs every
  installed adapter's `purge_parties` hook (Google Ads drops pending uploads),
  and writes one `<provider>.erase` outbox row per provider that holds
  something (ClickUp, Klaviyo). `process_outbox` does the provider work later.
- **Every account of the tenant is swept, enabled or not.** A disabled
  account's provider still holds what Fincas wrote while it was on.
- **Fincas never cancels an erase row.** Throttled, rejected or down: it
  retries with the outbox backoff and goes `dead` after 20 attempts. On a
  disabled or deleted account it waits (`account_disabled` /
  `account_missing`) and retries hourly. A partial failure resumes without
  re-sending finished records. **Do not `outbox cancel` a `.erase` row** unless
  you erased those records at the provider by hand: a cancelled row drops out
  of `check_erasure`.
- **A new account does not inherit a deleted one's erase rows.** A row written
  for another account (deleted, then a new one created) ends `dead` with
  `account_changed`, and requeueing it ends the same way: the new account
  cannot reach the old one's records. Erase them at the provider by hand, then
  cancel the row.
- **Tombstones block re-sync.** Mirror refs (tasks, messages, calls,
  touchpoints) keep their ids with `metadata.erased`; the person's own refs
  are hashed; addresses and numbers get erasure suppressions. Later mail,
  calls, profiles and tasks for the person write nothing, and a ClickUp task
  created later on the erased person's deal is not mirrored (`erased_party`).
- **Watch it** with `manage.py check_erasure [--company <uuid>]
  [--max-age-hours 24] [--json]`: it lists unfinished erase rows (provider,
  party id, records left, attempts, error code; never a provider id) and exits
  1 on a dead row, a row with no handler (adapter uninstalled) or one older
  than the threshold. `result.counts` of an erasure carries
  `provider_erasures`, `mirror_refs_flagged`, `outbox_deleted` and
  `outbox_scrubbed`; the audit entry carries the `erasure_id`.
- **Never delete an account, disable it.** Deletion is refused while the tenant
  holds that provider's refs not yet flagged erased, or an unfinished erase row
  (the admin hides the button), because erasure reaches a provider through its account. **Never
  re-point an existing account at another workspace** (an API key of another
  ClickUp workspace or Klaviyo account): refs carry no account, so records
  written under the old one would read as already gone.
- **Known limit:** a Klaviyo subscribe that a worker sent just before the
  erasure committed can land after the deletion job, leaving a profile at
  Klaviyo under an id Fincas never learns. Check Klaviyo by address after an
  erasure if that matters for the request.


**What erasure cannot reach: the deployment's duty, per provider.**

| Provider | What Fincas does on erasure | What stays at the provider (handle it with them) |
|---|---|---|
| Aircall | nothing to send (Fincas writes nothing there); later calls from the erased number are skipped | call logs, recordings, voicemails |
| Gmail | nothing to send; later mail involving the erased address is never ingested | the mail itself, in every connected mailbox |
| ClickUp | renames each task Fincas scrubbed to `Erased` and clears its description and engagement field; never deletes the task | the task itself, its comments, attachments and activity history |
| Klaviyo | files a data-privacy deletion job per profile Fincas holds a ref for | anything Klaviyo keeps outside the deleted profile (its own event history) |
| Google Ads | deletes the person's pending and dead upload rows; sends nothing (no personal data was sent) | conversions already uploaded (click id, order id, value) |

## 4. NL report tools in the assistant

The CRM reports are also available as read-only JSON:

| Route | Report | Service-account permissions |
|---|---|---|
| `GET /api/v1/crm/reports/` | the catalog: parameters, allowed values, column names, definitions | `crm.view_engagement` |
| `GET /api/v1/crm/reports/funnel/` | funnel by source (`attribution=engagement` or `first_touch`) | `crm.view_engagement` |
| `GET /api/v1/crm/reports/stage-duration/` | stage duration (`pipeline` key or id) | `crm.view_engagement` |
| `GET /api/v1/crm/reports/follow-ups/` | follow-up completion | `crm.view_task` and `crm.view_engagement` |
| `GET /api/v1/crm/reports/attribution/` | multi-touch attribution, engagements won in the period | `crm.view_touchpoint` and `crm.view_engagement` |

Parameters: `company`, then `period` (`today`, `week`, `month`, `quarter`,
`year`; default `month`) or `date_from` + `date_to` (`YYYY-MM-DD`, together, at
most 366 days). An unknown parameter or value is 400 with `code`, `param` and
`allowed`. Periods are local days in the site time zone, as on the report
pages. Each answer carries `summary`, a deterministic sentence the ERP writes
(counts, dates, currency codes and keys only; never a name, email or phone).

**Tenancy.** A principal pinned to one company reads that company (naming
another is 404). A superuser or the shared key must name `company` unless only
one company exists (400 `company_required`). Give the assistant its own service
account and key, **pinned to the company** (`create_api_key --name <name>
--role <role> --company <uuid>`), whose role carries exactly the permissions
above. A non-superuser key with no company gets no `company_required` error:
every report comes back empty with a `no_company` note, which looks like "no
data yet".

**The assistant side** (`erp_assist`, stdio MCP server in
`erp_assist/mcp_adapter.py`, run with `python -m erp_assist.mcp_adapter`): it
reads the ERP base URL and the key from the environment variables
`erp_assist/core/client.py` (`ERPConfig.from_env`) names. MCP tools:
`list_crm_reports`, `crm_funnel_report`, `crm_stage_report`,
`crm_follow_up_report`; each report tool requires `company`. The attribution
report has no MCP tool of its own yet: it is reachable through the core
function `erp_assist.core.crm.crm_report("attribution", …)` and the HTTP route.
Allow-list those tools in the assistant's configuration; they are read-only.

## 5. First-week runbook

For each adapter, in the order of 1.7. Write each line in the deployment's log
with the date.

**Day 0 (enable one adapter, one company).**

1. `python manage.py check` passes (no `core.E040` / `E041`).
   `python manage.py check_erasure` exits 0 and is on the scheduler.
2. Confirm the provider facts of its section 2 entry.
3. Test connection on the page: green.
4. Register the webhook (if any), store `webhook_secret`, enable (1.7).
5. Within ten minutes: run `python manage.py integrations_poll --provider
   <provider> --account <uuid> --max-seconds 240` once by hand and read its
   summary line (accounts, items, changed, failed, paused skipped); the
   account's row on the page shows no error code and no `paused until …`
   badge; for a webhook adapter, the provider's delivery log shows 200s;
   `check_outbox` exits 0 and its counts show the provider's rows
   (`<provider>.inbound`, and the push consumers) reaching `done`.
6. Klaviyo only: the first poll has finished before you expect any push.
   Google Ads only: `ads_conversions --account <uuid> --dry-run` before the
   first won engagement, and read its skipped reasons.

**Daily, days 1–7.**

- [ ] `check_outbox` exit 0 (or every failure explained). It is red on any
      dead row, so dead letters are read the same day.
- [ ] `check_erasure` exit 0: no erasure stuck at a provider.
- [ ] The account's row on the page shows no error code and no `paused until …`.
- [ ] Spot-check five records: a call, an email, a task, a profile or a
      conversion lands where section 2 says, on the right party, and nothing
      lands for people Fincas does not know.
- [ ] Gmail: every user who connected is still listed under the account (a
      grant Google revoked, or one the user disconnected, disappears from the
      list; the account's error code may read `GrantRevoked`).

**Dead letters.**

```bash
python manage.py outbox list --state dead [--consumer <provider>.<name>]   # ids, consumer, error code; no payloads
python manage.py outbox requeue <id> ...          # or: outbox requeue --consumer <name>, after the fix
python manage.py outbox cancel <id> ...           # give up on rows that no longer matter (never a .erase row, section 3)
```

`requeue_outbox <id> …` is the same as `outbox requeue`. Requeue works on
`dead` rows only; a `cancelled` row is final. The error code says what to fix:
an adapter's own codes are in its [adapters.md](adapters.md) section (ClickUp
`status_unmapped`, `list_not_mapped`, `duplicate_task`; Klaviyo `no_list_id`;
Google Ads the provider's error code). Fix the cause (settings, credential),
then requeue by consumer. `duplicate_task` is the exception: delete the
duplicate in ClickUp and cancel the row.

**Paused accounts.** A 429 pauses the account for the provider's `Retry-After`
(60 s without one, at most 6 h) and shows `rate_limited`: wait, or **Resume
now** on the page to retry earlier (do not rotate a good key). A 401 pauses it
for an hour and shows `credentials_rejected`: storing a new account-level
credential lifts that pause. For Gmail, a 429 or 401 on one user's mailbox
pauses that user's grant only. While paused, rows wait (`account_paused`) and polls skip the account;
nothing is lost.

**Stopping an adapter.** There is no operator pause: to stop an account,
**disable** it (the page, or `integration_connect <provider> --company <uuid>
--disable`). Know what that costs before you do it:

- rows already queued for it end `cancelled` (`account_disabled`) and cannot be
  requeued;
- changes made in Fincas while it is disabled are not queued at all, so they
  are not sent when you enable it again. Klaviyo catches up with
  `integrations_sync klaviyo --reconcile --full`; Fincas edits to ClickUp
  tasks made meanwhile are not sent (ClickUp's values win on the next inbound
  copy), and a Fincas task created meanwhile reaches ClickUp only on its next
  save of a synced field; Google Ads wins made while disabled never
  upload;
- erasures keep queueing work for it: an erase row for a disabled ClickUp or
  Klaviyo account waits (`account_disabled`), and after 24 h `check_erasure`
  exits 1 until the account is enabled again. Disabling erases nothing at the
  provider;
- its webhook URL answers 404, so the provider may stop calling it; the poll
  backstop reads what was missed once it is enabled again (Gmail re-lists at
  least 7 days if its history id has expired).

To stop deliveries without losing rows, stop the worker instead: rows wait as
`pending` and `check_outbox` turns red after its threshold.

**Disconnecting a Gmail user.** The user clicks **Disconnect my account**
(disconnecting someone else's grant needs `core.change_integrationaccount`):
Fincas revokes the grant, stops polling that mailbox and asks Google to revoke
it through the outbox (best effort). Mail already logged stays.

## 6. Settings

Django settings (your settings may override them; `GOLDEN_SECRET_KEYS` belongs in
the platform's secrets, not in a settings file):

| Setting | Default | Meaning |
|---|---|---|
| `GOLDEN_SECRET_KEYS` | `[]` (read from the `GOLDEN_SECRET_KEYS` environment variable, comma-separated) | Fernet keys for `IntegrationCredential`, newest first. Empty: no credential can be stored |
| `GOLDEN_OUTBOX` | `{"retain_done_days": 7, "retain_inbound_days": 1, "retain_cancelled_days": 7}` | retention for `process_outbox --prune`; `dead` rows are never pruned |

The adapters also depend on `GOLDEN_PLUGINS` (which adapters load;
default `[]`) and `WEBHOOKS_DELIVER_ON_SIGNALS` (default `False`; when `True`,
outbound webhooks need the outbox worker, see [Before you start](#before-you-start)).

`GOLDEN_FIXTURES_WRITE=1` is an environment variable for the project's own test
suite (it rewrites fixture expectations); never set it in a deployment.

Per-account configuration is not a Django setting: it is
`IntegrationAccount.settings`, listed per adapter in section 2.
