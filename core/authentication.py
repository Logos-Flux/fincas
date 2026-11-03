"""
DRF authentication backends — JWT Bearer and API-Key.

Every machine caller is a service account (``core/service_accounts.py``,
identity-unification D7, spec §2.6); none of these paths ever resolves to a
human.

1. **Api-Key** (server-to-server)
   Header:  Authorization: Api-Key <key>   (legacy X-API-Key still accepted
            for one release — SEC-109 back-compat, see APIKeyAuthentication)
   - ``gk_<prefix>_<secret>``: a per-integration ``core.ApiKey``; acts as its
     ``svc-<name>`` account, limited to that account's service roles. Revoked
     or expired keys are refused (401). ``manage.py create_api_key``.
   - the ``API_KEY`` env var (deprecated): acts as the dedicated
     ``api-service`` superuser (``GOLDEN_LEGACY_API_KEY_USER``), never "the
     first superuser". Logged on every use.

2. **Bearer JWT** (external clients, short-lived)
   Header:  Authorization: Bearer <jwt>
   Obtain:  POST /api/v1/core/auth/token/ with an Api-Key header.
   Signed (HS256) with ``API_JWT_SIGNING_KEY``, a key of its own, never
   ``API_KEY`` (unset: derived from ``SECRET_KEY``, system check core.W110).
   ``sub`` is ``api-service``, ``storefront`` or ``svc-<name>``; a ``svc-*``
   token also carries ``akp`` (its key's prefix) and dies with that key.
   Stateless otherwise. Default lifetime 1h (less if the scope's
   max_lifetime_s is shorter); max 24h for the full scope, and each plugin
   scope's own max_lifetime_s (up to 30 days; plugin scopes are path-bound)
   for the rest, never past the minting key's expiry.

Bearer tokens carry a ``scope`` claim. Core provides ``"api"`` (full scope,
also assumed for tokens with no claim); plugins register more through
``BasePlugin.get_token_scopes()`` (docs/plugins.md, "API token scopes"). A
bearer whose scope is not registered is refused.

Both are registered in REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]
ahead of SessionAuthentication, so API clients skip CSRF enforcement.

Tests: core/tests/test_authentication.py, test_service_accounts.py
"""

import logging
import posixpath
import secrets
import time

import jwt
from django.conf import settings
from django.utils.crypto import salted_hmac
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .plugins.registry import FULL_API_SCOPE, plugin_registry
from .service_accounts import (
    LEGACY_SUBJECT,
    SERVICE_USERNAME_PREFIX,
    STOREFRONT_SUBJECT,
    ApiKeyRejected,
    active_key,
    get_legacy_api_user,
    legacy_api_username,
    legacy_key_fingerprint,
    resolve_api_key,
    resolve_token_subject,
)

logger = logging.getLogger(__name__)

# JWT issuer for this service — used as the `iss` claim on issued tokens.
JWT_ISSUER = "golden-erp"

# Claims issue_api_jwt owns; neither a scope's claims() nor extra_claims may set them.
RESERVED_CLAIMS = frozenset({"iss", "iat", "exp", "jti", "sub", "scope", "akp", "akh"})

__all__ = [
    "FULL_API_SCOPE",
    "APIKeyAuthentication",
    "BearerTokenAuthentication",
    "get_api_key",
    "get_jwt_signing_key",
    "get_token_scopes",
    "issue_api_jwt",
    "token_scope",
]


def get_token_scopes():
    """Every accepted bearer scope, ``{name: TokenScope}`` (core + plugins)."""
    return plugin_registry.get_token_scopes()


def get_jwt_signing_key() -> str:
    """The HS256 key for golden-issued bearers: ``API_JWT_SIGNING_KEY``.

    Unset, it is a salted derivation of ``SECRET_KEY`` (system check
    core.W110: rotating ``SECRET_KEY`` then voids every bearer). Never
    ``API_KEY``: a leaked integration key must not also be a token mint.
    """
    configured = getattr(settings, "API_JWT_SIGNING_KEY", "") or ""
    if configured:
        return configured
    return salted_hmac("core.authentication.api_jwt", "signing-key").hexdigest()


def _decode(raw: str, key: str) -> dict:
    """Verify signature, issuer and expiry. ``aud`` is left to the scope's
    ``check`` (core has no notion of a destination)."""
    return jwt.decode(
        raw,
        key,
        algorithms=["HS256"],
        issuer=JWT_ISSUER,
        options={"require": ["exp", "iss", "sub"], "verify_aud": False},
    )


def _accepts_api_key_signature() -> bool:
    # Off by default (2026-10-08): a long-lived storefront bearer signed with
    # an old API_KEY would otherwise outlive every other switch. Opt in only
    # for a short, deliberate transition, and only with the legacy path on.
    return bool(getattr(settings, "API_JWT_ACCEPT_API_KEY_SIGNATURE", False)) and (
        legacy_api_username() is not None
    )


def _decode_bearer(raw: str) -> tuple[dict, bool]:
    """Decode with the signing key; during an opted-in transition
    (``API_JWT_ACCEPT_API_KEY_SIGNATURE``, default off), also accept a
    legacy-subject token signed with ``API_KEY`` (minted before step 5).

    The fallback grants nothing new: only ``api-service``/``storefront``
    subjects qualify, and whoever holds ``API_KEY`` already acts as
    ``api-service``. Never for ``svc-*``, never with the legacy path
    disabled.
    Returns ``(payload, signed_with_api_key)``.
    """
    try:
        return _decode(raw, get_jwt_signing_key()), False
    except jwt.InvalidSignatureError:
        api_key = get_api_key()
        if not (api_key and _accepts_api_key_signature()):
            raise
        payload = _decode(raw, api_key)
        if payload.get("sub") not in (LEGACY_SUBJECT, STOREFRONT_SUBJECT):
            raise jwt.InvalidSignatureError("Signature verification failed")
        logger.warning(
            "Deprecated: accepted a bearer signed with API_KEY (sub=%r). Re-mint it; "
            "tokens are now signed with API_JWT_SIGNING_KEY.", payload.get("sub"),
        )
        return payload, True


#: What ``token_scope`` reports for a bearer request that authentication did
#: not accept (or has not run on). Not a valid scope name, so any check against
#: a real scope fails: the helper fails closed.
UNVERIFIED_SCOPE = "!unverified"

_SCOPE_ATTR = "_golden_token_scope"


def token_scope(request) -> str:
    """The bearer scope authentication resolved for this request.

    - An Api-Key request reports its key's scope (full for the legacy key).
    - Session and unauthenticated requests (no ``Bearer`` header) report full
      scope: they carry no scope.
    - A bearer that ``BearerTokenAuthentication`` accepted reports its scope
      (full scope for tokens minted before scopes existed).
    - Any other bearer (garbage, unknown scope, or one authentication never
      ran on) reports ``UNVERIFIED_SCOPE``, which matches no real scope.

    Non-full scopes are already path-bound by authentication; use this to
    narrow further inside a plugin's own API, comparing for equality with
    the scopes you allow (``== FULL_API_SCOPE``), never with ``!=``.
    """
    stashed = getattr(request, _SCOPE_ATTR, None)
    if stashed is not None:
        return stashed
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Bearer "):
        return FULL_API_SCOPE
    return UNVERIFIED_SCOPE


def _stash_scope(request, name: str) -> None:
    setattr(request, _SCOPE_ATTR, name)
    inner = getattr(request, "_request", None)  # DRF Request wraps HttpRequest
    if inner is not None:
        setattr(inner, _SCOPE_ATTR, name)


def _within_prefix(request, prefix: str) -> bool:
    """True when ``path_info`` stays under ``prefix``, raw AND normalized, so
    ``..`` segments (literal, or percent-decoded by Django before we see
    them) cannot carry a path-bound token outside it."""
    path = request.path_info
    normalized = posixpath.normpath(path) + "/"
    return path.startswith(prefix) and normalized.startswith(prefix)


def get_api_key() -> str:
    """Return the configured API_KEY from Django settings.

    Settings (not os.environ) is the single source of truth so that prod reads
    the Fly secret via golden_erp.settings.base.API_KEY while tests can inject one
    with override_settings(API_KEY=...).
    """
    return getattr(settings, "API_KEY", "") or ""


def _get_service_user():
    """The legacy ``API_KEY`` principal: the dedicated ``api-service`` account.

    Kept for callers of the old name. It never returns another superuser and
    never creates one (``core.service_accounts.get_legacy_api_user``); ``None``
    when the legacy path is disabled or the account is missing.
    """
    return get_legacy_api_user()


def issue_api_jwt(
    lifetime_s: int = 3600,
    sub: str = LEGACY_SUBJECT,
    scope: str = FULL_API_SCOPE,
    extra_claims: dict | None = None,
    key=None,
) -> str:
    """Issue a signed JWT for a service principal (``API_JWT_SIGNING_KEY``).

    ``sub`` selects the identity the token authenticates as — never a human:

    - ``"api-service"`` (default): the legacy full-access account. Needs a
      configured ``API_KEY`` and an enabled ``GOLDEN_LEGACY_API_KEY_USER``;
      carries ``akh``, a fingerprint of the key, so rotating or removing
      ``API_KEY`` retires the token.
    - ``"storefront"``: the least-privilege storefront identity
      (core/storefront.py) used by guest storefront flows (F-037). Full scope
      only.
    - ``"svc-<name>"``: a per-integration account; ``key`` must be a usable
      ``core.ApiKey`` of that account. The token carries ``akp`` (the key's
      prefix), so revoking the key revokes the token; a key with a non-full
      scope mints only that scope; ``exp`` never passes the key's expiry.

    ``scope`` must be registered (core's ``"api"`` or a plugin's
    ``TokenScope``). ``lifetime_s`` may not exceed the scope's
    ``max_lifetime_s``. The scope's ``claims()`` and then ``extra_claims``
    are added to the payload; neither may set a reserved claim.
    """
    spec = get_token_scopes().get(scope)
    if spec is None:
        raise ValueError(f"Unknown token scope: {scope!r}")
    if lifetime_s > spec.max_lifetime_s:
        raise ValueError(
            f"lifetime_s {lifetime_s} exceeds scope {scope!r} max of {spec.max_lifetime_s}"
        )
    now = int(time.time())
    exp = now + lifetime_s
    payload = {"iss": JWT_ISSUER, "iat": now, "exp": exp, "jti": secrets.token_hex(16)}
    if sub == LEGACY_SUBJECT:
        if not get_api_key():
            raise ValueError("API_KEY is not configured — cannot issue an api-service JWT")
        if legacy_api_username() is None:
            raise ValueError("The legacy api-service account is disabled (GOLDEN_LEGACY_API_KEY_USER)")
        payload["akh"] = legacy_key_fingerprint()
    elif sub == STOREFRONT_SUBJECT:
        if scope != FULL_API_SCOPE:
            raise ValueError(f"Scope {scope!r} is not valid for the storefront subject")
    elif isinstance(sub, str) and sub.startswith(SERVICE_USERNAME_PREFIX):
        if key is None:
            raise ValueError(f"Subject {sub!r} needs the ApiKey it is minted from")
        try:
            key = active_key(key.prefix, username=sub)
        except ApiKeyRejected as exc:
            raise ValueError(str(exc)) from exc
        if key.scope != FULL_API_SCOPE and scope != key.scope:
            raise ValueError(f"Key {key.prefix} is limited to scope {key.scope!r}")
        if key.expires_at is not None and exp > int(key.expires_at.timestamp()):
            raise ValueError("Token would outlive its API key")
        payload["akp"] = key.prefix
    else:
        raise ValueError(f"Unknown JWT subject: {sub!r}")
    payload["sub"] = sub
    payload["scope"] = scope
    for source in ((spec.claims() if spec.claims else None), extra_claims):
        if not source:
            continue
        clash = RESERVED_CLAIMS & set(source)
        if clash:
            raise ValueError(f"Reserved JWT claims cannot be overridden: {sorted(clash)}")
        payload.update(source)
    return jwt.encode(payload, get_jwt_signing_key(), algorithm="HS256")


def extract_api_key(request) -> str | None:
    """The presented key: ``Authorization: Api-Key <key>``, else the legacy
    ``X-API-Key`` header (SEC-109 back-compat). Shared with the middleware
    and the token endpoint."""
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if auth.startswith(APIKeyAuthentication.API_KEY_PREFIX):
        return auth[len(APIKeyAuthentication.API_KEY_PREFIX) :]
    legacy = request.META.get("HTTP_X_API_KEY", "")
    return legacy or None


class APIKeyAuthentication(BaseAuthentication):
    """Authenticate DRF requests using an Api-Key header.

    Expects: Authorization: Api-Key <key>. Resolution is
    ``core.service_accounts.resolve_api_key`` (shared with
    ``APIKeyMiddleware``): a ``gk_`` key acts as its ``svc-*`` account and a
    revoked/expired/unknown one is a 401; the legacy ``API_KEY`` acts as the
    dedicated ``api-service`` account. A non-``gk_`` value that is not the
    legacy key returns ``None`` (other authenticators may run).

    This is a proper DRF authentication class (unlike the middleware)
    so it bypasses CSRF enforcement that SessionAuthentication adds.

    Back-compat (SEC-109): also accepts the legacy `X-API-Key` header for
    one release so callers can migrate. Remove the X-API-Key fallback next
    release once all clients send `Authorization: Api-Key`.
    """

    API_KEY_PREFIX = "Api-Key "

    def _extract_token(self, request):
        return extract_api_key(request)

    def authenticate(self, request):
        token = self._extract_token(request)
        if token is None:
            return None
        cached = getattr(getattr(request, "_request", request), "_golden_api_key_resolution", None)
        if cached is not None and cached[0] == token and cached[1] is not None:
            resolved = cached[1]  # APIKeyMiddleware already resolved this key
        else:
            try:
                resolved = resolve_api_key(token, request)
            except ApiKeyRejected as exc:
                raise AuthenticationFailed(str(exc))
        if resolved is None:
            return None
        _stash_scope(request, resolved.scope)
        return (resolved.user, resolved.key if resolved.key is not None else "api-key")

    def authenticate_header(self, request):
        return "Api-Key"


class BearerTokenAuthentication(BaseAuthentication):
    """Authenticate DRF requests using a golden-issued Bearer JWT.

    Expects: Authorization: Bearer <jwt>, signed with ``API_JWT_SIGNING_KEY``
    (see ``_decode_bearer`` for the transition fallback), with ``iss``,
    ``exp`` and ``sub``. ``sub`` resolves only to a service principal
    (``core.service_accounts.resolve_token_subject``); anything else is a
    forged or tampered token. Tokens are issued by
    POST /api/v1/core/auth/token/.
    """

    BEARER_PREFIX = "Bearer "

    def authenticate(self, request):
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth.startswith(self.BEARER_PREFIX):
            return None

        token = auth[len(self.BEARER_PREFIX) :]
        if not token:
            return None

        try:
            payload, signed_with_api_key = _decode_bearer(token)
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed("Token expired")
        except jwt.InvalidIssuerError:
            raise AuthenticationFailed("Invalid token issuer")
        except jwt.InvalidTokenError as e:
            raise AuthenticationFailed(f"Invalid token: {e}")

        try:
            user, key = resolve_token_subject(payload, signed_with_api_key=signed_with_api_key)
        except ApiKeyRejected as exc:
            raise AuthenticationFailed(str(exc))

        self._check_scope(request, payload, payload["sub"], key)
        return (user, token)

    def _check_scope(self, request, payload: dict, sub: str, key=None) -> None:
        """Refuse an unregistered scope, then apply what the scope binds.

        Fail-closed: a token whose plugin was uninstalled stops
        authenticating rather than falling back to full scope; a non-full
        scope only authenticates under its ``path_prefix``; ``check`` must
        return exactly True; ``aud`` is refused unless the scope declares
        ``verifies_aud``.
        """
        name = payload.get("scope", FULL_API_SCOPE)
        spec = get_token_scopes().get(name) if isinstance(name, str) else None
        if spec is None:
            raise AuthenticationFailed("Unknown token scope")
        if sub == STOREFRONT_SUBJECT and name != FULL_API_SCOPE:
            raise AuthenticationFailed("Invalid token scope for subject")
        if key is not None and key.scope != FULL_API_SCOPE and name != key.scope:
            raise AuthenticationFailed("Token scope exceeds its API key's scope")
        if "aud" in payload and not spec.verifies_aud:
            raise AuthenticationFailed("Token audience is not verifiable for this scope")
        if name != FULL_API_SCOPE:
            # Registration guarantees a prefix for every non-full scope; refuse
            # here too so a scope without one can never act as full scope.
            if not spec.path_prefix or not _within_prefix(request, spec.path_prefix):
                raise AuthenticationFailed(f"Token scope {name!r} is not valid on this path")
        if spec.check is not None and spec.check(request, payload) is not True:
            raise AuthenticationFailed(f"Token refused by scope {name!r}")
        _stash_scope(request, name)

    def authenticate_header(self, request):
        return "Bearer"
