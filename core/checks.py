"""System checks: roles and derived ``is_staff`` (identity-unification §2.3),
service-account auth settings (§2.6) and field definitions (phase4-fields.md 4.1).

They read the database, so they are registered with the ``database`` tag:
Django runs them during ``migrate`` and ``check --database default``, not on
every ``manage.py`` invocation. A database without the auth tables yet is
skipped silently.
"""

from __future__ import annotations

from django.core.checks import Error, Tags, Warning, register
from django.db.utils import OperationalError, ProgrammingError

_SAMPLE = 10


def _names(queryset) -> str:
    names = list(queryset.values_list("username", flat=True)[: _SAMPLE + 1])
    shown = ", ".join(names[:_SAMPLE])
    return shown + (", …" if len(names) > _SAMPLE else "")


@register(Tags.database)
def roles_check(app_configs, databases=None, **kwargs):
    if not databases:
        return []
    from django.contrib.auth.models import Group

    from .role_registry import ROLES, STAFF_ROLE
    from .roles import _permission, unadopted_staff, unflagged_staff

    errors = []
    try:
        # Before the staff group exists, creating it adopts these users
        # (migrate / sync_roles), so W101 would be a false alarm; W103 says so.
        demote = unadopted_staff()
        staff_group_exists = Group.objects.filter(name=STAFF_ROLE).exists()
        if staff_group_exists and demote.exists():
            errors.append(Warning(
                f"{demote.count()} staff user(s) hold no staff role and would lose "
                f"is_staff when derivation next touches them: {_names(demote)}.",
                hint=(
                    "is_staff is derived from role membership (GOLDEN_ROLES). Run "
                    "`manage.py sync_roles --adopt-existing` to add them to the "
                    f"{STAFF_ROLE!r} role, or remove their is_staff flag on purpose."
                ),
                id="core.W101",
            ))
        promote = unflagged_staff()
        if promote.exists():
            errors.append(Warning(
                f"{promote.count()} user(s) hold a staff role but not is_staff: "
                f"{_names(promote)}.",
                hint="Run `manage.py sync_roles` to re-derive is_staff.",
                id="core.W102",
            ))
        existing_groups = set(Group.objects.filter(name__in=list(ROLES)).values_list(
            "name", flat=True))
        missing_groups = [name for name in ROLES if name not in existing_groups]
        if missing_groups:
            errors.append(Warning(
                f"Role group(s) not created yet: {', '.join(missing_groups)}.",
                hint="Run `manage.py migrate` or `manage.py sync_roles`.",
                id="core.W103",
            ))
        unknown = [
            f"{role.name}: {perm}"
            for role in ROLES.values()
            for perm in role.permissions
            if _permission(perm) is None
        ]
        if unknown:
            errors.append(Warning(
                f"GOLDEN_ROLES declares permission(s) that do not exist: {'; '.join(unknown)}.",
                hint="Check the app_label.codename spelling, or migrate the app that defines it.",
                id="core.W104",
            ))
    except (OperationalError, ProgrammingError):
        return []
    return errors


@register(Tags.security)
def api_auth_settings_check(app_configs, **kwargs):
    """Service-account auth configuration (identity-unification §2.6)."""
    from django.conf import settings

    errors = []
    signing = getattr(settings, "API_JWT_SIGNING_KEY", "") or ""
    api_key = getattr(settings, "API_KEY", "") or ""
    if not signing:
        errors.append(Warning(
            "API_JWT_SIGNING_KEY is unset; bearer tokens are signed with a key derived "
            "from SECRET_KEY.",
            hint="Set API_JWT_SIGNING_KEY to a dedicated random secret. Until then, "
                 "rotating SECRET_KEY invalidates every issued bearer token.",
            id="core.W110",
        ))
    elif api_key and signing == api_key:
        errors.append(Warning(
            "API_JWT_SIGNING_KEY equals API_KEY.",
            hint="Use a separate secret: whoever holds the API key could otherwise "
                 "mint tokens for any service subject.",
            id="core.W111",
        ))
    if api_key and getattr(settings, "GOLDEN_LEGACY_API_KEY_USER", "api-service"):
        errors.append(Warning(
            "API_KEY (the shared legacy key) is configured; it acts as the "
            f"{settings.GOLDEN_LEGACY_API_KEY_USER!r} superuser and is deprecated.",
            hint="Give each integration its own key (`manage.py create_api_key`), set "
                 "GOLDEN_STOREFRONT_TOKENS=1 if the storefront issues tokens, then unset "
                 "API_KEY. docs/identity-unification.md §2.6.",
            id="core.W112",
        ))
    return errors


@register(Tags.security)
def trusted_proxies_check(app_configs, **kwargs):
    """``TRUSTED_PROXIES`` parses, and is set when the deployment sits behind a
    proxy (F-087): every throttle keyed by client address (login, OIDC start,
    token, coupon) otherwise sees the proxy's address and lumps every visitor
    into one bucket."""
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    from .client_ip import _networks

    raw = getattr(settings, "TRUSTED_PROXIES", None) or ()
    entries = tuple(str(e).strip() for e in raw if str(e).strip())  # as client_ip reads it
    try:
        _networks(entries)
    except ImproperlyConfigured as exc:
        return [Error(
            str(exc),
            hint="TRUSTED_PROXIES is a comma-separated list of IP addresses or CIDRs "
                 "(env TRUSTED_PROXIES). Until it parses, every login POST fails.",
            id="core.E080",
        )]
    if getattr(settings, "SECURE_PROXY_SSL_HEADER", None) and not entries:
        return [Warning(
            "SECURE_PROXY_SSL_HEADER is set (the deployment runs behind a proxy) but "
            "TRUSTED_PROXIES is empty, so the login, OIDC, token and coupon throttles "
            "key on the proxy's address: 20 bad sign-ins by anyone lock a username "
            "for everyone (F-087).",
            hint="Set TRUSTED_PROXIES to the proxy's address or network.",
            id="core.W114",
        )]
    return []


#: Secrets a production deployment must set itself (docs/deployment.md,
#: "Required secrets"). Each would otherwise derive from SECRET_KEY or, for
#: GOLDEN_SECRET_KEYS, leave connected accounts unable to store a credential.
DEPLOY_SECRETS = {
    "API_JWT_SIGNING_KEY": "signs API bearer tokens",
    "LLMHUB_FERNET_KEY": "encrypts the stored LLM gateway token",
    "GOLDEN_SECRET_KEYS": "encrypts connected-account credentials",
}


@register(Tags.security)
def deploy_secrets_check(app_configs, **kwargs):
    """Production must set every DEPLOY_SECRETS key (``GOLDEN_REQUIRE_DEPLOY_SECRETS``).

    An Error, so ``migrate`` — run on every deploy, whatever the host — stops
    the release instead of a web server booting on derived keys.
    """
    from django.conf import settings

    if not getattr(settings, "GOLDEN_REQUIRE_DEPLOY_SECRETS", False):
        return []
    missing = [name for name in DEPLOY_SECRETS if not getattr(settings, name, None)]
    if not missing:
        return []
    return [Error(
        "Production secrets are not set: " + ", ".join(
            f"{name} ({DEPLOY_SECRETS[name]})" for name in missing) + ".",
        hint="Set them in the environment (Fly secrets, the compose .env, a systemd "
             "EnvironmentFile, a Kubernetes Secret, ...). `manage.py generate_secrets` "
             "prints a fresh set; docs/deployment.md, \"Required secrets\", covers "
             "setting them on a running install.",
        id="core.E060",
    )]


@register(Tags.database)
def field_definitions_check(app_configs, databases=None, **kwargs):
    """A FieldDefinition scoped to a company that does not exist (phase4-fields.md
    4.1). Settings load before the database, so the loader cannot check it.

    ``core.W113``: the spec named ``core.W110``, which was already the
    ``API_JWT_SIGNING_KEY`` warning.
    """
    if not databases:
        return []
    from .field_registry import all_company_ids
    from .models import Company

    wanted = all_company_ids()
    if not wanted:
        return []
    try:
        found = {str(pk) for pk in Company.objects.filter(pk__in=list(wanted)).values_list("pk", flat=True)}
    except (OperationalError, ProgrammingError):
        return []
    missing = sorted(wanted - found)
    if not missing:
        return []
    return [Warning(
        f"GOLDEN_FIELD_DEFINITIONS names Company id(s) that do not exist: {', '.join(missing)}.",
        hint="Fix the companies list of those definitions; until then they apply to no tenant.",
        id="core.W113",
    )]


@register()
def outbox_consumers_check(app_configs, **kwargs):
    """``core.E050``: two owners claim one outbox consumer name. ``emit``
    would log and drop every durable row until it is fixed, so fail early."""
    from django.core.checks import Error
    from django.core.exceptions import ImproperlyConfigured

    from core import outbox

    try:
        outbox.consumers()
    except ImproperlyConfigured as exc:
        return [Error(str(exc), id="core.E050")]
    return []


# --- one instance per company (release-hardening-plan.md 9.6, decision 14) ----

ONE_COMPANY_DOC = "docs/deployment.md#one-instance-per-company"
ONE_COMPANY_CODE = "one_company_per_instance"


def one_company_message(companies) -> str:
    """The E070 text, also used by the admin and API refusals.

    ``companies`` is an iterable of ``(pk, name)``; the message lists each
    one so an operator can see what to keep.
    """
    companies = list(companies)
    n = len(companies)
    listed = "; ".join(f"{pk}: {name}" for pk, name in companies)
    return (
        f"{n} compan{'y' if n == 1 else 'ies'} exist{'s' if n == 1 else ''} ({listed}). "
        "Fincas runs one company per instance; keep one Company and run a separate "
        f"instance for another company. See {ONE_COMPANY_DOC}."
    )


@register(Tags.database)
def one_company_check(app_configs, databases=None, **kwargs):
    """``core.E070``: more than one Company in the database.

    ``Tags.database`` so it runs on ``migrate`` and ``check --database
    default``, never on ``runserver`` or a plain ``check``. Before
    ``core_company`` exists (the first ``migrate``) it passes. No opt-out:
    ``Company.save`` stays permissive (tests and ``seed_test_edge`` build two
    companies to keep proving the per-company scoping), the check is the
    guard for a real database.
    """
    if not databases:
        return []
    from django.core.checks import Error

    from .models import Company

    try:
        companies = list(Company.objects.order_by("name", "pk").values_list("pk", "name"))
    except (OperationalError, ProgrammingError):
        return []
    if len(companies) <= 1:
        return []
    return [Error(
        one_company_message(companies),
        hint="Keep one Company and move the other company to its own instance.",
        id="core.E070",
    )]
