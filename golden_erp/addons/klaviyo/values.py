"""Klaviyo provider facts, isolated (docs/crm/phase6-adapters.md section 7).

Golden builds and tests this adapter offline against invented fixtures. Every
value below is a provider detail golden cannot verify without a live account;
``docs/crm/adapters.md`` ("Klaviyo") lists them for the downstream to confirm
against Klaviyo's documentation before enabling an account.
"""

from __future__ import annotations

PROVIDER = "klaviyo"
#: Newest envelope version the readers accept. It names the pinned API revision
#: below: a recording made against a newer revision is a newer envelope and is
#: refused by ``require_known_version``, never misread.
SCHEMA_VERSION = 1
#: The one Klaviyo API revision this adapter speaks (sent as the ``revision`` header).
API_REVISION = "2024-10-15"
BASE_URL = "https://a.klaviyo.com"
DOCS = "See docs/crm/adapters.md#klaviyo."

#: ``Authorization: Klaviyo-API-Key <private key>``.
AUTH_SCHEME = "Klaviyo-API-Key"
CONTENT_TYPE = "application/vnd.api+json"

# Endpoints (relative to BASE_URL).
PROFILE_IMPORT = "/api/profile-import/"                     # upsert by email (rule 4: natural key)
SUBSCRIBE = "/api/profile-subscription-bulk-create-jobs/"   # consent + list membership
UNSUBSCRIBE = "/api/profile-subscription-bulk-delete-jobs/" # withdraw email marketing
SUPPRESS = "/api/profile-suppression-bulk-create-jobs/"     # do-not-email at Klaviyo
LIST_PROFILES = "/api/lists/{list_id}/relationships/profiles/"  # DELETE: leave a list (3b)
EVENTS = "/api/events/"
PROFILE = "/api/profiles/{profile_id}/"                     # GET with additional-fields[profile]=subscriptions
DELETION = "/api/data-privacy-deletion-jobs/"               # erasure (section 4)

#: Events page size and the include that names each event's metric and profile.
EVENTS_PAGE_SIZE = 100
EVENTS_INCLUDE = "metric,profile"
EVENTS_SORT = "datetime"
#: Each poll re-reads this much before its cursor (events can surface late).
EVENTS_OVERLAP_SECONDS = 3600

# Metric names (event ``metric.attributes.name``) golden reads. Anything else is skipped.
METRIC_RECEIVED = "Received Email"
METRIC_OPENED = "Opened Email"
METRIC_CLICKED = "Clicked Email"
METRIC_UNSUBSCRIBED = "Unsubscribed"
METRIC_BOUNCED = "Bounced Email"
METRIC_SPAM = "Marked Email as Spam"
METRIC_SUBSCRIBED = "Subscribed to Email Marketing"

# Event property names.
PROP_MESSAGE = "$message"           # the campaign (or flow) message id: golden's campaign id
PROP_CAMPAIGN = "Campaign Name"     # never stored: a display string
PROP_SUBJECT = "Subject"
PROP_BOUNCE_TYPE = "Bounce Type"    # "hard" / "soft"
HARD_BOUNCE = "hard"

# A profile's own email-marketing state (``subscriptions.email.marketing``), read
# once when golden adopts a profile it did not create.
CONSENT_UNSUBSCRIBED = "UNSUBSCRIBED"
#: Klaviyo suppression reasons -> golden ``Suppression.reason``.
SUPPRESSION_REASONS = {
    "HARD_BOUNCE": "hard_bounce",
    "INVALID_EMAIL": "hard_bounce",
    "SPAM_COMPLAINT": "complaint",
    "USER_SUPPRESSED": "opt_out",
}

#: Profile URL shown on the ExternalRef.
PROFILE_URL = "https://www.klaviyo.com/profile/{profile_id}"
