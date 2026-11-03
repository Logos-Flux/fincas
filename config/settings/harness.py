"""Release-harness settings (docs/release-testing.md). Never shipped: ``config/`` is not in the wheel.

The back-office harness (``scripts/harness/run.sh``, ``make harness``) runs
golden's own server on this module: Postgres through ``DATABASE_URL``, a
request/exception log the harness slices per step, and a harness-only
URLconf (``config.harness_urls``) that mounts, beside ``golden_erp.urls``:

- a dev stub identity provider for the portal (local issuer: JWKS, the
  Supabase-shaped admin "users" endpoint the portal's link step confirms
  addresses through, and a token mint), registered as ``harness-stub`` in
  ``GOLDEN_IDENTITY_PROVIDERS``, never ``trusted``;
- ``/harness/boom/``: a redirect to a view that raises, the fixture for the
  server-error capture (a 500 the page never shows must still fail a step);
- ``/harness/webhooks/``: a local webhook receiver that dedupes by
  ``X-Golden-Delivery-Id``;
- ``/harness/state/...``: read-only database facts the journeys assert.

It refuses to import unless ``DEBUG`` or ``GOLDEN_HARNESS=1`` is set and
``ALLOWED_HOSTS`` is loopback only (device emulation for 360 px is a
viewport, it needs no other host), with one line naming what is missing.

Environment it reads (the runner sets them):
  GOLDEN_HARNESS=1              required (or DEBUG=1)
  DATABASE_URL=postgres://...   required; SQLite is refused
  ALLOWED_HOSTS                 optional; only loopback names allowed
  GOLDEN_HARNESS_PORT           the server's port (default 8771), used in the stub issuer URLs
  GOLDEN_HARNESS_LOG            path of the request/exception log (default: none)
  GOLDEN_HARNESS_WEBHOOK_LOG    JSON-lines file the webhook receiver appends to (default: memory only)
  GOLDEN_HARNESS_DEBUG          "1" runs with DEBUG=True (default off: the styled error pages render)
  GOLDEN_PORTAL_COMPANY         default HARNESS_COMPANY_ID
"""

from __future__ import annotations

import os

# The one Company every harness run uses (``seed_release_test`` creates it
# with this pk first, so GOLDEN_PORTAL_COMPANY is known before any command
# runs). It lives in shipped code because the seed command cannot import
# ``config``; this is the settings-level name for it.
from core.release_test import HARNESS_COMPANY_ID  # noqa: F401

DOCS = "docs/release-testing.md"

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _refuse(missing: str) -> None:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        f"config.settings.harness refuses to load: {missing}. See {DOCS}#running-the-harness."
    )


if not (_truthy(os.environ.get("GOLDEN_HARNESS")) or _truthy(os.environ.get("DEBUG"))):
    _refuse("set GOLDEN_HARNESS=1 (or DEBUG=1); these settings mount a raising view and a stub identity provider")

_hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
_bad_hosts = [h for h in _hosts if h not in LOOPBACK_HOSTS]
if _bad_hosts:
    _refuse(f"ALLOWED_HOSTS must be loopback only, got {', '.join(_bad_hosts)}")

_db_url = os.environ.get("DATABASE_URL", "")
if not _db_url.startswith(("postgres://", "postgresql://")):
    _refuse("DATABASE_URL must point at a Postgres database (the harness never runs on SQLite)")

# The project shell: DEBUG on, DATABASE_URL honoured, console email, AllowAny DRF defaults.
from .dev import *  # noqa: E402,F401,F403

DEBUG = _truthy(os.environ.get("GOLDEN_HARNESS_DEBUG"))
ALLOWED_HOSTS = list(LOOPBACK_HOSTS)
ROOT_URLCONF = "config.harness_urls"

# Static files without collectstatic, DEBUG on or off (whitenoise serves from the finders).
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

HARNESS_PORT = int(os.environ.get("GOLDEN_HARNESS_PORT", "8771"))
HARNESS_BASE_URL = f"http://127.0.0.1:{HARNESS_PORT}"

GOLDEN_PORTAL_COMPANY = os.environ.get("GOLDEN_PORTAL_COMPANY") or str(HARNESS_COMPANY_ID)

# Staff roles the seed gives a login each (docs/roles.md). Built-in ``staff``
# always exists; these are the back-office roles R2 and R10 refuse actions for.
GOLDEN_ROLES = [
    {"name": "sales", "label": "Sales", "staff": True},
    {"name": "sales-manager", "label": "Sales manager", "staff": True,
     "permissions": ["crm.reopen_engagement"]},
    {"name": "warehouse", "label": "Warehouse", "staff": True},
    {"name": "purchasing", "label": "Purchasing", "staff": True},
    {"name": "bookkeeper", "label": "Bookkeeper", "staff": True},
    # R7, R8 and R10 act as a back-office administrator. The merge queue,
    # erasure, applying CRM imports, teams, pipelines and connected accounts
    # are permission-gated, and a superuser resolves to no company
    # (core.views.request_company fails closed), so a staff role carries them.
    {"name": "crm-admin", "label": "CRM admin", "staff": True,
     "permissions": ["parties.merge_party", "parties.erase_party", "crm_import.apply_import",
                     "core.manage_teams", "crm.manage_pipelines", "core.change_integrationaccount",
                     # R10: roles from the Users list, labels from Settings.
                     "auth.change_user", "core.change_labeloverride"]},
    # The integration identity R1's API intake posts leads with (a service
    # contract: exactly crm.add_lead; the storefront's own contract cannot).
    {"name": "lead-intake", "label": "Lead intake", "kind": "service", "permissions": ["crm.add_lead"]},
]

# The dev stub identity provider (config.harness_views): a local issuer the
# portal verifies tokens against. Not trusted: it may never link or act for a
# staff account. The service key is a fixed, non-secret string (loopback only).
HARNESS_IDP_URL = f"{HARNESS_BASE_URL}/harness/idp"
HARNESS_IDP_ISSUER = HARNESS_IDP_URL
HARNESS_IDP_JWKS_URL = f"{HARNESS_IDP_URL}/.well-known/jwks.json"
HARNESS_IDP_SERVICE_KEY = "harness-stub-service-key"
HARNESS_IDP_PROVIDER = "harness-stub"
GOLDEN_IDENTITY_PROVIDERS = [
    {
        "name": HARNESS_IDP_PROVIDER,
        "backend": "core.identity.providers.SupabaseJWT",
        "entry_points": ["portal"],
        "self_register": True,
        "roles_on_create": ["customer"],
        "link_by_verified_email": True,
        "trusted": False,
        "options": {
            "jwks_url_setting": "HARNESS_IDP_JWKS_URL",
            "issuer_setting": "HARNESS_IDP_ISSUER",
            "audience": "authenticated",
            "url_setting": "HARNESS_IDP_URL",
            "service_role_key_setting": "HARNESS_IDP_SERVICE_KEY",
        },
    },
]
# R6 signs up several customers from one browser; the per-IP throttle is for production.
GOLDEN_PORTAL_SIGNUP_RATE = None

# Connected accounts need a key to store anything (core.E040); fixed, loopback-only.
GOLDEN_SECRET_KEYS = GOLDEN_SECRET_KEYS or ["aGFybmVzcy1vbmx5LWZlcm5ldC1rZXktMDAwMDAwMDA="]  # noqa: F405

HARNESS_WEBHOOK_LOG = os.environ.get("GOLDEN_HARNESS_WEBHOOK_LOG") or None

_log_path = os.environ.get("GOLDEN_HARNESS_LOG")
if _log_path:
    from config.harness_log import harness_logging

    LOGGING = harness_logging(_log_path, LOGGING)  # noqa: F405

# R10 (setup) needs a provider the Connected-accounts screen can offer and a
# canonical-export adapter R7 can stage the HubSpot fixture with. Both are
# golden's own addons; the fixture provider is tests-only and never shipped
# in a deployment's settings. Loopback only, like everything here.
GOLDEN_PLUGINS = [
    "golden_erp.addons.fixture_crm.plugin.FixtureCrmPlugin",
    "golden_erp.addons.hubspot_export.plugin.HubSpotExportPlugin",
]
# The fixture provider's API answers locally (config/harness_transport.py):
# R10.07's "Test connection" needs no network, and no other host is reached.
from config.harness_transport import install as _install_stub_transport  # noqa: E402

HARNESS_STUB_TRANSPORT = _install_stub_transport()

# R10's webhook delivery rides the outbox: lifecycle signals write
# core.OutboxEvent rows and `manage.py process_outbox --once` delivers them
# to /harness/webhooks/ with X-Golden-Delivery-Id (docs/release-testing.md).
WEBHOOKS_DELIVER_ON_SIGNALS = True
