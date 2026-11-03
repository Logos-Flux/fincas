"""Aircall facts golden cannot verify offline (docs/crm/phase6-adapters.md 7).

Everything here is taken from Aircall's public API documentation at the
time of writing and is listed for the downstream to confirm before enabling
an account (docs/crm/phase6-downstream.md, step 9).
"""

from __future__ import annotations

PROVIDER = "aircall"
#: Newest envelope version the reader accepts (the Aircall API v1 call object).
SCHEMA_VERSION = 1
BASE_URL = "https://api.aircall.io"
DOCS = "See docs/crm/adapters.md#aircall."

#: ``ExternalRef.object_type`` of a call (``communications.ingest.object_type_for("call")``).
OBJECT_CALL = "call"

# -- REST (Basic auth "<api_id>:<api_token>", stored as one api_key credential) --
CALLS_PATH = "/v1/calls"
PING_PATH = "/v1/ping"
#: The listing's ``from`` filter is assumed to apply to the call's start
#: (``started_at``); confirm it is not a separate creation time.
#: Calls per page (Aircall allows up to 50).
PER_PAGE = 50
#: Aircall serves at most this many records through one paginated listing;
#: a sweep stops there and the next run starts from the last call seen.
MAX_LISTING = 10_000
#: A call still in progress holds the poll cursor back (so the backstop
#: re-reads it once it ends) for at most this long.
MAX_HOLD_SECONDS = 24 * 3600
#: Conservative default (Aircall documents 60 requests per minute per company).
RATE_REQUESTS = 50
RATE_PER_SECONDS = 60

# -- webhooks --------------------------------------------------------------
# Aircall puts a shared token in the JSON body ("token"); there is no
# signature header and no signed timestamp. The adapter compares the token
# with the account's webhook_secret and strips it from the stored envelope.
TOKEN_FIELD = "token"
#: Webhook events the adapter reads; any other event is acknowledged and skipped.
CALL_EVENTS = frozenset({"call.ended", "call.commented"})
#: The event name the poll gives its items (not an Aircall event).
POLL_EVENT = "calls.list"

# -- call object -----------------------------------------------------------
#: ``status`` of a call that is over; anything else is still in progress.
FINAL_STATUS = "done"
#: ``missed_call_reason`` values that mean the caller hung up first.
ABANDONED_REASONS = frozenset({"short_abandoned", "abandoned_in_ivr", "abandoned_in_classic"})
#: ``summary`` cap (the joined comments).
SUMMARY_MAX = 4000

#: "Open in Aircall": the app's page for one call (confirm at wiring time).
CALL_APP_URL = "https://dashboard.aircall.io/conversations/{call_id}"

# Outcomes stored in ``metadata.aircall.outcome``.
ANSWERED = "answered"
MISSED = "missed"
ABANDONED = "abandoned"
VOICEMAIL = "voicemail"
NOT_ANSWERED = "not_answered"
