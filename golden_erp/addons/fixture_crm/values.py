"""Provider facts for the fixture adapters, isolated as a real adapter isolates them.

A real adapter puts here every detail golden cannot verify offline (header
names, limits, versions) so the downstream guide can list what to confirm.
"""

from __future__ import annotations

PROVIDER = "fixture"
OAUTH_PROVIDER = "fixture_oauth"
#: Newest envelope version the readers accept.
SCHEMA_VERSION = 1
BASE_URL = "https://api.fixture.example.com"
DOCS = "See docs/crm/adapters.md#the-fixture-provider."

# Webhook scheme: HMAC-SHA256 (hex) over b"<timestamp>." + body, keyed by the
# account's webhook_secret; timestamp in unix seconds; a delivery id.
SIGNATURE_HEADER = "x-fixture-signature"
TIMESTAMP_HEADER = "x-fixture-timestamp"
DELIVERY_HEADER = "x-fixture-delivery"

# OAuth (fixture_oauth).
AUTHORIZE_URL = "https://auth.fixture.example.com/oauth/authorize"
TOKEN_URL = "https://auth.fixture.example.com/oauth/token"
REVOKE_URL = "https://auth.fixture.example.com/oauth/revoke"
SCOPES = ("messages.read",)

#: Event types the fixture provider sends; anything else is skipped, not misread.
MESSAGE_TYPES = {"message.received": "inbound", "message.sent": "outbound"}
