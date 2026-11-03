"""Service accounts and per-integration API keys (identity-unification D7, §2.6).

Every machine caller is an ``auth.User`` that can never act as a person:

- **``svc-<name>``** — one per integration, created by
  ``manage.py create_api_key``. Unusable password, ``is_staff=False``,
  ``is_superuser=False``, member of one or more ``kind="service"`` roles
  (``GOLDEN_ROLES``). It authenticates only through its ``core.ApiKey`` rows
  (``Authorization: Api-Key gk_<prefix>_<secret>``) or bearer tokens minted
  from one, and ``PrincipalPermission`` gives it exactly what its roles'
  groups permit (:func:`service_contract_allows`), nothing more.
- **``storefront-api``** — the storefront channel identity
  (``core.storefront``), the first instance of this pattern: role
  ``storefront``, public token issuance, same contract check.
- **``api-service``** (``GOLDEN_LEGACY_API_KEY_USER``) — what the legacy
  ``API_KEY`` env var and ``sub="api-service"`` bearers act as. A dedicated
  superuser with an unusable password, never "the first superuser", so the
  audit trail stops naming a human. Deprecated: logged on every use.

``APIKeyAuthentication`` (DRF) and ``APIKeyMiddleware`` (non-DRF ``/api/``
views) both call :func:`resolve_api_key`; ``BearerTokenAuthentication`` calls
:func:`resolve_token_subject`. One resolver each, so the two Api-Key paths
cannot drift.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import SAFE_METHODS

from .storefront import STOREFRONT_USERNAME, ensure_storefront_identity, is_storefront_user

logger = logging.getLogger(__name__)

SERVICE_USERNAME_PREFIX = "svc-"
KEY_PREFIX = "gk_"
#: The bearer subject the legacy key's tokens carry (unchanged wire value).
LEGACY_SUBJECT = "api-service"
#: Storefront bearers keep ``sub="storefront"`` (the storefront reads it).
STOREFRONT_SUBJECT = "storefront"
#: ``last_used_at`` is written at most this often per key (one UPDATE, no save()).
LAST_USED_RESOLUTION = timedelta(seconds=60)

SERVICE_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,59}\Z")
_PREFIX_RE = re.compile(r"\A[0-9a-f]{8}\Z")

# Default HTTP method -> model permission verb for a viewset action. Custom
# @actions (transition, duplicate, recalculate, ...) map to nothing and are
# refused for every service account: their side effects are not described by a
# model permission.
_ACTION_VERBS = {
    "create": "add",
    "update": "change",
    "partial_update": "change",
    "destroy": "delete",
}


class ApiKeyRejected(Exception):
    """A presented key (or a token derived from one) must not authenticate."""


# ── principals ──────────────────────────────────────────────────────────────


def legacy_api_username() -> str | None:
    """Username the legacy ``API_KEY`` acts as; ``None`` disables that path."""
    name = getattr(settings, "GOLDEN_LEGACY_API_KEY_USER", LEGACY_SUBJECT)
    return name or None


def get_legacy_api_user() -> User | None:
    """The dedicated legacy-key account, or ``None`` (disabled or missing).

    Never created on a request: an operator who deletes ``api-service`` has
    switched the legacy key off, and a request must not undo that. It is
    created by migration ``core.0009_api_service_account``, by
    ``manage.py ensure_legacy_api_user``, and (only when core's migrations are
    disabled, i.e. the test database) by a ``post_migrate`` receiver standing
    in for that migration.
    """
    username = legacy_api_username()
    if username is None:
        return None
    return User.objects.filter(username=username).first()


def ensure_legacy_api_user() -> User | None:
    """Create the legacy-key account if missing; return it (provisioning only).

    A superuser *and* ``is_staff`` (spec §2.6: existing integrations keep
    their power, including the ``IsAdminUser`` endpoints the old
    first-superuser reached) with an unusable password, so it can never sign
    in to ``/erp/`` or the admin. An existing account is returned untouched;
    :func:`legacy_user_problem` decides whether it may be used. Same rows as
    migration ``core.0009_api_service_account``.
    """
    username = legacy_api_username()
    if username is None:
        return None
    user, _created = User.objects.get_or_create(
        username=username,
        defaults={
            "email": "api-service@example.com",
            "is_superuser": True,
            "is_staff": True,
            "is_active": True,
            "password": make_password(None),
            # auth.0001 declares last_login NOT NULL (see core.storefront).
            "last_login": timezone.now(),
        },
    )
    return user


def ensure_legacy_api_user_without_migrations(sender, using=None, **kwargs):
    """``post_migrate`` receiver: create the legacy account only where
    migration ``core.0009`` cannot run (core's migrations disabled, as in the
    test settings). With migrations enabled it does nothing, so deleting the
    account stays deleted."""
    from django.db import DEFAULT_DB_ALIAS
    from django.db.migrations.loader import MigrationLoader

    if (using or DEFAULT_DB_ALIAS) != DEFAULT_DB_ALIAS:
        return
    module, _explicit = MigrationLoader.migrations_module("core")
    if module is None:
        ensure_legacy_api_user()


def legacy_user_problem(user) -> str | None:
    """Why the legacy account must not be used, or ``None``.

    An account with a usable password, or one holding an external login, is a
    person (``GOLDEN_LEGACY_API_KEY_USER`` pointed at a human, or someone set
    a password on ``api-service``): refuse it rather than act as them or wipe
    their password.
    """
    if not user.is_active:
        return "inactive"
    if user.has_usable_password():
        return "has a usable password (a human login?)"
    from .identity import RESERVED_PROVIDER_NAMES
    from .identity.models import UserIdentity

    # `local` / `legacy_supabase` rows are written by backfills for every
    # account (identity step 3), the service account included: not a login.
    if UserIdentity.objects.filter(user_id=user.pk).exclude(
        provider__in=RESERVED_PROVIDER_NAMES
    ).exists():
        return "has an external login (a human?)"
    return None


def legacy_key_fingerprint() -> str:
    """Short fingerprint of the current ``API_KEY``; ``api-service`` bearers
    carry it as ``akh`` so rotating the key retires them (as it did when the
    key signed them)."""
    from django.utils.crypto import salted_hmac

    from .authentication import get_api_key

    return salted_hmac("core.service_accounts.api_key_fp", get_api_key()).hexdigest()[:32]


_SERVICE_ATTR = "_golden_is_service_account"


def service_role_names() -> tuple[str, ...]:
    from .role_registry import ROLES

    return tuple(name for name, role in ROLES.items() if role.kind == "service")


def is_service_account(user) -> bool:
    """True for a machine principal: ``svc-*``, the storefront identity, a
    member of a ``kind="service"`` role, or a holder of an ``ApiKey``.

    Fail-closed by design: anything that looks like a service is held to the
    service contract, even if someone also made it staff. The legacy
    ``api-service`` superuser is *not* one (it is the deprecated full-access
    path, a superuser by spec). Cached on the user instance.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    cached = getattr(user, _SERVICE_ATTR, None)
    if cached is not None:
        return cached
    username = getattr(user, "username", "") or ""
    result = username.startswith(SERVICE_USERNAME_PREFIX) or is_storefront_user(user)
    if not result and getattr(user, "pk", None) is not None:
        from .models import ApiKey

        result = (
            user.groups.filter(name__in=service_role_names()).exists()
            or ApiKey.objects.filter(user_id=user.pk).exists()
        )
    try:
        setattr(user, _SERVICE_ATTR, result)
    except AttributeError:  # pragma: no cover - exotic user objects
        pass
    return result


def _view_model(view):
    queryset = getattr(view, "queryset", None)
    if queryset is not None:
        return queryset.model
    return None


def service_contract_allows(request, view) -> bool:
    """A service account may do exactly what its groups' model perms say.

    Safe methods need ``view_<model>``; a viewset's ``create`` /
    ``update``/``partial_update`` / ``destroy`` need ``add_`` / ``change_`` /
    ``delete_``. Custom actions and views without a model are refused. Writes
    need a pinned company (``--company``), except for the storefront, whose
    only write verb is ``add`` (guest orders) whatever its group holds: the
    old storefront contract, unchanged.
    """
    model = _view_model(view)
    if model is None:
        return False
    user = request.user
    # has_perm() is True for everything on an active superuser, so a flagged
    # service account would escape its contract: refuse it instead (staff
    # too, for symmetry with the resolver's soundness check).
    if (
        not getattr(user, "is_active", False)
        or getattr(user, "is_superuser", False)
        or getattr(user, "is_staff", False)
    ):
        return False
    app_label = model._meta.app_label
    model_name = model._meta.model_name
    if request.method in SAFE_METHODS:
        return user.has_perm(f"{app_label}.view_{model_name}")
    verb = _ACTION_VERBS.get(getattr(view, "action", None))
    if verb is None:
        return False
    if is_storefront_user(user):
        # The public storefront identity only ever creates (guest orders),
        # whatever an admin adds to its group.
        if verb != "add":
            return False
    elif service_company(user) is None:
        # Reads by an unpinned account fail closed in scope_to_company, but
        # CompanyScopedViewSet only polices writes for a resolvable company:
        # an unpinned service account must not write at all.
        return False
    return user.has_perm(f"{app_label}.{verb}_{model_name}")


def service_company(user):
    """The company a service account is pinned to (its UserProfile), or None."""
    return getattr(getattr(user, "userprofile", None), "company", None)


def service_account_user_ids():
    """Queryset of the pks of every service account (for bulk exclusions)."""
    from .models import ApiKey

    q = Q(username__startswith=SERVICE_USERNAME_PREFIX) | Q(username=STOREFRONT_USERNAME)
    if legacy_api_username():
        q |= Q(username=legacy_api_username())
    q |= Q(groups__name__in=service_role_names())
    q |= Q(pk__in=ApiKey.objects.values("user_id"))
    return User.objects.filter(q).values("pk")


def _service_user_is_sound(user) -> bool:
    """A per-key account must still look like one, or its keys stop working.

    Someone giving ``svc-x`` a password, staff or superuser in the admin turns
    it into something a key must not act as; refuse rather than guess.
    """
    return (
        user.is_active
        and not user.is_staff
        and not user.is_superuser
        and not user.has_usable_password()
        and user.username.startswith(SERVICE_USERNAME_PREFIX)
    )


# ── provisioning ────────────────────────────────────────────────────────────


def service_username(name: str) -> str:
    return f"{SERVICE_USERNAME_PREFIX}{name}"


def ensure_service_account(name: str, roles=(), company=None) -> User:
    """Create (or extend) ``svc-<name>``. Refuses to adopt a human account.

    ``roles`` must be ``kind="service"`` roles; they are added (never removed)
    to the account's groups. ``company`` gives the account a ``UserProfile``
    pinned to that company, so company-scoped endpoints its roles permit
    return that company's rows (without one they fail closed to none).
    """
    from .role_registry import ROLES
    from .roles import _permission, ensure_role

    if not SERVICE_NAME_RE.match(name or ""):
        raise ValueError(
            f"Service name {name!r} must be a lower-case slug ([a-z0-9][a-z0-9-]*, ≤60)."
        )
    for role in roles:
        config = ROLES.get(role)
        if config is None:
            raise ValueError(f"Unknown role {role!r} (declare it in GOLDEN_ROLES).")
        if config.kind != "service":
            raise ValueError(f"Role {role!r} is a {config.kind} role; a service account takes service roles only.")
    username = service_username(name)
    with transaction.atomic():
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "email": "",
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
                "password": make_password(None),
                "last_login": timezone.now(),
            },
        )
        if not created and not _service_user_is_sound(user):
            raise ValueError(
                f"{username} exists but is not a sound service account (active, no "
                "password, not staff, not superuser). Fix or remove it first."
            )
        for role in roles:
            group = ensure_role(role)[0]
            for perm in ROLES[role].permissions:  # declared perms are a floor
                permission = _permission(perm)
                if permission is not None:
                    group.permissions.add(permission)
            user.groups.add(group)
        if company is not None:
            from .models import UserProfile

            profile = UserProfile.objects.filter(user=user).first()
            if profile is None:
                UserProfile.objects.create(user=user, company=company, display_name=username)
            elif profile.company_id != company.pk:
                raise ValueError(f"{username} is already pinned to company {profile.company}.")
    return user


def _hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def issue_api_key(user, *, name: str, scope: str = "api", expires_at=None, created_by=None):
    """Create an ``ApiKey`` for service account ``user``; return ``(key, raw)``.

    ``raw`` is the only copy of the secret: show it once, never store it.
    """
    from .authentication import get_token_scopes
    from .models import ApiKey

    if not _service_user_is_sound(user):
        raise ValueError(f"{user.username} is not a sound service account; refusing to key it.")
    if scope not in get_token_scopes():
        raise ValueError(f"Unknown token scope {scope!r}.")
    if expires_at is not None and expires_at <= timezone.now():
        raise ValueError("expires_at is in the past.")
    for _ in range(10):
        prefix = secrets.token_hex(4)
        if not ApiKey.objects.filter(prefix=prefix).exists():
            break
    else:  # pragma: no cover - 2**32 space
        raise RuntimeError("Could not allocate a free key prefix.")
    raw = f"{KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    key = ApiKey.objects.create(
        user=user, name=name, prefix=prefix, key_hash=_hash(raw), scope=scope,
        expires_at=expires_at, created_by=created_by,
    )
    return key, raw


def revoke(queryset) -> int:
    """Revoke every still-active key in ``queryset``; returns the count."""
    return queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())


# ── resolution ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApiKeyAuth:
    """What a presented Api-Key resolved to."""

    user: User
    key: object | None  # core.ApiKey, or None for the legacy API_KEY
    scope: str

    @property
    def is_legacy(self) -> bool:
        return self.key is None


def _touch(key) -> None:
    """Record use without a save(): one conditional UPDATE, at most per minute."""
    now = timezone.now()
    if key.last_used_at is not None and now - key.last_used_at < LAST_USED_RESOLUTION:
        return
    type(key).objects.filter(pk=key.pk).filter(
        Q(last_used_at__isnull=True) | Q(last_used_at__lt=now - LAST_USED_RESOLUTION)
    ).update(last_used_at=now)
    key.last_used_at = now


INVALID_KEY = "Invalid API key"


def _key_problem(key, username: str | None = None) -> str | None:
    if username is not None and key.user.username != username:
        return "belongs to another subject"
    if key.revoked_at is not None:
        return "revoked"
    if not key.is_usable():
        return "expired"
    if not _service_user_is_sound(key.user):
        return f"account {key.user.username} is no longer a sound service account"
    return None


def active_key(prefix: str, *, username: str | None = None):
    """The usable ``ApiKey`` with this prefix whose account is still sound.

    Raises ``ApiKeyRejected`` (one generic message; the reason is logged at
    info) otherwise. ``username`` additionally pins the owner (bearer ``sub``).
    Callers holding a raw key must compare its hash *before* trusting this
    (:func:`resolve_api_key`), so a key's status is never told to someone
    who does not hold it.
    """
    from .models import ApiKey

    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        raise ApiKeyRejected(INVALID_KEY)
    key = ApiKey.objects.select_related("user").filter(prefix=prefix).first()
    if key is None:
        raise ApiKeyRejected(INVALID_KEY)
    problem = _key_problem(key, username)
    if problem is not None:
        logger.info("API key %s refused: %s", key.prefix, problem)
        raise ApiKeyRejected(INVALID_KEY)
    return key


def _check_key_scope(scope_name: str, request, enforce_path: bool):
    """The key's scope must still be registered; a non-full scope binds paths.

    A scope with a ``check`` callable judges token claims, which a raw key
    does not have, so such keys only mint bearers (the token endpoint passes
    ``enforce_path=False``) and are refused on direct use.
    """
    from .authentication import FULL_API_SCOPE, _within_prefix, get_token_scopes

    spec = get_token_scopes().get(scope_name)
    if spec is None:
        raise ApiKeyRejected("API key scope is not registered")
    if scope_name == FULL_API_SCOPE or not enforce_path:
        return
    if not spec.path_prefix or not _within_prefix(request, spec.path_prefix):
        raise ApiKeyRejected(f"API key scope {scope_name!r} is not valid on this path")
    if spec.check is not None:
        raise ApiKeyRejected(
            f"API key scope {scope_name!r} requires a bearer token; POST /api/v1/core/auth/token/"
        )


_DUMMY_HASH = "0" * 64


def resolve_api_key(raw: str, request=None, *, enforce_path: bool = True) -> ApiKeyAuth | None:
    """Resolve a presented Api-Key. Shared by the DRF class and the middleware.

    - equal to ``settings.API_KEY`` → the legacy user (logged as deprecated on
      every use). Checked first, so a legacy key of any shape keeps working.
    - ``gk_<prefix>_<secret>``: its ``ApiKey``. The hash is compared
      (constant time) before anything about the key is revealed; revoked or
      expired keys and unsound accounts → ``ApiKeyRejected``.
    - anything else → ``None`` (not ours; other authenticators may run).
    """
    from .authentication import FULL_API_SCOPE, get_api_key
    from .models import ApiKey

    if not raw:
        return None
    digest = _hash(raw)
    legacy = get_api_key()
    if legacy and hmac.compare_digest(digest, _hash(legacy)):
        if legacy_api_username() is None:
            raise ApiKeyRejected("The legacy API_KEY is disabled (GOLDEN_LEGACY_API_KEY_USER)")
        user = get_legacy_api_user()
        if user is None:
            logger.error(
                "Legacy API_KEY refused: account %r does not exist (deleted, or never "
                "migrated). `manage.py ensure_legacy_api_user` recreates it.",
                legacy_api_username(),
            )
            raise ApiKeyRejected(INVALID_KEY)
        problem = legacy_user_problem(user)
        if problem is not None:
            logger.error("Legacy API_KEY refused: account %r %s", user.username, problem)
            raise ApiKeyRejected(INVALID_KEY)
        logger.warning(
            "Deprecated: request authenticated with the legacy API_KEY as %r. Issue a "
            "per-integration key with `manage.py create_api_key` (docs/identity-unification.md §2.6).",
            user.username,
        )
        return ApiKeyAuth(user=user, key=None, scope=FULL_API_SCOPE)
    if not raw.startswith(KEY_PREFIX):
        return None
    parts = raw.split("_", 2)
    key = None
    if len(parts) == 3 and _PREFIX_RE.match(parts[1]):
        key = ApiKey.objects.select_related("user").filter(prefix=parts[1]).first()
    # Compare even when there is no key, so an unknown prefix costs the same.
    if not hmac.compare_digest(key.key_hash if key is not None else _DUMMY_HASH, digest) or key is None:
        raise ApiKeyRejected(INVALID_KEY)
    problem = _key_problem(key)
    if problem is not None:
        logger.info("API key %s refused: %s", key.prefix, problem)
        raise ApiKeyRejected(INVALID_KEY)
    _check_key_scope(key.scope, request, enforce_path)
    _touch(key)
    return ApiKeyAuth(user=key.user, key=key, scope=key.scope)


def resolve_token_subject(payload: dict, *, signed_with_api_key: bool = False) -> tuple[User, object | None]:
    """Map a verified golden bearer's ``sub`` to its service principal.

    Never a human: the only subjects are the legacy ``api-service`` (needs a
    configured ``API_KEY`` and the ``akh`` fingerprint of that same key, so
    removing or rotating it retires its tokens), the
    storefront, and ``svc-*`` whose ``akp`` claim names a still-usable key of
    that same account (so revoking or expiring the key kills its bearers).
    """
    from .authentication import get_api_key

    sub = payload.get("sub")
    if sub == LEGACY_SUBJECT:
        user = get_legacy_api_user() if get_api_key() else None
        if user is None or legacy_user_problem(user) is not None:
            raise ApiKeyRejected("Invalid token subject")
        # Bound to the API_KEY that was current at issuance (``akh``), so
        # rotating the key retires its tokens. A token signed with API_KEY
        # itself (transition) is bound by its signature instead.
        if not signed_with_api_key and not hmac.compare_digest(
            str(payload.get("akh", "")), legacy_key_fingerprint()
        ):
            raise ApiKeyRejected("Token was issued for a rotated API_KEY")
        return user, None
    if sub == STOREFRONT_SUBJECT:
        return ensure_storefront_identity(), None
    if isinstance(sub, str) and sub.startswith(SERVICE_USERNAME_PREFIX) and sub != STOREFRONT_USERNAME:
        key = active_key(payload.get("akp"), username=sub)
        _touch(key)
        return key.user, key
    raise ApiKeyRejected("Invalid token subject")
