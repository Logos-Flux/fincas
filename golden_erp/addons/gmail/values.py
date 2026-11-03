"""Provider facts for the Gmail adapter, isolated here so the downstream guide
(docs/crm/phase6-downstream.md, step 9) can list what to confirm against
Google's documentation before an account is enabled.

Nothing below was verified against a live account: golden makes no live call.
"""

from __future__ import annotations

PROVIDER = "gmail"
#: Newest envelope version the readers accept (the Gmail API v1 message resource).
SCHEMA_VERSION = 1
DOCS = "See docs/crm/adapters.md#gmail."

BASE_URL = "https://gmail.googleapis.com"
API = "/gmail/v1/users/me"

# OAuth (per-user grant, read-only scope).
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)

# Pub/Sub push: an OIDC token Google signs (RS256), checked against these keys.
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
#: Tolerated clock skew on a push token's ``iat`` / ``exp``.
TOKEN_SKEW_S = 60

#: Golden's own document emails carry this header (communications.services).
GOLDEN_LOG_HEADER = "X-Golden-Email-Log"

#: Page size for messages.list and history.list.
PAGE_SIZE = 50
#: ``body_text`` cap (UTF-8 bytes).
BODY_CAP_BYTES = 100 * 1024
#: When Gmail no longer has the stored history id (404), re-list this many days.
RESYNC_DAYS = 7

#: Never read, whatever the settings say: unsent drafts (one per saved revision), spam, trash, chats.
ALWAYS_EXCLUDED_LABELS = ("DRAFT", "SPAM", "TRASH", "CHAT")
#: ``settings["exclude_labels"]`` when unset; a configured list replaces this one (never the above).
DEFAULT_EXCLUDE_LABELS = ("CATEGORY_PROMOTIONS",)
#: ``settings["initial_since"]`` when unset (core's default, days).
INITIAL_SINCE_DAYS = 90
#: The per-user label a staff user puts on mail golden must never read.
DEFAULT_PRIVATE_LABEL = "golden-private"
