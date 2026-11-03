"""Identity providers — ``settings.GOLDEN_IDENTITY_PROVIDERS`` (D4, §2.4, step 4).

Same loader shape as ``core/role_registry.py``: a frozen dataclass per entry,
``ImproperlyConfigured`` at import on any bad entry, rebuilt on
``setting_changed``. Building the registry never touches the database and
never reads a secret: secrets are named by setting (``*_setting``) or
environment variable (``*_env``) and read when a token is verified, so
``override_settings`` and key rotation work without a restart.

Built in and always present: ``password`` (``Password``, entry point
``erp``, trusted). It is Django's ``ModelBackend`` and writes no
``UserIdentity`` rows. Shipped backends: ``Password``, ``SupabaseJWT``
(portal) and ``OIDC`` (``core.identity.oidc``: ``erp`` session login and
portal bearer tokens). A deployment declares the rest, e.g. (golden's
``base.py`` default)::

    GOLDEN_IDENTITY_PROVIDERS = [
        {"name": "supabase", "backend": "core.identity.providers.SupabaseJWT",
         "entry_points": ["portal"], "self_register": True,
         "roles_on_create": ["customer"], "link_by_verified_email": True,
         "options": {"jwt_secret_setting": "SUPABASE_JWT_SECRET",
                     "jwks_url_setting": "SUPABASE_JWKS_URL",
                     "issuer_setting": "SUPABASE_ISSUER",
                     "audience": "authenticated",
                     "url_setting": "SUPABASE_URL",
                     "service_role_key_setting": "SUPABASE_SERVICE_ROLE_KEY"}},
    ]

Provider names are persisted in ``UserIdentity.provider``: never rename one in
place. ``local`` and ``legacy_supabase`` are refused (``core.identity``): the
step-3 backfill wrote inert history rows under them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

import jwt
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string

from . import RESERVED_PROVIDER_NAMES
from .base import (  # noqa: F401  (re-exported: the registry's public names)
    DEFAULT_JWT_LEEWAY_S,
    JWKS_REFRESH_MIN_INTERVAL_S,
    InviteFailed,
    Provider,
    TokenRejected,
    _jwks_client,
    _jwks_clients,
    _ThrottledJWKClient,
    jwt_leeway,
)

ENTRY_POINTS = ("erp", "portal")
NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")  # UserIdentity.provider is 64 chars
PASSWORD_PROVIDER = "password"


class Password(Provider):
    """Django's ``ModelBackend``: username + password, ``/erp/`` only."""

    entry_points = ("erp",)
    authentication_backend = "django.contrib.auth.backends.ModelBackend"


class SupabaseJWT(Provider):
    """Supabase Auth access tokens for the customer portal.

    Verifies with the project's JWKS (asymmetric keys, preferred when set) or
    the legacy HS256 secret. ``aud`` is checked, ``exp`` is required, and a
    leeway of ``GOLDEN_IDENTITY_JWT_LEEWAY_S`` (default 30 s) absorbs clock
    skew, including an ``iat`` slightly in the future. When an issuer is
    configured, ``iss`` must equal it; that is also how the portal
    authentication class picks this provider among several.
    """

    entry_points = ("portal",)
    option_names = (
        "jwt_secret", "jwks_url", "issuer", "audience", "url", "service_role_key",
    )
    secret_options = ("jwt_secret", "service_role_key")
    bearer = True
    can_invite = True
    JWKS_ALGORITHMS = ("RS256", "ES256", "EdDSA")

    @classmethod
    def validate_options(cls, options: dict, where: str) -> None:
        super().validate_options(options, where)
        if not any(k.startswith(("jwt_secret", "jwks_url")) for k in options):
            raise ImproperlyConfigured(
                f"{where} needs jwt_secret_setting/_env or jwks_url (one of them)."
            )

    def issuer(self) -> str | None:
        return self.option("issuer")

    def verify(self, token: str) -> dict:
        """Return the verified claims or raise :class:`TokenRejected`.

        The algorithm is fixed by the key, never chosen by the token: an
        HS256 token is checked only against the shared secret, any other
        token only against the JWKS key named by its ``kid``, with exactly
        that key's algorithm. Both may be configured during Supabase's move
        to asymmetric keys, so tokens issued before it keep verifying.
        """
        jwks_url = self.option("jwks_url")
        secret = self.option("jwt_secret")
        try:
            header_alg = jwt.get_unverified_header(token).get("alg")
        except jwt.InvalidTokenError as exc:
            raise TokenRejected(f"Invalid token: {exc}") from exc
        if header_alg == "HS256" and secret:
            key, algorithms = secret, ["HS256"]
        elif jwks_url:
            try:
                signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
            except (jwt.PyJWKClientError, jwt.PyJWTError) as exc:
                raise TokenRejected(f"Invalid token: {exc}") from exc
            algorithm = getattr(signing_key, "algorithm_name", None)
            if algorithm not in self.JWKS_ALGORITHMS:
                raise TokenRejected("Invalid token: unsupported signing key")
            key, algorithms = signing_key.key, [algorithm]
        elif secret:
            key, algorithms = secret, ["HS256"]
        else:
            raise TokenRejected("Portal authentication is not configured")
        issuer = self.issuer()
        options = {"require": ["exp", "sub"]}
        try:
            return jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=self.option("audience", "authenticated"),
                issuer=issuer,  # None: not checked (no issuer configured)
                leeway=jwt_leeway(),
                options=options,
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenRejected("Token expired") from exc
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            # TypeError/ValueError: a key PyJWT cannot use for that algorithm.
            raise TokenRejected(f"Invalid token: {exc}") from exc

    def _admin_headers(self):
        key = self.option("service_role_key")
        if not key:
            return None
        return {"apikey": key, "Authorization": f"Bearer {key}"}

    def admin_user(self, subject: str):
        """The provider's own record of ``subject``, from the Admin API.

        Returns the user dict, ``False`` when the provider says the subject
        does not exist (404), or ``None`` when it cannot tell (no service key,
        transport or upstream error). Used only at link time, never per request.
        """
        import httpx

        headers = self._admin_headers()
        url = str(self.option("url", "")).rstrip("/")
        if headers is None or not url:
            return None
        try:
            resp = httpx.get(f"{url}/auth/v1/admin/users/{subject}", headers=headers, timeout=10)
        except httpx.HTTPError:
            return None
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def confirms_email(self, subject: str, email: str):
        """Does the provider itself say ``subject`` confirmed ``email``?

        True/False from the Admin API (``email_confirmed_at`` set and the
        address equal); ``None`` when it cannot be asked. ``user_metadata``
        is user-writable in Supabase, so a link prefers this answer.
        """
        record = self.admin_user(subject)
        if not record:
            return None if record is None else False
        confirmed = bool(record.get("email_confirmed_at"))
        same = str(record.get("email") or "").strip().lower() == email.strip().lower()
        return confirmed and same

    @classmethod
    def can_confirm(cls, options: dict) -> bool:
        declared = set(options)
        return any(k.startswith("service_role_key") for k in declared) and any(
            k == "url" or k.startswith("url_") for k in declared
        )

    def subject_status(self, subject: str):
        record = self.admin_user(subject)
        if record is None:
            return None
        if record is False:
            return "gone"
        return "live" if record.get("email_confirmed_at") else "unconfirmed"

    def invite(self, email: str, data: dict) -> str:
        """Supabase Admin invite. Returns the provider subject (Supabase user id)."""
        import httpx

        service_role_key = self.option("service_role_key")
        if not service_role_key:
            raise InviteFailed("SUPABASE_SERVICE_ROLE_KEY not configured", status=500)
        url = str(self.option("url", "")).rstrip("/")
        try:
            resp = httpx.post(
                f"{url}/auth/v1/admin/invite",
                headers={
                    "apikey": service_role_key,
                    "Authorization": f"Bearer {service_role_key}",
                    "Content-Type": "application/json",
                },
                json={"email": email, "data": data},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise InviteFailed(f"Supabase request failed: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise InviteFailed("Supabase invite failed", detail=resp.text)
        subject = (resp.json() or {}).get("id")
        if not subject:
            raise InviteFailed("Supabase response missing user id")
        return str(subject)


# Generic OpenID Connect (step 6). Re-exported so entries can name
# "core.identity.providers.OIDC"; it lives in its own module.
from .oidc import OIDC  # noqa: E402, F401

# ── registry ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderConfig:
    name: str                              # persisted in UserIdentity.provider
    backend: str                           # dotted path to a Provider subclass
    entry_points: tuple[str, ...] = ()     # subset of ENTRY_POINTS
    self_register: bool = False            # portal: may create a User on first login
    roles_on_create: tuple[str, ...] = ()  # granted to users this provider creates
    link_by_verified_email: bool = False   # portal: may link an existing Customer
    trusted: bool = False                  # may link/act for staff-kind accounts
    options: dict = field(default_factory=dict)


def _builtin_providers() -> tuple[ProviderConfig, ...]:
    return (
        ProviderConfig(
            PASSWORD_PROVIDER, "core.identity.providers.Password",
            entry_points=("erp",), trusted=True,
        ),
    )


def _as_tuple(value, where, what) -> tuple:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ImproperlyConfigured(f"{where} {what} must be a list of strings.")
    if not all(isinstance(v, str) for v in value):
        raise ImproperlyConfigured(f"{where} {what} must be a list of strings.")
    if len(set(value)) != len(value):
        raise ImproperlyConfigured(f"{where} {what} lists a value twice.")
    return tuple(value)


def _provider_from_config(entry: Any, index: int) -> ProviderConfig:
    where = f"GOLDEN_IDENTITY_PROVIDERS[{index}]"
    if isinstance(entry, ProviderConfig):
        config = entry
    elif isinstance(entry, dict):
        try:
            config = ProviderConfig(**entry)
        except TypeError as exc:
            raise ImproperlyConfigured(f"{where} is not a valid ProviderConfig: {exc}") from exc
    else:
        raise ImproperlyConfigured(
            f"{where} must be a dict of ProviderConfig kwargs, got {type(entry).__name__}."
        )
    if not isinstance(config.name, str) or not NAME_RE.match(config.name):
        raise ImproperlyConfigured(
            f"{where} has name={config.name!r}; use a lower-case slug "
            "([a-z0-9][a-z0-9_-]*, at most 64 characters)."
        )
    where = f"{where} ({config.name})"
    if config.name in RESERVED_PROVIDER_NAMES:
        raise ImproperlyConfigured(
            f"{where}: {config.name!r} is reserved for inert history rows written by the "
            "identity backfill; configuring it would let those rows log someone in."
        )
    for flag in ("self_register", "link_by_verified_email", "trusted"):
        if not isinstance(getattr(config, flag), bool):
            raise ImproperlyConfigured(f"{where} {flag} must be True or False.")
    entry_points = _as_tuple(config.entry_points, where, "entry_points")
    roles_on_create = _as_tuple(config.roles_on_create, where, "roles_on_create")
    if not isinstance(config.options, dict):
        raise ImproperlyConfigured(f"{where} options must be a dict.")
    return replace(config, entry_points=entry_points, roles_on_create=roles_on_create,
                   options=dict(config.options))


def _validate(config: ProviderConfig, where: str) -> type[Provider]:
    """Checks that need the backend class or the role registry."""
    from core.role_registry import ROLES

    try:
        backend = import_string(config.backend)
    except ImportError as exc:
        raise ImproperlyConfigured(f"{where} backend {config.backend!r} cannot be imported: {exc}")
    if not (isinstance(backend, type) and issubclass(backend, Provider)):
        raise ImproperlyConfigured(f"{where} backend {config.backend!r} is not a Provider class.")
    if not config.entry_points:
        raise ImproperlyConfigured(f"{where} declares no entry_points.")
    for point in config.entry_points:
        if point not in ENTRY_POINTS:
            raise ImproperlyConfigured(
                f"{where} has entry point {point!r}; must be one of {', '.join(ENTRY_POINTS)}."
            )
        if point not in backend.entry_points:
            raise ImproperlyConfigured(
                f"{where}: backend {backend.__name__} cannot serve the {point!r} entry point."
            )
    portal = "portal" in config.entry_points
    if config.link_by_verified_email and not portal:
        raise ImproperlyConfigured(f"{where}: link_by_verified_email needs the portal entry point.")
    if config.self_register and not portal:
        raise ImproperlyConfigured(f"{where}: self_register needs the portal entry point.")
    for role_name in config.roles_on_create:
        role = ROLES.get(role_name)
        if role is None:
            raise ImproperlyConfigured(f"{where} roles_on_create names unknown role {role_name!r}.")
        if role.kind == "service" or role.staff:
            # Staff access comes from an admin or a trusted provider's role
            # mapping (step 6), never from "this login created an account".
            raise ImproperlyConfigured(
                f"{where} roles_on_create may not grant {role_name!r}: only non-staff "
                "human roles can be granted on account creation."
            )
    if portal and not any(ROLES[r].portal for r in config.roles_on_create):
        raise ImproperlyConfigured(
            f"{where} serves the portal, so roles_on_create must include a portal role "
            "(e.g. 'customer'); users it creates could not use the portal otherwise."
        )
    backend.validate_options(config.options, where)
    backend.validate_config(config, where)
    if (config.self_register or config.link_by_verified_email) and not backend.can_confirm(
        config.options
    ):
        raise ImproperlyConfigured(
            f"{where}: self_register / link_by_verified_email need a provider the resolver "
            "can ask whether an email is confirmed (for SupabaseJWT: url and "
            "service_role_key options). A token's own verified flag is user-writable."
        )
    return backend


def build_providers() -> dict[str, Provider]:
    """The built-in ``password`` plus ``GOLDEN_IDENTITY_PROVIDERS``, keyed by name."""
    configured = getattr(settings, "GOLDEN_IDENTITY_PROVIDERS", []) or []
    if not isinstance(configured, (list, tuple)):
        raise ImproperlyConfigured(
            "GOLDEN_IDENTITY_PROVIDERS must be a list of ProviderConfig kwargs dicts, "
            f"got {type(configured).__name__}."
        )
    configs = list(_builtin_providers())
    builtin = {c.name for c in configs}
    seen = set(builtin)
    for i, entry in enumerate(configured):
        config = _provider_from_config(entry, i)
        if config.name in builtin:
            raise ImproperlyConfigured(
                f"GOLDEN_IDENTITY_PROVIDERS[{i}] redefines the built-in provider "
                f"{config.name!r}."
            )
        if config.name in seen:
            raise ImproperlyConfigured(f"GOLDEN_IDENTITY_PROVIDERS reuses the name {config.name!r}.")
        seen.add(config.name)
        configs.append(config)
    providers: dict[str, Provider] = {}
    for i, config in enumerate(configs):
        backend = _validate(config, f"identity provider {config.name!r}")
        providers[config.name] = backend(config)
    bearer = [p for p in providers.values() if p.bearer and "portal" in p.config.entry_points]
    seen_issuers: dict[str, str] = {}
    for p in bearer:
        issuer = p.option("issuer") if p.declares("issuer") else None
        if issuer and issuer in seen_issuers:
            raise ImproperlyConfigured(
                f"Portal token providers {seen_issuers[issuer]!r} and {p.config.name!r} "
                f"share the issuer {issuer!r}; dispatch on iss would be ambiguous."
            )
        if issuer:
            seen_issuers[issuer] = p.config.name
    if len(bearer) > 1:
        undeclared = [p.config.name for p in bearer if not p.declares("issuer")]
        if undeclared:
            raise ImproperlyConfigured(
                "Several portal token providers are configured, so each must declare an "
                f"issuer to dispatch on; missing: {', '.join(undeclared)}."
            )
    return providers


# Mutated in place on reload so that `from … import PROVIDERS` holds.
PROVIDERS: dict[str, Provider] = build_providers()


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    if setting not in ("GOLDEN_IDENTITY_PROVIDERS", "GOLDEN_ROLES"):
        return
    rebuilt = build_providers()
    PROVIDERS.clear()
    PROVIDERS.update(rebuilt)


def get_provider(name: str) -> Provider | None:
    return PROVIDERS.get(name)


def portal_bearer_providers() -> list[Provider]:
    return [p for p in PROVIDERS.values() if p.bearer and "portal" in p.config.entry_points]


def provider_for_issuer(iss) -> Provider | None:
    """The portal token provider for an (unverified) ``iss`` claim, or None.

    A provider with an issuer configured matches only that exact issuer. A
    provider with none configured is the fallback, allowed only when it is the
    sole portal token provider (the registry refuses ambiguous configs), so
    a token is never tried against several keys.
    """
    candidates = portal_bearer_providers()
    for provider in candidates:
        if provider.declares("issuer"):
            configured = provider.issuer()
            if configured and iss == configured:
                return provider
    if len(candidates) == 1 and not candidates[0].declares("issuer"):
        return candidates[0]
    if len(candidates) == 1 and candidates[0].declares("issuer") and not candidates[0].issuer():
        # Declared but the setting/variable is empty: behave as undeclared.
        return candidates[0]
    return None


def invite_provider() -> Provider | None:
    """The portal provider that sends invites (the first one able to)."""
    for provider in portal_bearer_providers():
        if provider.can_invite:
            return provider
    return None
