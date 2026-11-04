"""Django base settings for Fincas.

Generic, open-source commerce platform and operations engine.

This module ships in the ``golden-erp`` package. A project builds on it with
``from golden_erp.settings.base import *`` and then sets what is genuinely its
own: ``DATABASES``, ``BASE_DIR`` (and so ``STATIC_ROOT``), ``ROOT_URLCONF``,
``WSGI_APPLICATION``, extra apps via ``compose_installed_apps``. golden's own
project shell does exactly that in ``config/settings/base.py``.

Values here are read from the environment at import time; this module does not
load a ``.env`` file — the importing project does that first if it wants one.
Packaged templates and static resolve through ``golden_erp.paths``, never
``BASE_DIR``, so they are found the same way from a checkout or a wheel.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from golden_erp import paths as _paths

# The project root. Only the defaults below derive from it (STATIC_ROOT); a
# project overrides BASE_DIR and re-derives them. Defaults to the working
# directory, i.e. where manage.py is run from.
BASE_DIR = Path(os.environ.get("GOLDEN_BASE_DIR") or Path.cwd()).resolve()

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")

# Fail-fast in production if SECRET_KEY is not set
if not SECRET_KEY and not os.environ.get("DEBUG"):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY environment variable is required in production. "
        "Set DJANGO_SECRET_KEY or run with DEBUG=True for development."
    )

# Fallback for development only
if not SECRET_KEY:
    SECRET_KEY = "django-insecure-golden-erp-dev-only-change-in-production"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(",") if os.environ.get("ALLOWED_HOSTS") else []

# Reverse proxies (and server-side callers, e.g. a storefront renderer) whose
# X-Forwarded-For is believed: IPs or CIDRs, comma-separated. Empty = trust no
# one and key per-client throttles on REMOTE_ADDR. See core/client_ip.py.
TRUSTED_PROXIES = [p.strip() for p in os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()]
# F-086 escape hatch: with the cache unreachable the cache-counted gates (login,
# OIDC start, the DRF throttles) refuse; "1" lets attempts through uncounted so
# an operator can sign in during the incident. core.cache_guard reads it.
GOLDEN_CACHE_DOWN_FAIL_OPEN = os.environ.get("GOLDEN_CACHE_DOWN_FAIL_OPEN", "").strip().lower() in ("1", "true", "yes", "on")

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.humanize",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
    "anymail",
    # Core apps
    "core",
    "products",
    "customers",
    "orders",
    "payments",
    "purchasing",
    "fulfillment",
    "inventory",
    "pricing",
    "accounting",
    "documents",
    "reporting",
    "crm",
    "discounts",
    "content",
    "communications",
    "portal",
    "chat",
    "llmhub",
    "webhooks",
    "unify",
    "parties",
    "crm_import",
]

# Plugins (docs/plugins.md). GOLDEN_PLUGINS = BasePlugin glue classes;
# GOLDEN_PLUGIN_APPS = real Django apps. A deployment overlay sets both and
# ends with ``INSTALLED_APPS = compose_installed_apps(INSTALLED_APPS,
# GOLDEN_PLUGIN_APPS)``; CoreConfig.ready() fails startup on a misplaced entry
# or an app that never reached INSTALLED_APPS.
GOLDEN_PLUGINS = []
GOLDEN_PLUGIN_APPS = []

# Outbound webhooks fired from lifecycle signals. Delivery runs from the
# outbox (webhooks/outbox.py, `manage.py process_outbox`), never in the request
# thread. Still off by default: a deployment whose subscriptions never received
# events turns it on deliberately (docs/webhooks.md section 5).
WEBHOOKS_DELIVER_ON_SIGNALS = False

# The outbox (core/outbox, docs/crm/phase6-adapters.md 1.5). `process_outbox
# --prune` deletes done rows after retain_done_days, done `*.inbound` rows
# (provider bodies) after retain_inbound_days, cancelled rows after
# retain_cancelled_days. Dead rows stay until requeued or cancelled.
GOLDEN_OUTBOX = {"retain_done_days": 7, "retain_inbound_days": 1, "retain_cancelled_days": 7}

# Webhook payload versions (docs/webhooks.md 1.1). A subscription created
# before this instant that pins no version keeps customer.registered v1
# during 0.3. A deployment that upgrades to 0.3 later sets its own upgrade
# time here (ISO 8601 with zone). None = the default in webhooks/events.py.
WEBHOOKS_VERSIONING_CUTOFF = None

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Audit actor for LLM settings writes (contextvar set + reset per request).
    "llmhub.middleware.LLMHubActorMiddleware",
    "core.middleware.APIKeyMiddleware",
    # /erp/ is staff-only; signed-in non-staff accounts get 403 (login/logout/
    # password-reset stay open). Anonymous users are redirected by the views.
    "core.middleware.StaffOnlyERPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Guard rails for a publicly advertised demo login (inert unless
    # DEMO_USERNAMES is set).
    "core.middleware.DemoGuardMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.ForcePasswordChangeMiddleware",
]

SECURE_CONTENT_TYPE_NOSNIFF = True

# Cap for manufacturer document uploads (F-048). Overridable via env for
# tenants that routinely handle larger datasheets/manuals.
DOCUMENT_MAX_UPLOAD_BYTES = int(
    os.environ.get("DOCUMENT_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))
)

ROOT_URLCONF = "golden_erp.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [_paths.TEMPLATES_DIR],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "chat.context_processors.chat_models",
                "core.context_processors.erp_themes",
                "core.context_processors.erp_nav",
                "core.context_processors.labels",
            ],
        },
    },
]

# WSGI_APPLICATION is project-local (golden's lives in config/settings/base.py);
# left unset here, runserver falls back to Django's default WSGI handler.

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "static/"
# collectstatic target: project-local, so it follows BASE_DIR (override both
# together). STATICFILES_DIRS is golden's packaged static; a project that
# overrides golden assets prepends its own directory.
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [_paths.STATIC_DIR]

LOGIN_URL = "/erp/login/"
LOGIN_REDIRECT_URL = "/erp/"
LOGOUT_REDIRECT_URL = "/erp/login/"

# Public preview convenience: show demo credentials on the login page.
# Default off — enabling advertises a working login, so only ever set
# this on throwaway preview deployments, never production.
SHOW_DEMO_LOGIN_HINT = os.environ.get("SHOW_DEMO_LOGIN_HINT", "False").lower() == "true"
# Public demo login (staging). The hint on the login page shows these values,
# never a superuser's. DEMO_USERNAMES are the accounts DemoGuardMiddleware
# restricts; `manage.py setup_demo_user` creates DEMO_LOGIN_USERNAME as
# non-superuser staff from DEMO_LOGIN_PASSWORD.
DEMO_LOGIN_USERNAME = os.environ.get("DEMO_LOGIN_USERNAME", "")
DEMO_LOGIN_PASSWORD = os.environ.get("DEMO_LOGIN_PASSWORD", "")
# Empty counts as unset: docker compose passes "" for an unset variable.
DEMO_USERNAMES = [
    u.strip() for u in (os.environ.get("DEMO_USERNAMES") or DEMO_LOGIN_USERNAME).split(",") if u.strip()
]
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Caching
REDIS_URL = os.environ.get("REDIS_URL", "")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            # A Redis that hangs instead of refusing must not stall every request
            # (F-086, docs/perf/2026-10-18-staging-k6-outages.md): fail fast, then
            # core.cache_guard treats the failure as a miss.
            "OPTIONS": {"socket_connect_timeout": 0.5, "socket_timeout": 1.0},
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    # Staff API default (identity-unification D6): staff/superuser humans,
    # service identities by contract, everyone else 403. Viewsets still pin
    # it explicitly (dev/test override this default with AllowAny).
    "DEFAULT_PERMISSION_CLASSES": [
        "core.permissions.PrincipalPermission",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "core.authentication.BearerTokenAuthentication",
        "core.authentication.APIKeyAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # DRF's default handling plus one mapping: a DELETE refused by an
    # on_delete=PROTECT/RESTRICT FK is 409 ``code: protected``, not a 500.
    "EXCEPTION_HANDLER": "core.api_exceptions.exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fincas API",
    "DESCRIPTION": "Headless Commerce, Inventory, Accounting, and Operations REST API for Fincas.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

# CORS
CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if os.environ.get("CORS_ALLOWED_ORIGINS") else []
# The storefront's checkout sends Idempotency-Key (OP #3431); without it here
# the browser's preflight fails and no cross-origin checkout can be placed.
from corsheaders.defaults import default_headers as _cors_default_headers  # noqa: E402

CORS_ALLOW_HEADERS = (*_cors_default_headers, "idempotency-key")

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {"level": "WARNING", "propagate": True},
        "django.request": {"level": "ERROR", "propagate": True},
    },
}

# Email notifications
SALES_NOTIFICATION_EMAIL = os.environ.get("SALES_NOTIFICATION_EMAIL", "")

# Public storefront base URL. Used for pay-by-link URLs on invoice PDFs and emails.
# Fails closed when unset.
STOREFRONT_URL = os.environ.get("STOREFRONT_URL", "").rstrip("/")

# Staff SSO bridge to the storefront admin
STAFF_SESSION_SECRET = os.environ.get("STAFF_SESSION_SECRET", "")
STAFF_SSO_CALLBACK_URL = os.environ.get(
    "STAFF_SSO_CALLBACK_URL", f"{STOREFRONT_URL}/staff/callback" if STOREFRONT_URL else ""
)

# External API auth (core/authentication.py, core/service_accounts.py).
# Per-integration keys are core.ApiKey rows (`manage.py create_api_key`).
# API_KEY is the deprecated shared key; it acts as GOLDEN_LEGACY_API_KEY_USER,
# a dedicated superuser service account (empty/None disables the legacy path).
API_KEY = os.environ.get("API_KEY", "")
GOLDEN_LEGACY_API_KEY_USER = os.environ.get("GOLDEN_LEGACY_API_KEY_USER", "api-service") or None
# HS256 key for golden-issued bearer JWTs. Unset: derived from SECRET_KEY
# (system check core.W110). Never reuse API_KEY here.
API_JWT_SIGNING_KEY = os.environ.get("API_JWT_SIGNING_KEY", "")
# Public storefront token issuance (/api/v1/core/auth/storefront-token/).
# Unset: on exactly when API_KEY is set (the pre-step-5 behaviour).
_storefront_tokens = os.environ.get("GOLDEN_STOREFRONT_TOKENS", "").strip().lower()
GOLDEN_STOREFRONT_TOKENS = None if not _storefront_tokens else _storefront_tokens in ("1", "true", "yes")
# Transition (off by default, 2026-10-08): also accept api-service/storefront
# bearers signed with API_KEY, i.e. minted before identity step 5. Only for a
# short, deliberate window: such tokens bypass the newer revocation switches.
API_JWT_ACCEPT_API_KEY_SIGNATURE = os.environ.get(
    "API_JWT_ACCEPT_API_KEY_SIGNATURE", ""
).lower() in ("1", "true", "yes")

# Supabase (customer portal auth). Read by the "supabase" identity provider
# below when a token arrives, never at import. JWKS (Supabase's asymmetric
# signing keys) wins over the HS256 secret when both are set. With an issuer
# set, a token's `iss` must equal it (e.g. https://<ref>.supabase.co/auth/v1).
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET") or None
SUPABASE_JWKS_URL = os.environ.get("SUPABASE_JWKS_URL", "")
SUPABASE_ISSUER = os.environ.get("SUPABASE_ISSUER", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")

# Identity providers (identity-unification D4, §2.4; core/identity/providers.py).
# `password` (Django's ModelBackend, /erp/ only, trusted) is built in. Each
# entry here is validated at import (ImproperlyConfigured); `local` and
# `legacy_supabase` are reserved. Names are persisted in UserIdentity.provider:
# never rename one in place. Secrets are named by setting (`*_setting`) or
# environment variable (`*_env`), never written inline.
GOLDEN_IDENTITY_PROVIDERS = [
    {
        "name": "supabase",
        "backend": "core.identity.providers.SupabaseJWT",
        "entry_points": ["portal"],
        "self_register": True,
        "roles_on_create": ["customer"],
        "link_by_verified_email": True,
        "options": {
            "jwt_secret_setting": "SUPABASE_JWT_SECRET",
            "jwks_url_setting": "SUPABASE_JWKS_URL",
            "issuer_setting": "SUPABASE_ISSUER",
            "audience": "authenticated",
            "url_setting": "SUPABASE_URL",
            "service_role_key_setting": "SUPABASE_SERVICE_ROLE_KEY",
        },
    },
]
# Per-IP limit on portal self-registration (the resolver's create branch).
GOLDEN_PORTAL_SIGNUP_RATE = os.environ.get("GOLDEN_PORTAL_SIGNUP_RATE", "20/hour") or None
# Per-IP limit on storefront guest checkout (orders created by the storefront
# identity). DRF rate syntax ("10/minute"); empty disables it.
GOLDEN_GUEST_ORDER_RATE = os.environ.get("GOLDEN_GUEST_ORDER_RATE", "10/minute") or None
# Clock-skew allowance for provider tokens (`exp`, `iat`), in seconds.
GOLDEN_IDENTITY_JWT_LEEWAY_S = int(os.environ.get("GOLDEN_IDENTITY_JWT_LEEWAY_S", "30"))

# EasyPost shipping label API
EASYPOST_API_KEY = os.environ.get("EASYPOST_API_KEY", "")

# Warehouse printer — raw TCP socket to Zebra (ip:port)
ZEBRA_PRINTER_HOST = os.environ.get("ZEBRA_PRINTER_HOST", "")

# CDN / Image delivery base URL
BUNNY_IMAGES_BASE = os.environ.get("BUNNY_IMAGES_BASE", "")

# SEO copy-quality rules (products/seo_quality.py): JSON files merged onto
# golden's tenant-neutral defaults (products/data/seo_quality_defaults.json),
# carrying the deployment's own template patterns and cut-point words, e.g.
#   GOLDEN_SEO_PATTERN_FILES = [BASE_DIR / "myshop/data/seo_patterns.json"]
# Relative paths resolve against BASE_DIR. Validated at startup
# (ImproperlyConfigured on a missing file, a bad regex or a duplicate id).
GOLDEN_SEO_PATTERN_FILES: list = []

# ERP chat assistant — direct provider keys (no gateway).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")

CHAT_EXTRA_MODELS: list[dict] = []
CHAT_EXTRA_MODEL_ALIASES: dict[str, str] = {}

# ---- llmhub: LLM routing + the /erp/llm/ settings page -----------------------
# Resolution everywhere is DB row (set on /erp/llm/) → these env values →
# built-in defaults. `direct` calls providers with the keys above; `gateway`
# calls logical lanes on an OpenAI-compatible gateway (e.g. LiteLLM), so a
# model swap is a lane re-point in the gateway's UI — no code, no deploy.
LLMHUB_MODE = os.environ.get("LLMHUB_MODE", "direct")
LLMHUB_GATEWAY_BASE_URL = os.environ.get("LLMHUB_GATEWAY_BASE_URL", "")
# Env fallback for the gateway caller token (used while LLMSettings.fallback_to_env).
LLMHUB_GATEWAY_API_KEY = os.environ.get("LLMHUB_GATEWAY_API_KEY", "")
# Dedicated Fernet key for the DB-stored token. Unset = derived from SECRET_KEY
# (system check llmhub.W001 warns: a SECRET_KEY rotation then bricks the token).
LLMHUB_FERNET_KEY = os.environ.get("LLMHUB_FERNET_KEY", "")

# ---- Connected accounts: encryption keys for provider credentials ------------
# Fernet keys, comma-separated, NEWEST FIRST (docs/crm/adapters.md#keys). The
# first encrypts; any decrypts; `manage.py rotate_secrets` re-encrypts under
# the first. Empty = no credential can be stored. Never derived from SECRET_KEY.
GOLDEN_SECRET_KEYS = [k.strip() for k in os.environ.get("GOLDEN_SECRET_KEYS", "").split(",") if k.strip()]
# Production refuses to deploy without the dedicated secrets above
# (API_JWT_SIGNING_KEY, LLMHUB_FERNET_KEY, GOLDEN_SECRET_KEYS): system check
# core.E060, which `migrate` runs on every deploy. golden_erp.settings.prod
# turns it on; `manage.py generate_secrets` prints a fresh set.
GOLDEN_REQUIRE_DEPLOY_SECRETS = False
# Deploy-time allowlist for the DB base URL. EMPTY = UNLOCKED: a template cannot
# know its tenants' hosts. A production deployment should set it — otherwise a
# staff session can point LLM traffic (prompts, ERP data) at any host.
LLMHUB_ALLOWED_BASE_URLS = [u.strip() for u in os.environ.get("LLMHUB_ALLOWED_BASE_URLS", "").split(",") if u.strip()]
LLMHUB_CACHE_TTL_S = int(os.environ.get("LLMHUB_CACHE_TTL_S", "60"))
LLMHUB_TEST_TIMEOUT_S = int(os.environ.get("LLMHUB_TEST_TIMEOUT_S", "15"))
# The jobs this deployment routes. Adding one = an entry here + its call site.
# Unification purposes (Step 4, XD-6): the lane is empty until the operator
# wires one — propose_* fail loudly through LLMHubError, never silently.
LLMHUB_PURPOSES = {
    "chat_default": {
        "label": "ERP chat (default)",
        "description": "The chat drawer's default model, and its fallback when no chat models are configured.",
        "kind": "chat",
        "gateway_route": os.environ.get("LLMHUB_CHAT_LANE", ""),
        "tools": True,
    },
    "mapping_propose": {
        "label": "Header-to-field mapping proposals",
        "description": "Maps messy CSV headers to product fields for staged import rows. No writes; output validated before it touches a row.",
        "kind": "chat",
        "gateway_route": os.environ.get("LLMHUB_UNIFY_LANE", ""),
    },
    "dedupe_propose": {
        "label": "Duplicate-row proposals",
        "description": "Groups likely-duplicate staged rows. Merging is never automatic — approval marks losers N/A.",
        "kind": "chat",
        "gateway_route": os.environ.get("LLMHUB_UNIFY_LANE", ""),
    },
    "coa_map": {
        "label": "Chart-to-posting-rule mapping",
        "description": "Proposes posting-rule account assignments from a company chart. Applied only through explicit rule approval.",
        "kind": "chat",
        "gateway_route": os.environ.get("LLMHUB_UNIFY_LANE", ""),
    },
    # CRM import (docs/crm/phase5-importer.md 3.5): suggestions only, sent
    # comparison features (booleans, buckets), never names or contact details.
    "crm_match_propose": {
        "label": "CRM import match suggestions",
        "description": "Suggests a candidate (or create) for undecided CRM import rows. Never changes a decision; a disagreement only flags the row.",
        "kind": "chat",
        "gateway_route": os.environ.get("LLMHUB_UNIFY_LANE", ""),
    },
    "crm_dedupe_hint": {
        "label": "CRM import duplicate hints",
        "description": "Hints which incoming CRM records look like the same person or organization. A hint for the merge queue, never a merge.",
        "kind": "chat",
        "gateway_route": os.environ.get("LLMHUB_UNIFY_LANE", ""),
    },
}
# CRM importer (docs/crm/importing.md). ai_budget_per_batch: the LLM calls one
# object type's batch may spend on AI suggestions (spec 3.5).
GOLDEN_CRM_IMPORT = {
    "ai_budget_per_batch": os.environ.get("GOLDEN_CRM_IMPORT_AI_BUDGET", "500"),  # crm_import.ai parses it
}
# Optional drift-check dimension probe (one paid embedding per run).
LLMHUB_EMBED_PROBE_API_KEY = os.environ.get("LLMHUB_EMBED_PROBE_API_KEY", "")
LLMHUB_EMBED_PROBE_ROUTE = os.environ.get("LLMHUB_EMBED_PROBE_ROUTE", "")
LLMHUB_EMBED_PROBE_DIMENSIONS = int(os.environ.get("LLMHUB_EMBED_PROBE_DIMENSIONS", "0")) or None
# Env only on purpose: the probe key is not the ERP's, so it never follows the DB base URL.
LLMHUB_EMBED_PROBE_BASE_URL = os.environ.get("LLMHUB_EMBED_PROBE_BASE_URL", "")

# ERP theme registry. Stylesheets are generated from the palettes in
# scripts/build_erp_themes.py (which also enforces WCAG AA); add a palette
# there, run it, then add the entry here.
# Extension points shared code calls instead of importing an add-on
# (core/hook_registry.py lists the hook names; docs/plugins.md "Hook points").
# Add-ons register their implementation from AppConfig.ready(). An entry here
# overrides that: a dotted path swaps the implementation, None switches the
# hook off. Names and shape are validated at import (ImproperlyConfigured); a
# dotted path is imported on first use.
ERP_HOOKS = {}

ERP_THEMES = [
    {
        "name": "fincas",
        "label": "Fincas",
        "css": "assets/css/themes/fincas.css",
        "mode": "light",
        "bg": "#fcfcfe",
    },
    {
        "name": "golden",
        "label": "Golden",
        "css": "assets/css/themes/golden.css",
        "mode": "light",
        "bg": "#f6f5f1",
    },
    {
        "name": "paper",
        "label": "Paper",
        "css": "assets/css/themes/paper.css",
        "mode": "light",
        "bg": "#f3eee4",
    },
    {
        "name": "slate",
        "label": "Slate",
        "css": "assets/css/themes/slate.css",
        "mode": "light",
        "bg": "#f2f4f8",
    },
    {
        "name": "sage",
        "label": "Sage",
        "css": "assets/css/themes/sage.css",
        "mode": "light",
        "bg": "#f1f4f0",
    },
    {
        "name": "midnight",
        "label": "Midnight",
        "css": "assets/css/themes/midnight.css",
        "mode": "dark",
        "bg": "#0b0d12",
    },
    {
        "name": "graphite",
        "label": "Graphite",
        "css": "assets/css/themes/graphite.css",
        "mode": "dark",
        "bg": "#121316",
    },
    {
        "name": "ember",
        "label": "Ember",
        "css": "assets/css/themes/ember.css",
        "mode": "dark",
        "bg": "#141110",
    },
    {
        "name": "eva-01",
        "label": "EVA-01",
        "css": "assets/css/themes/eva-01.css",
        "mode": "dark",
        "bg": "#030c1d",
    },
]
ERP_DEFAULT_THEME = "fincas"

# Roles (identity-unification D3; docs/roles.md). Built-ins `staff`,
# `customer` and `storefront` always exist and cannot be redefined; a
# deployment appends its own, e.g.
#   {"name": "manager", "label": "Manager", "staff": True,
#    "permissions": ["orders.change_quote"]}
# Each role is a Group created on post_migrate; `is_staff` is derived from
# membership of a staff=True role. Validated at import (ImproperlyConfigured).
GOLDEN_ROLES = []
# A role group that already exists with members who are not is_staff (e.g. a
# hand-made "staff" group) makes migrate fail rather than silently promote them.
# Set True to take such groups over (their members become back-office staff).
GOLDEN_ROLES_ADOPT_PREEXISTING = False

# Parties (CRM Phase 2; docs/crm/phase2-party.md 1.2, 1.3). Built-in party
# roles (customer, supplier, partner, lead, referral_source, staff, client) and
# relationship kinds (employment, contact_for, partnership, spouse, referral,
# parent_organisation) always exist and cannot be redefined; a deployment
# appends its own, e.g.
#   GOLDEN_PARTY_ROLES = [{"name": "installer", "label": "Installer",
#                          "kinds": ["organisation"]}]
#   GOLDEN_RELATIONSHIP_KINDS = [{"name": "installs_for", "label": "installs for",
#                                 "reverse_label": "installer"}]
# Validated at import (ImproperlyConfigured). Names are persisted: never rename.
GOLDEN_PARTY_ROLES = []
GOLDEN_RELATIONSHIP_KINDS = []
# Party classification (CRM Phase 4, docs/crm/phase4-fields.md 2.2).
# GOLDEN_LIFECYCLE_STAGES is an ordered funnel: built-ins subscriber, lead,
# marketing_qualified, sales_qualified, opportunity, customer, evangelist,
# other (sort_order 10..80) cannot be redefined; a deployment appends
#   [{"name": "churned", "label": "Churned", "sort_order": 90}]
# GOLDEN_CONTACT_TYPES has no built-ins; export_value (default: the label) is
# the exact string a segment publisher emits:
#   [{"name": "installer", "label": "Installer", "export_value": "Installer"}]
# Validated at import (ImproperlyConfigured). Names are persisted: never rename.
GOLDEN_LIFECYCLE_STAGES = []
GOLDEN_CONTACT_TYPES = []

# CRM engagement registries (CRM Phase 3, docs/crm/phase3-engagement.md 1.1,
# 1.2). Built-in loss reasons, engagement sources and task kinds always exist
# and cannot be redefined; a deployment appends its own, e.g.
#   GOLDEN_LOSS_REASONS = [{"name": "credit", "label": "Failed credit check"}]
#   GOLDEN_TASK_KINDS = [{"name": "site_visit", "label": "Site visit", "icon": "ti ti-map-pin"}]
# GOLDEN_DEFAULT_PIPELINES (None = golden's "sales" pipeline) *replaces* the
# pipelines seeded for each new Company:
#   [{"key": "sales", "name": "Sales", "stages": [{"key": "new", "name": "New",
#                                                  "default_probability": 5}, ...]}]
# Validated at import (ImproperlyConfigured). Keys are persisted: never rename.
GOLDEN_LOSS_REASONS = []
GOLDEN_ENGAGEMENT_SOURCES = []
GOLDEN_TASK_KINDS = []
GOLDEN_DEFAULT_PIPELINES = None
# CRM knobs (crm/conf.py): lead_response_hours (24), lead_default_owner (a
# UserProfile id, unset), attribution_lookback_days (90).
GOLDEN_CRM = {}

# Communication kinds (CRM Phase 3, docs/crm/phase3-engagement.md 2.3). The
# built-ins (email, sms, call, meeting, linkedin, note) always exist and
# cannot be redefined; a deployment appends kinds, plugins append theirs via
# BasePlugin.get_communication_kinds(). Keys are persisted: never rename.
#   GOLDEN_COMMUNICATION_KINDS = [{"name": "whatsapp", "label": "WhatsApp",
#                                  "icon": "ti ti-brand-whatsapp"}]
GOLDEN_COMMUNICATION_KINDS = []

# Staff-UI noun labels (CRM Phase 3, docs/labels.md). Reword a noun for the
# whole deployment, or per Company id, e.g.
#   GOLDEN_LABEL_OVERRIDES = {"crm.engagement": {"singular": "Idea", "plural": "Ideas"}}
#   GOLDEN_LABEL_TENANT_OVERRIDES = {"<company uuid>": {"orders.quote": {"singular": "Proposal"}}}
# Validated at import (ImproperlyConfigured on an unknown key). Staff UI only:
# APIs, CSV, PDFs, emails and webhooks always use canonical names.
GOLDEN_LABEL_OVERRIDES = {}
GOLDEN_LABEL_TENANT_OVERRIDES = {}
# Sidecar field definitions (CRM Phase 4, docs/fields.md): the ``metadata``
# keys a deployment uses on parties, engagements and tasks, so they validate,
# render, filter and export like columns without a golden migration, e.g.
#   GOLDEN_FIELD_DEFINITIONS = {"engagement": [
#       {"key": "custom.package", "label": "Package", "type": "choice",
#        "choices": [["accelerator", "Accelerator"], ["other", "Other"]],
#        "group": "Qualification", "list": True, "filterable": True}]}
# Validated at import (ImproperlyConfigured); an unknown company id in
# ``companies`` is system check core.W113. Keys are persisted: never rename.
GOLDEN_FIELD_DEFINITIONS = {}
# The deployment's field inventory CSVs (docs/crm/fields/README.md);
# ``check_sidecar`` reports provider keys none of them maps.
GOLDEN_FIELD_INVENTORIES = []
# Region for phone numbers written without a country code, used when the
# party's address country is unknown (ISO 3166-1 alpha-2; spec 7.3).
GOLDEN_DEFAULT_PHONE_REGION = "US"
# The tenant (Company pk) portal sign-ups and portal logins belong to
# (docs/crm/phase2-party.md 2.4). Unset: the only Company, if exactly one
# exists. Multi-company deployments with a portal provider must set it
# (system check parties.E001); the portal answers 503 until a tenant is known.
GOLDEN_PORTAL_COMPANY = os.environ.get("GOLDEN_PORTAL_COMPANY") or None
# The Company new orders and quotes pre-select in the back office, by name
# (case-insensitive). Unset: the only Company, if exactly one exists.
GOLDEN_DEFAULT_COMPANY_NAME = os.environ.get("GOLDEN_DEFAULT_COMPANY_NAME") or None

# Sidebar navigation registry
ERP_NAV = [
    {"type": "link", "section": "dashboard", "url_name": "erp-dashboard",
     "icon": "ti ti-layout-dashboard", "color": "text-dark", "label": "Dashboard"},

    {"type": "header", "label": "Sales"},
    {"type": "link", "section": "quotes", "url_name": "erp-quote-list",
     "icon": "ti ti-file-text", "color": "text-warning", "label": "Quotes",
     "label_key": "orders.quote"},
    {"type": "link", "section": "orders", "url_name": "erp-order-list",
     "icon": "ti ti-shopping-cart", "color": "text-info", "label": "Sales Orders",
     "label_key": "orders.order"},
    {"type": "link", "section": "invoices", "url_name": "erp-invoice-list",
     "icon": "ti ti-credit-card", "color": "text-success", "label": "Invoices",
     "label_key": "orders.sales_invoice"},
    {"type": "link", "section": "discounts", "url_name": "erp-discount-list",
     "icon": "fas fa-percent", "color": "text-success", "label": "Discount Codes"},

    {"type": "header", "label": "CRM"},
    {"type": "link", "section": "crm_my_day", "url_name": "erp-crm-my-day",
     "icon": "fas fa-sun", "color": "text-warning", "label": "My day"},
    {"type": "link", "section": "crm_engagements", "url_name": "erp-crm-engagement-list",
     "icon": "fas fa-briefcase", "color": "text-primary", "label": "Engagements",
     "label_key": "crm.engagement"},
    {"type": "link", "section": "crm_tasks", "url_name": "erp-crm-task-list",
     "icon": "fas fa-list-check", "color": "text-success", "label": "Tasks",
     "label_key": "crm.task"},
    {"type": "link", "section": "crm_leads", "url_name": "erp-crm-lead-list",
     "icon": "fas fa-inbox", "color": "text-info", "label": "Leads",
     "label_key": "crm.lead"},
    {"type": "link", "section": "customers", "url_name": "erp-customer-list",
     "icon": "ti ti-user", "color": "text-danger", "label": "Customers",
     "label_key": "customers.customer"},
    {"type": "link", "section": "parties", "url_name": "erp-party-list",
     "icon": "ti ti-users", "color": "text-info", "label": "Parties",
     "label_key": "parties.party"},

    {"type": "header", "label": "Inventory"},
    {"type": "link", "section": "products", "url_name": "erp-product-list",
     "icon": "ti ti-package", "color": "text-primary", "label": "Products"},
    {"type": "link", "section": "brands", "url_name": "erp-brand-list",
     "icon": "ti ti-badge", "color": "text-primary", "label": "Brands"},
    {"type": "link", "section": "categories", "url_name": "erp-category-list",
     "icon": "ti ti-category", "color": "text-primary", "label": "Categories"},
    {"type": "link", "section": "warehouses", "url_name": "erp-warehouse-list",
     "icon": "ti ti-building-warehouse", "color": "text-primary", "label": "Warehouses"},

    {"type": "header", "label": "Pricing"},
    {"type": "link", "section": "price_lists", "url_name": "erp-pricelist-list",
     "icon": "ti ti-tag", "color": "text-warning", "label": "Price Lists"},

    {"type": "header", "label": "Purchasing"},
    {"type": "link", "section": "purchase_orders", "url_name": "erp-purchaseorder-list",
     "icon": "ti ti-shopping-bag", "color": "text-dark", "label": "Purchase Orders"},
    {"type": "link", "section": "suppliers", "url_name": "erp-supplier-list",
     "icon": "ti ti-building", "color": "text-dark", "label": "Suppliers",
     "label_key": "purchasing.supplier"},

    {"type": "header", "label": "Fulfillment"},
    {"type": "link", "section": "pick_tickets", "url_name": "erp-pickticket-list",
     "icon": "ti ti-check", "color": "text-info", "label": "Pick Tickets"},

    {"type": "header", "label": "Documents"},
    {"type": "link", "section": "documents", "url_name": "erp-document-library",
     "icon": "ti ti-folder", "color": "text-dark", "label": "Documents"},

    {"type": "header", "label": "Content"},
    {"type": "link", "section": "content", "url_name": "erp-article-list",
     "icon": "fas fa-newspaper", "color": "text-warning", "label": "Articles"},

    {"type": "header", "label": "Reporting"},
    {"type": "link", "section": "reports", "url_name": "erp-report-list",
     "icon": "ti ti-chart-bar", "color": "text-success", "label": "Reports"},
    {"type": "link", "section": "reports_crm_funnel", "url_name": "erp-report-crm-funnel",
     "icon": "ti ti-chart-bar", "color": "text-success", "label": "Funnel by Source"},
    {"type": "link", "section": "reports_crm_stages", "url_name": "erp-report-crm-stages",
     "icon": "ti ti-clock", "color": "text-success", "label": "Stage Duration"},
    {"type": "link", "section": "reports_crm_follow_ups", "url_name": "erp-report-crm-follow-ups",
     "icon": "ti ti-check", "color": "text-success", "label": "Follow-up Completion"},
    {"type": "link", "section": "reports_crm_attribution", "url_name": "erp-report-crm-attribution",
     "icon": "ti ti-route", "color": "text-success", "label": "Multi-touch Attribution"},

    {"type": "header", "label": "Finance", "staff_only": True},
    {"type": "link", "section": "accounting", "url_name": "erp-accounting-dashboard",
     "icon": "ti ti-book", "color": "text-success", "label": "Accounting", "staff_only": True},
    {"type": "link", "section": "accounting_trial_balance", "url_name": "erp-accounting-trial-balance",
     "icon": "ti ti-scale", "color": "text-success", "label": "Trial Balance", "staff_only": True},
    {"type": "link", "section": "accounting_entries", "url_name": "erp-accounting-entries",
     "icon": "ti ti-list-details", "color": "text-success", "label": "Journal Entries", "staff_only": True},
    {"type": "link", "section": "accounting_import", "url_name": "erp-accounting-import",
     "icon": "ti ti-file-import", "color": "text-success", "label": "Accounting Import", "staff_only": True},

    {"type": "header", "label": "Data & Migration", "staff_only": True},
    {"type": "link", "section": "unify", "url_name": "erp-unify-batch-list",
     "icon": "ti ti-arrows-exchange", "color": "text-info", "label": "Imports", "staff_only": True},
    {"type": "link", "section": "crm_import", "url_name": "erp-crm-import-job-list",
     "icon": "fas fa-file-import", "color": "text-info", "label": "CRM Imports", "staff_only": True},

    {"type": "header", "label": "Admin", "staff_only": True},
    {"type": "link", "section": "crm_pipelines", "url_name": "erp-crm-pipeline-list",
     "icon": "fas fa-route", "color": "text-primary", "label": "Pipelines",
     "label_key": "crm.pipeline", "staff_only": True},
    {"type": "link", "section": "settings_teams", "url_name": "erp-settings-teams",
     "icon": "fas fa-people-group", "color": "text-primary", "label": "Teams", "staff_only": True},
    {"type": "link", "section": "settings_labels", "url_name": "erp-settings-labels",
     "icon": "fas fa-tags", "color": "text-primary", "label": "Labels", "staff_only": True},
    {"type": "link", "section": "llm", "url_name": "erp-llm-settings",
     "icon": "ti ti-brain", "color": "text-purple", "label": "LLM Settings", "staff_only": True},
    {"type": "link", "section": "users", "url_name": "erp-user-list",
     "icon": "ti ti-users", "color": "text-dark", "label": "Users", "staff_only": True},
    {"type": "link", "section": "integrations", "url_name": "erp-integrations",
     "icon": "fas fa-plug", "color": "text-dark", "label": "Integrations", "staff_only": True},
]

# External-service inventory (validated at import by core/integration_registry.py,
# rendered on Admin -> Integrations). A deployment replaces the list and the
# vocabulary below with its own in its settings overlay.
ERP_INTEGRATION_APPS = ["erp"]          # deployable apps that consume services
ERP_INTEGRATION_LOCAL_APP = "erp"       # which of them is this server
ERP_INTEGRATION_OWNERS = ["engineering", "operations", "unassigned"]
from golden_erp.integrations import ERP_INTEGRATIONS  # noqa: E402,F401

# Brand constants for document templates (can be overridden via instance config/settings)
ERP_BRAND = {
    "company_name": os.environ.get("BRAND_COMPANY_NAME", "Acme Commerce"),
    "address_line1": os.environ.get("BRAND_ADDRESS_LINE1", "100 Main Street"),
    "address_line2": os.environ.get("BRAND_ADDRESS_LINE2", "Suite 100, City, ST 00000"),
    "email": os.environ.get("BRAND_EMAIL", "info@example.com"),
    "phone": os.environ.get("BRAND_PHONE", "(555) 000-0000"),
    "website": os.environ.get("BRAND_WEBSITE", "example.com"),
    "logo_path": os.environ.get("BRAND_LOGO_PATH", "assets/img/logo.svg"),
    "colors": {
        "forest": "#042e0d",
        "green": "#278040",
        "blue": "#4d65b2",
        "bronze": "#c3a859",
        "bronze_dark": "#7a6223",
    },
    "fonts": {
        "heading": "Barlow, sans-serif",
        "body": "'Source Sans 3', sans-serif",
        "mono": "'Roboto Mono', monospace",
    },
}

# Maximum tokens per assistant turn.
CHAT_MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "4096"))
# Conversation history window
CHAT_HISTORY_WINDOW = int(os.environ.get("CHAT_HISTORY_WINDOW", "50"))
