"""Google Ads constants (docs/crm/phase6-adapters.md 3.5).

Provider facts golden cannot verify offline live here, so the downstream
wiring guide can list each one to confirm against Google's documentation
before an account is enabled: the API version, the endpoint paths, the
per-request conversion limit, the consent enum and which partial-failure
error codes are worth a retry.
"""

from __future__ import annotations

PROVIDER_ID = "google_ads"
ENVELOPE_VERSION = 1
DOCS = "See docs/crm/phase6-adapters.md 3.5 and docs/crm/adapters.md."

#: Checked 2026-10-09 against developers.google.com/google-ads/api/docs/sunset-dates
#: (v25 sunsets August 2027; v22 sunset 2026-10-07). Google sunsets each major
#: version about a year after release: re-check before enabling, then yearly.
API_VERSION = "v25"
BASE_URL = "https://googleads.googleapis.com"
UPLOAD_PATH = "/{version}/customers/{customer_id}:uploadClickConversions"
ADJUST_PATH = "/{version}/customers/{customer_id}:uploadConversionAdjustments"
TOKEN_URL = "https://oauth2.googleapis.com/token"
#: Conversions per upload request (Google documents 2,000).
BATCH_LIMIT = 2000

#: ``ad_user_data`` consent values Google accepts on a click conversion.
CONSENT_VALUES = ("UNSPECIFIED", "UNKNOWN", "GRANTED", "DENIED")
#: The default (OP #2246): golden records marketing-email consent, not
#: consent to share ad-click data with Google, so it asserts nothing. An
#: account overrides it with ``settings["ad_user_data_consent"]``.
DEFAULT_CONSENT = "UNSPECIFIED"
DEFAULT_CLICK_WINDOW_DAYS = 90

#: Partial-failure error codes retried through the outbox; any other code
#: dead-letters its row (the provider refused that conversion).
RETRYABLE_ERRORS = frozenset({
    "CONCURRENT_MODIFICATION", "INTERNAL_ERROR", "TRANSIENT_ERROR", "RESOURCE_EXHAUSTED",
    "DEADLINE_EXCEEDED",
})

#: Partial-failure codes meaning Google already holds this conversion or
#: retraction (a retry after a lost response): the row is done and the ref is
#: recorded, so a later reopen can still retract it. Confirm the names
#: against the release in use (ConversionUploadError /
#: ConversionAdjustmentUploadError).
ALREADY_DONE_ERRORS = frozenset({
    "CLICK_CONVERSION_ALREADY_EXISTS", "DUPLICATE_ORDER_ID", "CONVERSION_ALREADY_RETRACTED",
})

#: Settings keys an account carries (``IntegrationAccount.settings``).
SETTINGS_KEYS = frozenset({
    "customer_id", "login_customer_id", "conversion_actions", "click_window_days",
    "ad_user_data_consent",
})
#: Credential kinds: ``api_key`` holds Google's developer token; the OAuth
#: client and the account's offline refresh token (of the user who
#: authorised uploads) mint an access token per batch.
CREDENTIAL_KINDS = frozenset({"api_key", "oauth_client", "oauth_refresh"})

#: ``ExternalRef`` of an uploaded conversion: ``source_id`` is the order id
#: (the engagement number, Google's dedupe key).
REF_OBJECT_TYPE = "conversion"

CONSUMER = f"{PROVIDER_ID}.conversion"
UPLOAD, RETRACT = "upload", "retract"

#: Why a won engagement uploads nothing (the outbox row is ``cancelled``
#: with this code, and ``ads_conversions --dry-run`` reports it).
NO_CLICK = "no_click"
CLICK_TOO_OLD = "click_too_old"
PIPELINE_UNMAPPED = "pipeline_unmapped"
NEEDS_REVIEW = "needs_review"
NOT_WON = "not_won"
ERASED = "erased"
NO_ORDER_ID = "no_order_id"
NOT_UPLOADED = "not_uploaded"
#: A conversion uploaded with no value (null ``expected_value``).
VALUE_UNKNOWN = "value_unknown"
