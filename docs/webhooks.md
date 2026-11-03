# Webhook Integration Guide

Fincas includes an enterprise outbound webhook notification engine. It delivers near real-time HTTP POST notifications to external services whenever core lifecycle events occur (e.g. order placed, payment succeeded, fulfillment dispatched).

---

## 0. From the back office

Admin → Integrations → **Webhook subscriptions** (since v0.4.0) lists the
tenant's subscriptions with their last delivery, adds one (the signing secret
is shown once, in the confirmation message, and never printed again), enables
or disables one, and sends a test delivery (`webhook.ping`, one attempt).
Writes need `core.change_integrationaccount`, like every other write on that
page; validation is the API serializer's, so `event@n` pins work the same way.
Every ping, from the screen or `POST .../subscriptions/<id>/ping/`, carries
`X-Golden-Delivery-Id` (a fresh id stored as the log row's `delivery_id`; the
API answer's `delivery_id` is that header, and `log_id` the row).

## 1. Supported Events

Subscribers can listen for specific event types, wildcard all events (`*`), or namespace wildcards (`order.*`):

Signal-driven delivery is **off** unless `WEBHOOKS_DELIVER_ON_SIGNALS = True`
(see `docs/plugins.md` section 5, and section 5 below for how delivery runs).
Each event is built by the `webhooks` outbox consumer (`webhooks/outbox.py`)
from a lifecycle signal, inside the transaction that made the fact; `data`
keys are the latest version (section 1.1). Every payload builder lives in
`webhooks/events.py`.

| Event Name | Trigger Context | `data` keys |
| :--- | :--- | :--- |
| `order.placed` | An order is created live, or leaves `incomplete` | `order_id`, `order_number`, `total_amount`, `status` |
| `order.status_changed` | `Order.status` changes | `order_id`, `order_number`, `old_status`, `new_status` |
| `payment.completed` | A non-refund payment first reaches captured/settled | `payment_id`, `payment_number`, `amount`, `payment_method`, `status` |
| `inventory.adjusted` | A warehouse stock quantity changes | `sku`, `warehouse_code`, `delta`, `current_quantity`, `reason` |
| `customer.registered` | A customer is created (portal, ERP, API, import) | v2: `customer_id`, `email`, `name`, `customer_type`, `schema_version`, `party_id`, `account_holder_party_id`, `company_id` (v1: the first four) |
| `party.created` | A party is created through `save()` (not the backfill) | `schema_version`, `party_id`, `company_id`, `kind`, `display_name`, `is_active`, `merged_into_party_id`, `updated_at` |
| `party.updated` | An existing party is saved (once per save) | as `party.created`, plus `updated_fields` (sorted column names) |
| `party.merged` | A party merge commits | `schema_version`, `survivor_party_id`, `merged_party_ids`, `merge_id`, `company_id` |
| `party.unmerged` | A party merge is undone | `schema_version`, `survivor_party_id`, `restored_party_ids`, `merge_id`, `company_id` |
| `party.erased` | A party's erasure commits | `schema_version`, `party_id`, `company_id` |
| `engagement.created` | An engagement is created (ERP, API, lead intake, a quote's thread; not the backfills) | the engagement keys below |
| `engagement.stage_changed` | An engagement's stage moves (move, change of pipeline, reopen at another stage) | engagement keys, `from_pipeline_key`, `from_stage_key`, `to_stage_key`, `change_id`, `changed_at`, `change_source`, `changed_by_id` |
| `engagement.won` | An engagement is won (including a quote's conversion) | engagement keys, `closed_at`, `change_id`, `changed_at`, `change_source`, `changed_by_id` |
| `engagement.lost` | An engagement is lost | engagement keys, `closed_at`, `loss_reason`, `change_id`, `changed_at`, `change_source`, `changed_by_id` |
| `engagement.reopened` | A won or lost engagement returns to open | engagement keys, `previous_status`, `change_id`, `changed_at`, `change_source`, `changed_by_id` |
| `task.created` | A CRM task is created | the task keys below |
| `task.completed` | A task is completed | the task keys |
| `task.due` | A task crosses its due time (`manage.py crm_emit_due`, once per due time) | the task keys |
| `communication.logged` | A communication is logged (a manual log, a golden-sent document email, lead intake) | `schema_version`, `communication_id`, `company_id`, `kind`, `direction`, `status`, `provider`, `party_id`, `engagement_id`, `occurred_at` |

**Engagement keys** (every `engagement.*` event, in this order):
`schema_version`, `engagement_id`, `company_id`, `number`, `pipeline_key`,
`stage_key`, `status`, `party_id`, `owner_id`, `source`, `origin`,
`expected_value` (a decimal string, `null` when unknown, never `"0"`),
`currency`, `expected_close_date`, `created_at`. **Task keys**:
`schema_version`, `task_id`, `company_id`, `engagement_id`, `party_id`,
`kind`, `status`, `priority`, `assignee_id`, `due_at`, `all_day`, `due_tz`,
`completed_at`, `source`, `created_at`.

The CRM events (new in 0.4) carry ids, registry keys and timestamps only: an
engagement's title names its party, and task titles, communication subjects,
bodies, summaries, loss notes, competitor names and addresses are free text,
so none of them leave the server in a webhook; fetch the record through the
staff API when you need it. `owner_id`, `assignee_id` and `changed_by_id`
are UserProfile ids. Key idempotent handling on `change_id` for the
`engagement.*` transitions and on `(task_id, due_at)` for `task.due`
(`crm_emit_due` sends once per due time; a rescheduled task fires again).
`task_assigned` and `task_reopened` have no webhook event. Like every
event here, the payload is built when the event is delivered (after the
commit), so it shows the record as committed: two moves in one transaction
both report the final `stage_key`.

IDs are strings (UUIDs); an absent link is `null`. `party.merged` is the
event integrations must handle: anything keyed on a merged party's id must
re-key to `survivor_party_id`. `party.erased` obliges every integration that
copied the party's personal data to delete it. `party.updated` can arrive
more than once for one business edit (a customer save can save its parties
twice, and both deliveries carry the final state); deduplicate on
`(party_id, updated_at)`. `updated_fields` lists the columns the save
*wrote*, not a diff of what changed: a full save lists nearly every column.

The `party.*` events are new in 0.3. A subscription on `*` starts receiving
them on upgrade; `party.updated` follows every customer edit that touches a
mirrored field, so list explicit events if that is too much.

### 1.1 Payload versions

Each event's `data` has a schema version. A new version only **adds** keys;
an older version is the newer payload with those keys removed, so a
subscriber that stays on it receives exactly the bytes it did before.

| Event | Version | `data` keys | Status |
| :--- | :--- | :--- | :--- |
| `customer.registered` | **2** (0.3.0) | `customer_id`, `email`, `name`, `customer_type`, `schema_version` (`2`), `party_id`, `account_holder_party_id`, `company_id` | latest |
| `customer.registered` | 1 | `customer_id`, `email`, `name`, `customer_type` (no `schema_version`) | supported in 0.3; deprecated in 0.4; removed in 0.5 |
| every other event (including the CRM `engagement.*`, `task.*` and `communication.logged` events, pinnable as `@1`) | 1 | see the table above | latest |

**Choosing a version.** Append `@<n>` to the event name in
`subscribed_events`:

```json
{"subscribed_events": ["customer.registered@1", "order.*"]}
```

- `customer.registered@1` pins v1, for a receiver with a strict schema
  validator. `@2` pins v2.
- A bare name, `*` or `prefix.*` gets the **latest** version, so a new
  subscription receives v2.
- An explicit pin wins over a wildcard on the same subscription
  (`["*", "customer.registered@1"]` gets every event, and v1 of this one).
- Wildcards cannot be pinned (`customer.*@1` is refused), and neither can an
  unknown event or version. Two different pins for one event are refused.
- The envelope (`event`, `X-Golden-Event`) always carries the plain event
  name, never the `@<n>` suffix.

**Existing subscriptions keep v1 during 0.3.** A subscription created before
the versioning cutoff that names `customer.registered` without a version (or
reaches it through a wildcard) still receives v1 in 0.3.x. "Existing" means
its `created_at` is earlier than `WEBHOOKS_VERSIONING_CUTOFF` (ISO 8601;
default `2026-10-08T00:00:00Z`). A deployment that upgrades to 0.3 later
should set the setting to its own upgrade time, so subscriptions it created
on an older release count as existing. Pin `@2` to opt in early.

**Deprecation timeline for `customer.registered@1`:**

| Release | Unpinned existing subscription | `@1` pin |
| :--- | :--- | :--- |
| 0.3.x | v1 | v1 |
| 0.4.x | v2 | v1, with a warning logged on every delivery |
| 0.5.0 | v2 | removed: pin `@2` or drop the pin before upgrading |

---

## 2. Webhook Envelope Format

Every webhook payload is sent as an HTTP `POST` with `Content-Type: application/json`.

```json
{
  "event": "order.placed",
  "timestamp": "2026-09-09T15:00:00.123456+00:00",
  "subscription_id": "b34e5678-1234-5678-1234-567812345678",
  "data": {
    "id": 1042,
    "order_number": "SO-01042",
    "customer_id": 88,
    "total_amount": "1250.00",
    "currency": "USD",
    "line_items_count": 3
  }
}
```

### HTTP Headers

Every request includes standard delivery headers:

| Header | Description |
| :--- | :--- |
| `X-Golden-Event` | The event name (e.g., `order.placed`). |
| `X-Golden-Signature` | HMAC-SHA256 signature in the format `sha256=<hex_digest>`. |
| `X-Golden-Timestamp` | ISO-8601 time of this attempt (identical to the envelope `timestamp`; a retry carries a new one); used for replay protection. |
| `X-Golden-Delivery` | UUID identifying the webhook subscription. |
| `X-Golden-Delivery-Id` | UUID of this delivery (one event to one subscription), **the same on every retry**. Drop a request whose id you have already processed. Absent on a ping. |
| `User-Agent` | `Fincas-Webhooks/1.0` |

---

## 3. Cryptographic Signature Verification

Each subscription is assigned an HMAC secret key (or you can provide your own during creation). You should verify the incoming `X-Golden-Signature` header to ensure the payload was sent by Fincas and was not altered in transit.

**Signed material:** the signature covers **only the raw request body bytes** (the UTF-8 encoded JSON envelope). No headers participate in the MAC — there is no header canonicalization or ordering — so always verify against the untouched body before parsing JSON.

### Python Example

```python
import hmac
import hashlib

def verify_golden_webhook(raw_payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify incoming Fincas webhook request.
    
    :param raw_payload_bytes: The raw request body bytes (do NOT parse JSON before verifying).
    :param signature_header: The value of 'X-Golden-Signature' header.
    :param secret: Your subscription secret string.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
        
    expected_mac = hmac.new(
        secret.encode("utf-8"),
        msg=raw_payload_bytes,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    expected_header = f"sha256={expected_mac}"
    return hmac.compare_digest(expected_header, signature_header)
```

### Node.js / JavaScript Example

```javascript
import crypto from 'crypto';

export function verifyGoldenWebhook(rawBodyBuffer, signatureHeader, secret) {
  if (!signatureHeader || !signatureHeader.startsWith('sha256=')) {
    return false;
  }

  const expectedSignature = 'sha256=' + crypto
    .createHmac('sha256', secret)
    .update(rawBodyBuffer)
    .digest('hex');

  // Use timing-safe equality check to prevent timing attacks
  const expectedBuffer = Buffer.from(expectedSignature, 'utf8');
  const actualBuffer = Buffer.from(signatureHeader, 'utf8');

  if (expectedBuffer.length !== actualBuffer.length) {
    return false;
  }

  return crypto.timingSafeEqual(expectedBuffer, actualBuffer);
}
```

### Go Example

```go
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

func VerifyGoldenWebhook(rawPayload []byte, signatureHeader string, secret string) bool {
	if !strings.HasPrefix(signatureHeader, "sha256=") {
		return false
	}
	receivedHex := strings.TrimPrefix(signatureHeader, "sha256=")

	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(rawPayload)
	expectedHex := hex.EncodeToString(mac.Sum(nil))

	return hmac.Equal([]byte(receivedHex), []byte(expectedHex))
}
```

### Replay Protection (`X-Golden-Timestamp`)

Every delivery carries an `X-Golden-Timestamp` header holding the dispatch time in ISO-8601 format with timezone (the same value as the envelope `timestamp` field). Receivers should reject deliveries whose timestamp is outside the freshness window:

- **Window:** 300 seconds maximum age.
- **Clock skew:** a timestamp is accepted only if it is at or before now and no older than 300 seconds (`0 <= now - timestamp <= 300`). Timestamps from the future are rejected outright — there is no future skew tolerance. A timestamp without timezone information is assumed to be UTC.
- **Stale/missing/garbled timestamps:** verification fails cleanly (no exception). Receivers should respond with a `4xx` status (e.g. `401`); Fincas treats `4xx` as a permanent failure and does not retry.

Because the timestamp header is not covered by the HMAC, combine this check with the signature verification: accept a delivery only when both the signature and the freshness check pass.

---

## 4. Managing Subscriptions via REST API

Subscriptions can be created and managed dynamically through `/api/webhooks/subscriptions/`.

### Registering a Subscription

```bash
curl -X POST https://erp.yourdomain.com/api/webhooks/subscriptions/ \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "target_url": "https://api.yourdomain.com/webhooks/orders",
    "subscribed_events": ["order.placed", "order.status_changed"],
    "is_active": true,
    "company": 1
  }'
```

Response includes auto-generated `secret` for HMAC verification:
```json
{
  "id": "b34e5678-1234-5678-1234-567812345678",
  "target_url": "https://api.yourdomain.com/webhooks/orders",
  "subscribed_events": ["order.placed", "order.status_changed"],
  "secret": "<the signing secret, shown once>",
  "is_active": true,
  "created_at": "2026-09-09T15:00:00Z"
}
```

### Rotating a Subscription Secret

Each subscription stores two secrets:

- `secret` — the current signing secret.
- `previous_secret` — the prior secret, kept valid during the rotation window.

When `previous_secret` is set, a signature made with **either** secret verifies. Verification always tries the current `secret` first and falls back to `previous_secret` only on mismatch. The old secret remains valid **until it is explicitly cleared** — the rotation window is not time-bounded; there is no automatic expiry.

To rotate:

1. Generate a new secret and move the current one to `previous_secret`.
2. Update your receiver to accept signatures made with the new secret.
3. Once all in-flight deliveries signed with the old secret have been consumed, clear `previous_secret` to retire it.

Administration: the REST API exposes `secret` but **not** `previous_secret`, so the rotation window is administered via the Django admin (`WebhookSubscription` edit page) or a direct model update — there is no dedicated rotation endpoint.

---

## 5. Delivery, Retries and Delivery Logs

Deliveries run from the **outbox** (`core.OutboxEvent`,
`core/outbox/`), never in the request that made the
change:

1. The save that makes a fact (an order placed, a task completed) writes one
   `webhooks` outbox row in its own transaction, holding the event's payload
   as it was at that moment. A rollback leaves no row; a crash after the
   commit loses nothing. (Before the outbox the payload was read after the
   commit, so a later change to the same row in the same transaction showed;
   now it does not: the event says what was true when it happened.)
2. `manage.py process_outbox` (a long-running `--loop` service, or `--once
   --max-seconds 50` every minute; `docs/deployment.md` section 8) expands
   that row into one `webhooks.deliver` row per matching subscription. A dead
   endpoint retries on its own and never delays another subscriber.
3. Each run makes **one** HTTP attempt per due row (10 s timeout, no sleep),
   with no database transaction open:
   - `2xx` is delivered;
   - `4xx` fails at once and is not retried (dead letter);
   - `5xx`, a timeout or a connection error is retried after
     `min(30 s × 2^(attempt−1), 6 h)` ±20 %: 30 s, 1 min, 2 min … about 2 h,
     ten attempts over about 4.3 hours, then the row is dead.
4. A subscription deactivated, deleted, moved to another company or no
   longer listening to the event when a delivery runs is skipped (the row is
   cancelled); this is re-checked at every attempt.

Delivery is **at least once**: a worker that dies after the request but
before recording it means a repeat. Every repeat carries the same
`X-Golden-Delivery-Id`, so a receiver that remembers the ids it has
processed can drop it. Events are not ordered across deliveries; use the
payload's own timestamps (`updated_at`, `changed_at`, `occurred_at`).

- Fincas records **every attempt** in `WebhookDeliveryLog`
  (`/api/webhooks/logs/`), with `delivery_id` naming the delivery it belongs
  to. `2xx` responses are `SUCCESS`; anything else records `FAILURE` with the
  status code, error message and duration.
- Dead deliveries stay listed: `manage.py outbox list --state dead`, and
  `manage.py check_outbox` exits 1 while any exists. After fixing the
  endpoint, `manage.py requeue_outbox <id>` (or `outbox requeue --consumer
  webhooks.deliver`) sends them again.
- The **ping** (`POST /api/webhooks/subscriptions/<id>/ping/`) stays
  synchronous, as an operator's explicit check: one attempt with a 5 s
  timeout and no retry.
