"""External login identities (identity-unification §2.2, step 3).

``UserIdentity`` (``core.identity.models``, re-exported from ``core.models``)
maps a provider's stable subject to one ``auth.User``. Step 3 added the table
and backfilled it (``core.identity.backfill``). Step 4 added the provider
registry (``core.identity.providers``, ``GOLDEN_IDENTITY_PROVIDERS``) and the
portal resolver (``core.identity.resolve``), which authenticate through it.

Provider names are persisted in ``UserIdentity.provider``, so treat them like
theme names: never rename one in place.
"""

# The backfill writes rows under these names to record history. No provider
# may ever be configured under them: a configured "local" provider issuing the
# subject "local:5" would log someone in as user 5. The provider registry
# (core.identity.providers) refuses them.
LEGACY_SUPABASE_PROVIDER = "legacy_supabase"
LOCAL_PROVIDER = "local"
RESERVED_PROVIDER_NAMES = frozenset({LEGACY_SUPABASE_PROVIDER, LOCAL_PROVIDER})

# The customer portal's Supabase provider name (golden's base.py default).
SUPABASE_PROVIDER = "supabase"

# Generated human usernames ("u-<32 hex>"). Reserved: hand-made accounts must
# not use it (§7).
GENERATED_USERNAME_PREFIX = "u-"
