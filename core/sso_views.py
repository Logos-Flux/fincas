"""Staff SSO handoff to the storefront.

The storefront's /admin/* dashboards are locked behind a staff-session cookie
signed with STAFF_SESSION_SECRET. The storefront has no user store of its
own — Django is the source of truth for staff identity. This view bridges the
two:

  - A logged-in Django STAFF user hitting GET /erp/sso/storefront/ gets a
    SHORT-LIVED (~90s) handoff token, minted in the exact token format the
    storefront's auth-gate verifies, and is 302'd to the storefront callback
    (STAFF_SSO_CALLBACK_URL).
  - The storefront verifies the handoff with the same shared secret and issues
    its own longer-lived HttpOnly session cookie.

Security properties:
  - login_required → unauthenticated users are bounced to /erp/login/, so this
    URL doubles as the storefront's "log in" entry point.
  - Staff check (is_active AND (is_staff OR is_superuser)) → non-staff get 403.
  - The redirect target is a FIXED, server-configured URL — never derived from
    a request parameter — so there is no open-redirect surface.
  - Fail-closed: if STAFF_SESSION_SECRET is unset, no token is minted (403).
"""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.views.decorators.http import require_GET

HANDOFF_TTL_SECONDS = 90


def _b64url(raw: bytes) -> str:
    """Unpadded base64url — matches Node's `Buffer.toString('base64url')`."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mint_handoff_token(secret: str, username: str, email: str | None = None) -> str:
    """Mint a `<payloadB64url>.<sigB64url>` token the storefront auth-gate accepts."""
    payload = {
        "staff": True,
        "exp": int(time.time()) + HANDOFF_TTL_SECONDS,
        "sub": username,
    }
    if email is not None:
        payload["email"] = email
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url(sig)}"


@require_GET
@login_required(login_url="/erp/login/")
def storefront_staff_sso(request):
    """Issue a staff-session handoff and redirect to the storefront callback."""
    user = request.user
    if not (user.is_active and (user.is_staff or user.is_superuser)):
        return HttpResponseForbidden("Staff access required.")

    secret = getattr(settings, "STAFF_SESSION_SECRET", "") or ""
    if not secret:
        return HttpResponseForbidden("Staff SSO is not configured.")

    callback = getattr(settings, "STAFF_SSO_CALLBACK_URL", "") or ""
    if not callback:
        return HttpResponseForbidden("Staff SSO callback URL is not configured.")

    token = _mint_handoff_token(secret, user.get_username(), email=user.email)
    return HttpResponseRedirect(f"{callback}?{urlencode({'token': token})}")
