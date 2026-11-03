"""golden's default external-service inventory (``settings.ERP_INTEGRATIONS``).

Shipped in the wheel and imported by ``golden_erp.settings.base``.

Validated at import by ``core/integration_registry.py`` (the golden registry
pattern, like ``ERP_THEMES`` / ``ERP_NAV``) and rendered on Admin →
Integrations. A deployment supplies its own list; the page renders whatever it
is given. Rows reference credentials by setting / environment NAME only, never
by value.

Golden ships the services its own settings read. No row is ``managed`` here:
golden registers no connection tests. An app or plugin that owns an
integration registers one with
``core.integration_registry.register_connection_test("name", func)`` and the
deployment's row lists it under ``connection_tests`` with ``status: "managed"``.
"""

ERP = "erp"

_DEPLOY_OWNER_BASIS = "deployment secret, set in the hosting environment by whoever deploys this instance"

ERP_INTEGRATIONS = [
    {
        "key": "postgres",
        "service": "PostgreSQL: the ERP database",
        "category": "database",
        "consumers": [
            {"app": ERP, "purpose": "Source of truth", "source": "golden_erp/settings/prod.py (DATABASE_URL)"},
        ],
        "credentials": [
            {"name": "DATABASE_URL", "app": ERP, "role": "secret"},
        ],
        "owner": "engineering",
        "owner_basis": _DEPLOY_OWNER_BASIS,
        "status": "bootstrap",
        "bootstrap": True,
    },
    {
        "key": "django_signing",
        "service": "Django signing key and storefront staff session secret",
        "category": "auth",
        "consumers": [
            {"app": ERP, "purpose": "Sessions, CSRF, password-reset tokens, storefront staff SSO",
             "source": "golden_erp/settings/base.py (DJANGO_SECRET_KEY, STAFF_SESSION_SECRET)"},
        ],
        "credentials": [
            {"name": "DJANGO_SECRET_KEY", "app": ERP, "role": "secret"},
            {"name": "STAFF_SESSION_SECRET", "app": ERP, "role": "secret"},
        ],
        "owner": "engineering",
        "owner_basis": _DEPLOY_OWNER_BASIS,
        "status": "bootstrap",
        "bootstrap": True,
    },
    {
        "key": "mailgun",
        "service": "Mailgun: transactional email (production settings)",
        "category": "email",
        "consumers": [
            {"app": ERP, "purpose": "Order confirmations, password reset, invites",
             "source": "golden_erp/settings/prod.py (ANYMAIL)"},
        ],
        "credentials": [
            {"name": "MAILGUN_API_KEY", "app": ERP, "role": "secret"},
            {"name": "MAILGUN_SENDER_DOMAIN", "app": ERP, "role": "config"},
            {"name": "DEFAULT_FROM_EMAIL", "app": ERP, "role": "config"},
        ],
        "owner": "unassigned",
        "status": "inventoried",
    },
    {
        "key": "easypost",
        "service": "EasyPost: shipping rates and labels",
        "category": "shipping",
        "consumers": [
            {"app": ERP, "purpose": "Rates and labels for fulfillment", "source": "fulfillment/label_service.py, fulfillment/carriers/easypost_carrier.py"},
        ],
        "credentials": [
            {"name": "EASYPOST_API_KEY", "app": ERP, "role": "secret"},
        ],
        "owner": "unassigned",
        "status": "inventoried",
    },
    {
        "key": "llm_providers",
        "service": "LLM providers and gateway (llmhub)",
        "category": "llm",
        "consumers": [
            {"app": ERP, "purpose": "Chat assistant and import mapping",
             "source": "golden_erp/settings/base.py (LLMHUB_*), llmhub/"},
        ],
        "credentials": [
            {"name": "ANTHROPIC_API_KEY", "app": ERP, "role": "secret"},
            {"name": "OPENAI_API_KEY", "app": ERP, "role": "secret"},
            {"name": "OPENAI_BASE_URL", "app": ERP, "role": "config"},
            {"name": "LLMHUB_MODE", "app": ERP, "role": "config"},
            {"name": "LLMHUB_GATEWAY_BASE_URL", "app": ERP, "role": "config"},
            {"name": "LLMHUB_GATEWAY_API_KEY", "app": ERP, "role": "secret"},
        ],
        "owner": "unassigned",
        "status": "inventoried",
        "notes": "llmhub has its own per-model connection test on the LLM Settings page.",
    },
    {
        "key": "integration_secrets",
        "service": "Encryption keys for Connected accounts (provider credentials at rest)",
        "category": "auth",
        "consumers": [
            {"app": ERP, "purpose": "Fernet keys for IntegrationCredential, newest first",
             "source": "golden_erp/settings/base.py (GOLDEN_SECRET_KEYS), core/secrets.py"},
        ],
        "credentials": [
            {"name": "GOLDEN_SECRET_KEYS", "app": ERP, "role": "secret"},
        ],
        "owner": "engineering",
        "owner_basis": _DEPLOY_OWNER_BASIS,
        "status": "bootstrap",
        "bootstrap": True,
        "notes": "Rotate with manage.py rotate_secrets; never derived from DJANGO_SECRET_KEY.",
    },
    {
        "key": "sentry",
        "service": "Sentry: error reporting (production settings)",
        "category": "observability",
        "consumers": [
            {"app": ERP, "purpose": "Exception reporting", "source": "golden_erp/settings/prod.py (SENTRY_DSN)"},
        ],
        "credentials": [
            {"name": "SENTRY_DSN", "app": ERP, "role": "secret"},
        ],
        "owner": "unassigned",
        "status": "inventoried",
    },
]
