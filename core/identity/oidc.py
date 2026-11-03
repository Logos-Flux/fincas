"""Generic OpenID Connect provider (identity-unification §2.4, §6.2, step 6).

One ``OIDC`` backend serves both entry points of a provider entry:

- **erp** — a session login for ``/erp/`` through the Authorization Code flow
  with PKCE (``core.oidc_views``: ``/erp/oidc/<name>/start/`` and
  ``/callback/``). State, nonce and the PKCE verifier live in the session;
  the ID token is verified against the issuer's JWKS, ``aud`` = client id,
  and its ``nonce``. :func:`resolve_erp_login` maps it to an ``auth.User``.
- **portal** — bearer validation for the customer portal API, through the
  same JWKS machinery as ``SupabaseJWT`` (``core.portal_auth`` dispatches on
  ``iss``), then the shared resolver (``core.identity.resolve``).

Trust (D5) is enforced when the registry is built
(:meth:`OIDC.validate_config`): an ``erp`` entry point and any ``role_map``
need ``trusted: True``, and a ``role_map`` can never name a service role.
At login, a provider links to an *existing* user by email only when it is
trusted and the ID token says ``email_verified: true`` (a boolean), and never
to a superuser (break-glass accounts keep their password).

Role sync (:func:`sync_roles`): at every login, roles named by ``role_map``
for the user's IdP groups are added; roles this provider granted earlier
(recorded in ``UserIdentity.metadata["granted_roles"]``) and no longer
mapped are removed. A role granted by hand is never removed by the IdP. A
token without the groups claim changes nothing (many access tokens omit it).

Hand-rolled on PyJWT + httpx (both already dependencies): discovery, the
token exchange and JWKS verification are a few requests each, and the
verification half is shared with the portal path, which no OIDC client
library covers.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from urllib.parse import quote, urlencode

import jwt
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.utils import timezone

from . import GENERATED_USERNAME_PREFIX
from .base import Provider, TokenRejected, _jwks_client, jwt_leeway

logger = logging.getLogger(__name__)

DEFAULT_SCOPES = ("openid", "email", "profile")
DEFAULT_CLAIMS = {
    "email": "email",
    "email_verified": "email_verified",
    "name": "name",
    "groups": "groups",
}
TOKEN_AUTH_METHODS = ("client_secret_basic", "client_secret_post")
DISCOVERY_TTL_S = 3600
HTTP_TIMEOUT_S = 10
FLOW_TTL_S = 600          # a login started more than 10 minutes ago is refused
MAX_PENDING_FLOWS = 5     # per session (several tabs)
SESSION_KEY = "golden_oidc_flows"
GRANTED_ROLES_KEY = "granted_roles"
MAPPED_ROLES_KEY = "mapped_roles"


class OIDCError(Exception):
    """The provider or its configuration failed (discovery, token endpoint)."""


class LoginRefused(Exception):
    """A session login is refused. ``str()`` is safe to show on the login page."""


# The (provider, subject, email) the last verified token in this context
# vouched for. ``confirms_email`` reads it: the resolver asks after verify()
# in the same request, and an ID/access token signed by the IdP is the IdP's
# own statement (unlike Supabase's user-writable ``user_metadata``).
_confirmed: contextvars.ContextVar[tuple | None] = contextvars.ContextVar(
    "golden_oidc_confirmed", default=None
)

_discovery: dict[tuple, tuple[float, dict]] = {}
_discovery_failed: dict[tuple, float] = {}
DISCOVERY_RETRY_S = 30  # after a failed fetch, fail fast instead of refetching
_discovery_lock = threading.Lock()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class OIDC(Provider):
    """A generic OpenID Connect issuer (Authentik, Keycloak, Entra ID, …)."""

    entry_points = ("erp", "portal")
    option_names = (
        "issuer", "client_id", "client_secret", "discovery_url",
        "authorization_endpoint", "token_endpoint", "jwks_uri", "redirect_uri",
        "scopes", "claims", "role_map", "audience", "label", "token_endpoint_auth",
        "erp_link_by_email",
    )
    secret_options = ("client_secret",)
    #: Structured options are validated when the registry is built, so they
    #: cannot come from a setting or environment variable.
    literal_options = (
        "scopes", "claims", "role_map", "label", "token_endpoint_auth", "erp_link_by_email",
    )
    bearer = True
    session_login = True
    JWKS_ALGORITHMS = (
        "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "EdDSA",
    )

    # ── configuration ───────────────────────────────────────────────────────

    @classmethod
    def validate_options(cls, options: dict, where: str) -> None:
        super().validate_options(options, where)
        for name in cls.literal_options:
            for suffix in ("_setting", "_env"):
                if f"{name}{suffix}" in options:
                    raise ImproperlyConfigured(
                        f"{where} option {name!r} must be written inline (it is "
                        "validated when the registry is built)."
                    )
        for required in ("issuer", "client_id"):
            if not any(k in options for k in (required, f"{required}_setting",
                                               f"{required}_env")):
                raise ImproperlyConfigured(f"{where} needs the {required!r} option.")
        scopes = options.get("scopes", DEFAULT_SCOPES)
        if (isinstance(scopes, str) or not isinstance(scopes, (list, tuple))
                or not all(isinstance(s, str) and s and " " not in s for s in scopes)):
            raise ImproperlyConfigured(f"{where} scopes must be a list of scope names.")
        if "openid" not in scopes:
            raise ImproperlyConfigured(f"{where} scopes must include 'openid'.")
        claims = options.get("claims", {})
        if not isinstance(claims, dict) or not all(
            k in DEFAULT_CLAIMS and isinstance(v, str) and v for k, v in claims.items()
        ):
            raise ImproperlyConfigured(
                f"{where} claims must map some of {', '.join(DEFAULT_CLAIMS)} to claim names."
            )
        role_map = options.get("role_map", {})
        if not isinstance(role_map, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in role_map.items()
        ):
            raise ImproperlyConfigured(f"{where} role_map must map group names to role names.")
        if not isinstance(options.get("erp_link_by_email", False), bool):
            raise ImproperlyConfigured(f"{where} erp_link_by_email must be True or False.")
        label = options.get("label", "")
        if not isinstance(label, str):
            raise ImproperlyConfigured(f"{where} label must be a string.")
        method = options.get("token_endpoint_auth", TOKEN_AUTH_METHODS[0])
        if method not in TOKEN_AUTH_METHODS:
            raise ImproperlyConfigured(
                f"{where} token_endpoint_auth must be one of {', '.join(TOKEN_AUTH_METHODS)}."
            )

    @classmethod
    def validate_config(cls, config, where: str) -> None:
        """Trust rules (D5): checked when the registry is built, never at login."""
        from core.role_registry import ROLES

        if "erp" in config.entry_points and not config.trusted:
            # An /erp/ session exists only for staff, and staff accounts are
            # reachable only through a trusted provider: an untrusted erp
            # provider could only ever be a way in for someone it shouldn't.
            raise ImproperlyConfigured(
                f"{where}: the 'erp' entry point needs trusted=True (back-office "
                "sign-in is staff sign-in)."
            )
        role_map = config.options.get("role_map", {})
        if role_map and not config.trusted:
            raise ImproperlyConfigured(
                f"{where}: role_map needs trusted=True; an untrusted provider's groups "
                "never grant roles."
            )
        for group, role_name in role_map.items():
            role = ROLES.get(role_name)
            if role is None:
                raise ImproperlyConfigured(
                    f"{where} role_map maps {group!r} to unknown role {role_name!r}."
                )
            if role.kind == "service":
                raise ImproperlyConfigured(
                    f"{where} role_map may not grant the service role {role_name!r}."
                )
            if role.staff and "erp" not in config.entry_points:
                # Staff access comes only through the back-office login; a
                # portal-only provider would grant it from a bearer token.
                raise ImproperlyConfigured(
                    f"{where} role_map may grant the staff role {role_name!r} only with "
                    "the 'erp' entry point."
                )

    @classmethod
    def can_confirm(cls, options: dict) -> bool:
        # The ID/access token is signed by the IdP, and email_verified is the
        # IdP's own claim (see claims_from_payload: only a boolean true counts).
        return True

    @property
    def label(self) -> str:
        return self.config.options.get("label") or self.config.name.replace("-", " ").title()

    def issuer(self) -> str | None:
        return self.option("issuer")

    def client_id(self) -> str:
        value = self.option("client_id")
        if not value:
            raise OIDCError(f"{self.config.name}: client_id is not set")
        return str(value)

    def scopes(self) -> tuple[str, ...]:
        return tuple(self.config.options.get("scopes", DEFAULT_SCOPES))

    def claim_name(self, key: str) -> str:
        return self.config.options.get("claims", {}).get(key, DEFAULT_CLAIMS[key])

    def role_map(self) -> dict[str, str]:
        return dict(self.config.options.get("role_map", {}))

    # ── discovery and keys ──────────────────────────────────────────────────

    def discovery_url(self) -> str:
        explicit = self.option("discovery_url")
        if explicit:
            return str(explicit)
        issuer = self.issuer()
        if not issuer:
            raise OIDCError(f"{self.config.name}: issuer is not set")
        return str(issuer).rstrip("/") + "/.well-known/openid-configuration"

    def metadata(self) -> dict:
        """The issuer's discovery document, cached for ``DISCOVERY_TTL_S``.

        Its ``issuer`` must equal the configured one (OIDC Discovery §4.3), so
        a discovery URL cannot redirect trust to another issuer. Cached per
        (URL, issuer). After a failed fetch, calls fail fast for
        ``DISCOVERY_RETRY_S`` so a down IdP plus a stream of tokens (forgeable:
        no signature is checked before discovery) cannot tie up workers.
        """
        url = self.discovery_url()
        key = (url, self.issuer())
        now = time.monotonic()
        with _discovery_lock:
            cached = _discovery.get(key)
            if cached and now - cached[0] < DISCOVERY_TTL_S:
                return cached[1]
            failed = _discovery_failed.get(key)
            if failed is not None and now - failed < DISCOVERY_RETRY_S:
                raise OIDCError(f"{self.config.name}: discovery failed recently")
        try:
            doc = self._fetch_discovery(url)
        except OIDCError:
            with _discovery_lock:
                _discovery_failed[key] = now
            raise
        with _discovery_lock:
            _discovery[key] = (now, doc)
            _discovery_failed.pop(key, None)
        return doc

    def _fetch_discovery(self, url: str) -> dict:
        import httpx

        try:
            resp = httpx.get(url, timeout=HTTP_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise OIDCError(f"{self.config.name}: discovery failed: {exc}") from exc
        if resp.status_code != 200:
            raise OIDCError(f"{self.config.name}: discovery returned {resp.status_code}")
        try:
            doc = resp.json()
        except ValueError as exc:
            raise OIDCError(f"{self.config.name}: discovery is not JSON") from exc
        if not isinstance(doc, dict):
            raise OIDCError(f"{self.config.name}: discovery is not a JSON object")
        if doc.get("issuer") != self.issuer():
            raise OIDCError(
                f"{self.config.name}: discovery issuer {doc.get('issuer')!r} is not the "
                f"configured issuer"
            )
        return doc

    def endpoint(self, name: str) -> str:
        """An endpoint URL: the explicit option, else the discovery document."""
        value = self.option(name) or self.metadata().get(name)
        if not value or not isinstance(value, str):
            raise OIDCError(f"{self.config.name}: no {name}")
        return value

    # ── token verification (shared by both entry points) ────────────────────

    def decode(self, token: str, *, audience: str, nonce: str | None = None) -> dict:
        """Verify signature, ``iss``, ``aud``, ``exp`` (and ``nonce``); raw claims.

        The algorithm comes from the JWKS key the ``kid`` names, never from the
        token, and only asymmetric algorithms are accepted.
        """
        issuer = self.issuer()
        if not issuer:
            raise TokenRejected("Invalid token: issuer not configured")
        try:
            header_alg = jwt.get_unverified_header(token).get("alg")
        except jwt.InvalidTokenError as exc:
            raise TokenRejected(f"Invalid token: {exc}") from exc
        if header_alg not in self.JWKS_ALGORITHMS:
            raise TokenRejected("Invalid token: unsupported algorithm")
        try:
            jwks_uri = self.endpoint("jwks_uri")
        except OIDCError as exc:
            logger.warning("OIDC: %s", exc)
            raise TokenRejected("Sign-in provider unavailable") from exc
        try:
            signing_key = _jwks_client(jwks_uri).get_signing_key_from_jwt(token)
        except (jwt.PyJWKClientError, jwt.PyJWTError) as exc:
            raise TokenRejected(f"Invalid token: {exc}") from exc
        algorithm = getattr(signing_key, "algorithm_name", None)
        if algorithm not in self.JWKS_ALGORITHMS:
            raise TokenRejected("Invalid token: unsupported signing key")
        try:
            payload = jwt.decode(
                token, signing_key.key, algorithms=[algorithm], audience=audience,
                issuer=issuer, leeway=jwt_leeway(),
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenRejected("Token expired") from exc
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise TokenRejected(f"Invalid token: {exc}") from exc
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise TokenRejected("Invalid token: bad sub claim")
        if nonce is not None:
            got = payload.get("nonce")
            if not isinstance(got, str) or not hmac.compare_digest(got, nonce):
                raise TokenRejected("Invalid token: nonce mismatch")
        aud = payload.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and payload.get("azp") != audience:
            raise TokenRejected("Invalid token: azp mismatch")
        return payload

    def claims_from_payload(self, payload: dict) -> dict:
        """Normalise verified claims through the ``claims`` mapping.

        The result is what the resolver reads: ``email_verified`` is True only
        for a boolean ``true``, and nothing nested (no ``user_metadata``) is
        passed on, so no user-writable field can stand in for it.
        """
        email = payload.get(self.claim_name("email"))
        name = payload.get(self.claim_name("name"))
        claims = {
            "sub": payload["sub"],
            "iss": payload.get("iss"),
            "email": email.strip() if isinstance(email, str) else "",
            "email_verified": payload.get(self.claim_name("email_verified")) is True,
        }
        if isinstance(name, str) and name:
            claims["name"] = name
        for key in ("given_name", "family_name", "picture", "phone"):
            if isinstance(payload.get(key), str):
                claims[key] = payload[key]
        groups_claim = self.claim_name("groups")
        if groups_claim in payload:
            groups = payload[groups_claim]
            if isinstance(groups, str):
                groups = [groups]
            if isinstance(groups, list):
                claims["groups"] = [g for g in groups if isinstance(g, str)]
        return claims

    def _remember(self, claims: dict) -> None:
        _confirmed.set(
            (self.config.name, claims["sub"], claims["email"].lower())
            if claims["email_verified"] and claims["email"] else None
        )

    def may_link_privileged(self) -> bool:
        """Resolver hook: link a staff account by email only if erp linking is on."""
        return bool(self.config.options.get("erp_link_by_email", False))

    def verify(self, token: str) -> dict:
        """Portal bearer path: an access (or ID) token for this client."""
        try:
            audience = self.option("audience") or self.client_id()
        except OIDCError as exc:
            logger.warning("OIDC: %s", exc)
            raise TokenRejected("Sign-in provider unavailable") from exc
        claims = self.claims_from_payload(self.decode(token, audience=str(audience)))
        self._remember(claims)
        return claims

    def confirms_email(self, subject: str, email: str):
        """True when the token verified in this request asserted ``email`` verified."""
        remembered = _confirmed.get()
        return bool(remembered) and remembered == (
            self.config.name, subject, email.strip().lower(),
        )

    def after_portal_login(self, user, claims: dict) -> None:
        """Portal requests sync mapped roles too (only when the claim is present)."""
        if not self.role_map() or "groups" not in claims:
            return
        from .models import UserIdentity

        identity = UserIdentity.objects.filter(
            provider=self.config.name, subject=claims["sub"], user=user,
        ).first()
        if identity is not None:
            sync_roles(self, user, identity, claims)

    # ── authorization code + PKCE (the erp entry point) ─────────────────────

    def begin(self, session, redirect_uri: str, next_url: str) -> str:
        """Record a pending flow in the session; return the authorization URL."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        flows = {
            k: v for k, v in (session.get(SESSION_KEY) or {}).items()
            if isinstance(v, dict) and time.time() - v.get("created", 0) < FLOW_TTL_S
        }
        flows[state] = {
            "provider": self.config.name, "nonce": nonce, "verifier": verifier,
            "next": next_url, "redirect_uri": redirect_uri, "created": time.time(),
        }
        if len(flows) > MAX_PENDING_FLOWS:
            for old in sorted(flows, key=lambda k: flows[k]["created"])[:-MAX_PENDING_FLOWS]:
                del flows[old]
        session[SESSION_KEY] = flows
        params = {
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes()),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        endpoint = self.endpoint("authorization_endpoint")
        return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> str:
        """Trade the code for tokens at the token endpoint; return the ID token."""
        import httpx

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": self.client_id(),
        }
        auth = None
        secret = self.option("client_secret")
        if secret:
            method = self.config.options.get("token_endpoint_auth", TOKEN_AUTH_METHODS[0])
            if method == "client_secret_post":
                data["client_secret"] = str(secret)
            else:  # RFC 6749 §2.3.1: form-encode both parts before Basic
                auth = (quote(self.client_id(), safe=""), quote(str(secret), safe=""))
        try:
            resp = httpx.post(
                self.endpoint("token_endpoint"), data=data, auth=auth,
                headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise OIDCError(f"{self.config.name}: token request failed: {exc}") from exc
        if resp.status_code != 200:
            raise OIDCError(
                f"{self.config.name}: token endpoint returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise OIDCError(f"{self.config.name}: token response is not JSON") from exc
        id_token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(id_token, str) or not id_token:
            raise OIDCError(f"{self.config.name}: token response has no id_token")
        return id_token


# ── erp session login ───────────────────────────────────────────────────────


def take_flow(session, provider_name: str, state: str) -> dict:
    """Pop the pending flow for ``state``; refuse unknown, stale or crossed ones."""
    flows = session.get(SESSION_KEY) or {}
    flow = flows.pop(state, None) if isinstance(state, str) and state else None
    session[SESSION_KEY] = flows
    if not isinstance(flow, dict):
        raise LoginRefused("Your sign-in session expired or was started elsewhere. Try again.")
    if flow.get("provider") != provider_name:
        raise LoginRefused("Your sign-in session does not match this provider. Try again.")
    if time.time() - flow.get("created", 0) >= FLOW_TTL_S:
        raise LoginRefused("Your sign-in session expired. Try again.")
    return flow


def complete_login(provider: OIDC, flow: dict, code: str):
    """Exchange the code, verify the ID token against the flow, resolve the user."""
    try:
        id_token = provider.exchange(code, flow["verifier"], flow["redirect_uri"])
        payload = provider.decode(id_token, audience=provider.client_id(), nonce=flow["nonce"])
    except OIDCError as exc:
        logger.warning("OIDC: %s", exc)
        raise LoginRefused("The sign-in provider could not be reached. Try again.") from exc
    except TokenRejected as exc:
        logger.warning("OIDC: %s ID token refused: %s", provider.config.name, exc)
        raise LoginRefused("The sign-in provider's answer could not be verified.") from exc
    return resolve_erp_login(provider, provider.claims_from_payload(payload))


def _staff_role_names() -> set[str]:
    from core.role_registry import ROLES

    return {name for name, role in ROLES.items() if role.staff}


def mapped_roles(provider: OIDC, claims: dict) -> set[str] | None:
    """Roles the IdP groups map to; ``None`` when the token has no groups claim."""
    if "groups" not in claims:
        return None
    role_map = provider.role_map()
    return {role_map[g] for g in claims["groups"] if g in role_map}


def sync_roles(provider: OIDC, user, identity, claims: dict) -> None:
    """Make the mapped roles match the IdP groups (provenance-tracked).

    Adds every mapped role the user lacks; removes roles this provider granted
    before (``identity.metadata["granted_roles"]``) that are no longer mapped,
    unless another identity of the user still maps them (its last-seen
    ``mapped_roles``). Never touches a role it did not grant, never grants a
    service role, and never runs for an untrusted provider (the registry
    refuses a role_map there anyway).
    """
    from core.role_registry import ROLES
    from core.roles import grant_role, revoke_role

    from .models import UserIdentity

    if not provider.config.trusted or not provider.role_map():
        return
    wanted = mapped_roles(provider, claims)
    if wanted is None:
        return
    wanted = {r for r in wanted if r in ROLES and ROLES[r].kind != "service"}
    previously = set((identity.metadata or {}).get(GRANTED_ROLES_KEY) or [])
    held = set(user.groups.filter(name__in=wanted | previously).values_list("name", flat=True))
    added = wanted - held
    removed = (previously - wanted) & held
    if removed:
        for other in UserIdentity.objects.filter(user=user).exclude(pk=identity.pk):
            removed -= set((other.metadata or {}).get(MAPPED_ROLES_KEY) or [])
    for role in sorted(added):
        grant_role(user, role)
    for role in sorted(removed):
        revoke_role(user, role)
    granted = sorted((previously & wanted & held) | added)
    current = identity.metadata or {}
    if granted != sorted(previously) or sorted(wanted) != current.get(MAPPED_ROLES_KEY):
        metadata = dict(current)
        metadata[GRANTED_ROLES_KEY] = granted
        metadata[MAPPED_ROLES_KEY] = sorted(wanted)
        UserIdentity.objects.filter(pk=identity.pk).update(metadata=metadata)
        identity.metadata = metadata
    if added or removed:
        logger.info(
            "OIDC: %s roles for user %s: +%s -%s", provider.config.name, user.pk,
            sorted(added), sorted(removed),
        )


def _claims(claims: dict):
    from .resolve import Claims

    return Claims(claims["sub"], claims["email"], claims["email_verified"], claims)


def _link_by_email(provider: OIDC, claims: dict):
    """An existing user with this verified email, or None. Raises on refusal."""
    from django.contrib.auth.models import User

    from .resolve import HeldIdentity

    email = claims["email"]
    users = list(
        User.objects.annotate(_email_lower=Lower("email"))
        .filter(_email_lower=email.lower()).order_by("pk")[:2]
    )
    if not users:
        return None
    if not provider.config.options.get("erp_link_by_email", False):
        raise LoginRefused(
            "An account with this email already exists. Ask an administrator to "
            "link it to this sign-in."
        )
    if not (provider.config.trusted and claims["email_verified"] and email.isascii()):
        # Never create a second account beside one that owns the address.
        raise LoginRefused(
            "An account with this email exists but the sign-in provider has not "
            "verified the address. Ask an administrator."
        )
    if len(users) > 1:
        logger.warning("OIDC: %d users share the email of %s subject %s; refusing",
                       len(users), provider.config.name, claims["sub"])
        raise LoginRefused("Several accounts use this email. Ask an administrator.")
    user = users[0]
    _refuse_unlinkable(provider, user, claims)
    if not user.is_active:
        raise LoginRefused("This account is disabled.")
    try:
        with transaction.atomic():
            User.objects.select_for_update().filter(pk=user.pk).first()
            identity = _attach(provider, user, claims)
    except HeldIdentity:
        logger.warning("OIDC: user %s already holds another %s subject; refusing %s",
                       user.pk, provider.config.name, claims["sub"])
        raise LoginRefused(
            "This account is linked to a different sign-in. Ask an administrator."
        ) from None
    logger.info("OIDC: linked user %s to %s subject %s by verified email",
                user.pk, provider.config.name, claims["sub"])
    return identity


def _is_service_account(user) -> bool:
    from core.role_registry import ROLES

    service = [name for name, role in ROLES.items() if role.kind == "service"]
    return user.groups.filter(name__in=service).exists()


def _refuse_unlinkable(provider, user, claims: dict) -> None:
    """Superusers (break-glass) and service accounts never get an OIDC session."""
    if user.is_superuser or _is_service_account(user):
        logger.warning("OIDC: refused %s subject %s for user %s (superuser or service)",
                       provider.config.name, claims["sub"], user.pk)
        raise LoginRefused("This account cannot sign in with this provider.")


def _attach(provider, user, claims: dict):
    from .resolve import attach_identity

    return attach_identity(provider, user, _claims(claims))


def _provision(provider: OIDC, claims: dict):
    """A new user for a subject whose IdP groups map to a staff role."""
    from django.contrib.auth.models import User

    from .resolve import names_from_claims

    wanted = mapped_roles(provider, claims) or set()
    if not provider.config.trusted or not (wanted & _staff_role_names()):
        raise LoginRefused("There is no back-office account for this sign-in.")
    first, last = names_from_claims(claims, claims["email"] or claims["sub"])
    with transaction.atomic():
        user = User(
            username=f"{GENERATED_USERNAME_PREFIX}{uuid.uuid4().hex}",
            email=claims["email"].lower() if claims["email_verified"] else "",
            first_name=first[:150], last_name=last[:150], is_active=True,
        )
        user.set_unusable_password()
        user.save()
        identity = _attach(provider, user, claims)
    logger.info("OIDC: provisioned user %s for %s subject %s",
                user.pk, provider.config.name, claims["sub"])
    return identity


def resolve_erp_login(provider: OIDC, claims: dict):
    """Verified ID-token claims → a staff ``auth.User``, or :class:`LoginRefused`.

    1. ``UserIdentity(provider, sub)`` → its user.
    2. Else a user with the same email: linked only when the provider opts in
       (``erp_link_by_email``), is trusted, the token says the email is
       verified, and the user is active and not a superuser or service
       account; refused otherwise (no duplicate is created beside it).
    3. Else a new user, only when the IdP groups map to a staff role.

    Superusers and service accounts are refused on every branch (a superuser
    can reach an identity row through the portal resolver). Then the mapped
    roles are synced, and the user must hold a staff role to get a session.
    """
    from core.permissions import is_staff_principal

    from .models import UserIdentity

    if not provider.config.trusted or "erp" not in provider.config.entry_points:
        raise LoginRefused("This provider cannot sign in to the back office.")
    name = provider.config.name
    identity = UserIdentity.objects.select_related("user").filter(
        provider=name, subject=claims["sub"]).first()
    if identity is None:
        try:
            identity = (_link_by_email(provider, claims) if claims["email"] else None) \
                or _provision(provider, claims)
        except IntegrityError:  # a concurrent first login for the same subject
            identity = UserIdentity.objects.select_related("user").filter(
                provider=name, subject=claims["sub"]).first()
            if identity is None:
                raise LoginRefused("Sign-in failed. Try again.") from None
    user = identity.user
    _refuse_unlinkable(provider, user, claims)
    if ((identity.metadata or {}).get("linked_by") == "email"
            and not provider.config.options.get("erp_link_by_email", False)):
        # Linked to an existing account by the portal resolver's email match:
        # the same link erp_link_by_email=False refuses here.
        logger.warning("OIDC: refused erp login for %s subject %s: identity was linked "
                       "by email on the portal and erp_link_by_email is off",
                       provider.config.name, claims["sub"])
        raise LoginRefused(
            "An account with this email already exists. Ask an administrator to "
            "link it to this sign-in."
        )
    if not user.is_active:
        raise LoginRefused("This account is disabled.")
    sync_roles(provider, user, identity, claims)
    user.refresh_from_db()
    if not is_staff_principal(user):
        raise LoginRefused("This account has no back-office access.")
    UserIdentity.objects.filter(pk=identity.pk).update(
        last_login_at=timezone.now(), email=claims["email"].lower(),
        email_verified=claims["email_verified"],
    )
    return user


def clear_caches() -> None:
    """Forget cached discovery documents (tests, key rotation)."""
    with _discovery_lock:
        _discovery.clear()
        _discovery_failed.clear()
