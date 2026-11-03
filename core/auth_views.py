"""
Token endpoints — issue short-lived JWTs to service principals.

POST /api/v1/core/auth/token/
  Authorization: Api-Key <key>
  Body: {}  (optional — {"lifetime_s": 3600} for a shorter/longer token,
             {"scope": "<name>"} for a plugin-registered narrowed scope; see
             docs/plugins.md)

  - A per-integration key (``gk_…``) mints ``sub = svc-<name>``, its own
    account, carrying ``akp`` so the token dies with the key. A key limited
    to a scope mints only that scope; no token outlives its key. ``subject``
    may only name the key's own account.
  - The legacy ``API_KEY`` mints ``sub = "api-service"`` (default) or, with
    {"subject": "storefront"}, the least-privilege storefront identity.

Returns {"token": "<jwt>", "subject": ..., "scope": ..., "expires_in_s": ...,
         "expires_at": <unix-ts>}

POST /api/v1/core/auth/storefront-token/   (public, throttled)
  Body: {}

Issues a short-lived JWT for the storefront service identity (F-037).
The storefront SPA is a public client — every visitor's browser can hold
this token — so it must carry only the low-privilege storefront identity,
never the api-service superuser. Rate-limited per IP.
"""

import time

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .authentication import (
    FULL_API_SCOPE,
    extract_api_key,
    get_api_key,
    get_token_scopes,
    issue_api_jwt,
)
from .service_accounts import LEGACY_SUBJECT, STOREFRONT_SUBJECT, ApiKeyRejected, resolve_api_key
from .throttling import CacheTolerantThrottle, TokenObtainThrottle

VALID_SUBJECTS = (LEGACY_SUBJECT, STOREFRONT_SUBJECT)  # legacy API_KEY only


class StorefrontTokenThrottle(CacheTolerantThrottle, AnonRateThrottle):
    """Self-contained rate limit for the public token endpoint (no
    DEFAULT_THROTTLE_RATES setting required). Refuses, never 500s, with the
    cache down (F-086): every storefront visitor's token fetch goes through it."""

    rate = "30/minute"


class TokenObtainView(APIView):
    """Issue a short-lived JWT for Bearer authentication.

    The caller must present a valid Api-Key in the Authorization header.
    On success, returns a JWT signed with the same secret that
    BearerTokenAuthentication validates.
    """

    authentication_classes = []  # handled manually — we check Api-Key directly
    permission_classes = [AllowAny]
    throttle_classes = [TokenObtainThrottle]

    def post(self, request, *args, **kwargs):
        # Manual Api-Key check (bypasses DRF auth so we don't loop: the
        # endpoint that *issues* tokens shouldn't require a token). Same
        # resolver as APIKeyAuthentication/APIKeyMiddleware (constant-time,
        # legacy X-API-Key header accepted), minus the path binding: a scoped
        # key comes here to mint its scoped bearer.
        raw = extract_api_key(request)
        resolved = None
        if raw is not None:
            try:
                resolved = resolve_api_key(raw, request, enforce_path=False)
            except ApiKeyRejected:
                resolved = None
        if resolved is None:
            return Response(
                {"error": "Valid Api-Key required to obtain a token"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        key = resolved.key
        subject = LEGACY_SUBJECT if key is None else resolved.user.username
        scope = FULL_API_SCOPE if key is None else key.scope
        scopes = get_token_scopes()
        body = getattr(request, "data", {}) or {}
        if not isinstance(body, dict):
            body = {}
        requested_sub = body.get("subject")
        if key is None:
            if isinstance(requested_sub, str) and requested_sub in VALID_SUBJECTS:
                subject = requested_sub
        elif requested_sub is not None and requested_sub != subject:
            return Response(
                {"error": f"This key can only mint tokens for {subject!r}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Unknown scopes are rejected, never silently upgraded to full scope.
        requested_scope = body.get("scope")
        if requested_scope is not None:
            if not isinstance(requested_scope, str) or requested_scope not in scopes:
                return Response(
                    {
                        "error": f"Unknown scope {requested_scope!r}.",
                        "next_action": "Request one of: " + ", ".join(sorted(scopes)) + ".",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if subject == STOREFRONT_SUBJECT and requested_scope != FULL_API_SCOPE:
                return Response(
                    {"error": f"Scope {requested_scope!r} is not valid for the storefront subject."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if key is not None and key.scope != FULL_API_SCOPE and requested_scope != key.scope:
                return Response(
                    {"error": f"This key is limited to scope {key.scope!r}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            scope = requested_scope
        if scope not in scopes:  # a key whose plugin scope was uninstalled
            return Response(
                {"error": "Valid Api-Key required to obtain a token"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        max_lifetime_s = scopes[scope].max_lifetime_s
        lifetime_s = min(3600, max_lifetime_s)
        requested = body.get("lifetime_s")
        if isinstance(requested, (int, float)) and not isinstance(requested, bool):
            lifetime_s = max(60, min(int(requested), max_lifetime_s))
        if key is not None and key.expires_at is not None:
            remaining = int(key.expires_at.timestamp()) - int(time.time())
            lifetime_s = min(lifetime_s, remaining)
            if lifetime_s < 1:
                return Response(
                    {"error": "Valid Api-Key required to obtain a token"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

        try:
            token = issue_api_jwt(lifetime_s=lifetime_s, sub=subject, scope=scope, key=key)
        except ValueError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            "token": token,
            "subject": subject,
            "scope": scope,
            "expires_in_s": lifetime_s,
            "expires_at": int(time.time()) + lifetime_s,
        })


class StorefrontTokenView(APIView):
    """Public issuance of storefront-scoped JWTs (F-037).

    The Qwik storefront is a public, unauthenticated client: there is no
    secret it can present that a visitor's browser doesn't also have. What
    matters is that the token it receives is the least-privilege storefront
    identity (catalog read + guest order creation), never the api-service
    superuser — so this endpoint issues only sub="storefront" tokens and is
    rate-limited per IP. See core/storefront.py.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [StorefrontTokenThrottle]

    def post(self, request, *args, **kwargs):
        # GOLDEN_STOREFRONT_TOKENS turns public issuance on/off. Unset, it
        # follows API_KEY as it always did (signing no longer needs API_KEY,
        # but a deployment that never configured API access keeps this
        # endpoint closed); set it True before retiring the legacy key.
        enabled = getattr(settings, "GOLDEN_STOREFRONT_TOKENS", None)
        if not (bool(get_api_key()) if enabled is None else enabled):
            return Response(
                {"error": "Storefront tokens are not enabled on this server"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        lifetime_s = 3600
        token = issue_api_jwt(lifetime_s=lifetime_s, sub=STOREFRONT_SUBJECT)
        return Response({
            "token": token,
            "subject": "storefront",
            "expires_in_s": lifetime_s,
            "expires_at": int(time.time()) + lifetime_s,
        })
