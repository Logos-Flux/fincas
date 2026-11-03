"""Pub/Sub push verification for Gmail notifications (docs/crm/phase6-adapters.md 3.3).

Gmail's ``users.watch`` publishes "mailbox X changed, history id N" to a
Pub/Sub topic; a push subscription posts it to golden's webhook endpoint
with an OIDC token Google signs (RS256) in ``Authorization: Bearer``.

The check, all of it required:

- the token's ``kid`` names a key in Google's published JWKS, and the RS256
  signature verifies with it;
- ``iss`` is Google, ``exp`` is in the future and ``iat`` is not (60 s skew);
- ``aud`` equals the account's ``webhook_secret`` credential — the
  deployment sets the subscription's audience to that random value, so a
  token minted for any other audience (another tenant's, another service's)
  is refused; compared in constant time;
- when ``settings["pubsub_service_account"]`` is set, ``email`` equals it
  and ``email_verified`` is true.

The body carries a mailbox address and a history id, no mail. Its only
effect is a poll of that mailbox's grant, so a forged body could at most
cause a poll.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from .values import GOOGLE_ISSUERS, TOKEN_SKEW_S


class TokenRefused(ValueError):
    """The push token failed a check; ``reason`` is a code for the log (never the token)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _b64(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError):
        raise TokenRefused("malformed_token") from None


def _int(segment: str) -> int:
    return int.from_bytes(_b64(segment), "big")


def bearer(headers) -> str:
    value = headers.get("authorization", "") if headers else ""
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def unverified_header(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenRefused("malformed_token")
    try:
        head = json.loads(_b64(parts[0]))
    except ValueError:
        raise TokenRefused("malformed_token") from None
    if not isinstance(head, dict) or head.get("alg") != "RS256" or not isinstance(head.get("kid"), str):
        raise TokenRefused("unsupported_token")
    return head


def verify(token: str, jwks: dict, *, audience: str, now: datetime, service_account: str = "") -> dict:
    """The verified claims of ``token``; :class:`TokenRefused` on any failed check."""
    head = unverified_header(token)
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    jwk = next((k for k in keys or [] if isinstance(k, dict) and k.get("kid") == head["kid"]), None)
    if jwk is None or jwk.get("kty") != "RSA" or not jwk.get("n") or not jwk.get("e"):
        raise TokenRefused("unknown_key")
    head_b64, claims_b64, sig_b64 = token.split(".")
    public_key = RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
    try:
        public_key.verify(_b64(sig_b64), f"{head_b64}.{claims_b64}".encode("ascii"),
                          padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        raise TokenRefused("bad_signature") from None
    try:
        claims = json.loads(_b64(claims_b64))
    except ValueError:
        raise TokenRefused("malformed_token") from None
    if not isinstance(claims, dict):
        raise TokenRefused("malformed_token")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise TokenRefused("wrong_issuer")
    aud = claims.get("aud")
    if not isinstance(aud, str) or not audience or not hmac.compare_digest(aud.encode(), audience.encode()):
        raise TokenRefused("wrong_audience")
    stamp = now.timestamp()
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp + TOKEN_SKEW_S < stamp:
        raise TokenRefused("expired")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool) or iat - TOKEN_SKEW_S > stamp:
        raise TokenRefused("not_yet_valid")
    if service_account:
        if claims.get("email") != service_account or claims.get("email_verified") is not True:
            raise TokenRefused("wrong_service_account")
    return claims


def max_age(cache_control: str, default: int) -> int:
    """``max-age`` of a ``Cache-Control`` header (Google's JWKS sets one), else ``default``; at most a day."""
    for part in (cache_control or "").split(","):
        name, _, value = part.strip().partition("=")
        if name.lower() == "max-age" and value.strip().isdigit():
            return min(int(value), 86400)
    return default


def notification(payload: dict) -> tuple[str, str, str] | None:
    """``(message id, mailbox address, history id)`` from a push body, or None when it is not one."""
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("data"), str):
        return None
    try:
        data = json.loads(_b64(message["data"]))
    except (ValueError, TokenRefused):
        return None
    if not isinstance(data, dict):
        return None
    address, history = data.get("emailAddress"), data.get("historyId")
    if not isinstance(address, str) or "@" not in address:
        return None
    if isinstance(history, int) and not isinstance(history, bool):
        history = str(history)
    if not isinstance(history, str) or not history.isdigit():
        return None
    mid = message.get("messageId") or message.get("message_id") or ""
    return (mid if isinstance(mid, str) else ""), address.strip(), history
