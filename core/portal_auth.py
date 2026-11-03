"""Customer portal bearer authentication (identity-unification §2.4, §3.4).

``PortalTokenAuthentication`` reads ``Authorization: Bearer <jwt>``, picks the
portal provider by the token's (unverified) ``iss`` claim, verifies the token
with that provider's key only, and hands the claims to the shared resolver
(``core.identity.resolve``). ``request.user`` is the real ``auth.User``;
``core.portal_permissions.IsPortalCustomer`` then sets ``request.customer``.

Providers come from ``GOLDEN_IDENTITY_PROVIDERS`` (``core.identity.providers``).
A token whose issuer matches no portal provider is rejected, never tried
against every key. The class is registered only on portal views, so a portal
token cannot authenticate the staff API and a staff session or golden-issued
token cannot authenticate the portal.
"""

import jwt
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from core.identity.providers import TokenRejected, portal_bearer_providers, provider_for_issuer
from core.identity.resolve import resolve_portal_login


class PortalTokenAuthentication(BaseAuthentication):
    """Verify a portal provider's JWT and resolve it to an ``auth.User``."""

    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:].strip()
        if not portal_bearer_providers():
            raise AuthenticationFailed("Portal authentication is not configured")
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError as exc:
            raise AuthenticationFailed(f"Invalid token: {exc}") from exc
        provider = provider_for_issuer(unverified.get("iss"))
        if provider is None:
            raise AuthenticationFailed("Invalid token: issuer not accepted")
        try:
            claims = provider.verify(token)
        except TokenRejected as exc:
            raise AuthenticationFailed(str(exc)) from exc
        user, _customer = resolve_portal_login(provider, claims, request)
        # Optional backend hook (OIDC: sync role_map roles from the token).
        after = getattr(provider, "after_portal_login", None)
        if after is not None:
            after(user, claims)
        return (user, token)

    def authenticate_header(self, request):
        return "Bearer"


# Pre-step-4 name, kept for deployments that import it.
SupabaseJWTAuthentication = PortalTokenAuthentication
