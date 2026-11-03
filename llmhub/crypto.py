"""Encryption for the one secret llmhub stores in the database.

The stored credential is Fernet-encrypted. The Fernet key comes from
``settings.LLMHUB_FERNET_KEY`` (a urlsafe-base64 32-byte key, env-provided)
so that rotating Django's ``SECRET_KEY`` does not brick the stored token.

If ``LLMHUB_FERNET_KEY`` is unset the key is *derived* from ``SECRET_KEY``
with HKDF-SHA256 and a fixed context string. That keeps a fresh install
working, but in that mode a ``SECRET_KEY`` rotation makes the stored value
undecryptable — system check ``llmhub.W001`` says so at every boot, and the
settings page treats the undecryptable value as unset rather than crashing
(see ``conf.resolve``). Once a dedicated key is set, the derived key still
*decrypts* (never encrypts), so adding ``LLMHUB_FERNET_KEY`` to an install
does not lose the token it already stored; the next save re-encrypts it.

Threat model, stated plainly: this protects against read-only access to the
database (a dump, a replica, a backup). It does not protect against anyone
who can read the process environment, because the key lives there.

Generate a dedicated key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

HKDF_INFO = b"llmhub-fernet-v1"

__all__ = ["InvalidToken", "decrypt", "encrypt", "key_is_derived"]


def key_is_derived() -> bool:
    """True when no dedicated LLMHUB_FERNET_KEY is configured."""
    return not (getattr(settings, "LLMHUB_FERNET_KEY", "") or "").strip()


def _derived_fernet() -> Fernet:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(
        settings.SECRET_KEY.encode()
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def _fernet() -> Fernet | MultiFernet:
    dedicated = (getattr(settings, "LLMHUB_FERNET_KEY", "") or "").strip()
    if not dedicated:
        return _derived_fernet()
    try:
        primary = Fernet(dedicated.encode())
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "LLMHUB_FERNET_KEY is not a valid Fernet key (urlsafe base64, 32 bytes). "
            "Generate one with Fernet.generate_key()."
        ) from exc
    # The first key encrypts; the derived one only reads a token stored before
    # the dedicated key was set.
    return MultiFernet([primary, _derived_fernet()])


def encrypt(plaintext: str) -> str:
    """Encrypt a credential for storage. Empty stays empty."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def rotated(token: str) -> str | None:
    """``token`` re-encrypted under the dedicated key, or None when it already
    is (or no dedicated key is set, or it does not decrypt at all).

    A token stored before ``LLMHUB_FERNET_KEY`` was set is still under the
    SECRET_KEY-derived key; reading it is the moment to move it, so that a
    later ``SECRET_KEY`` rotation cannot void it (``conf.resolve_endpoint``
    stores the result).
    """
    if not token or key_is_derived():
        return None
    fernet = _fernet()
    try:
        fernet._fernets[0].decrypt(token.encode())
        return None  # already under the dedicated key
    except InvalidToken:
        pass
    try:
        return fernet.rotate(token.encode()).decode()
    except InvalidToken:
        return None


def decrypt(token: str) -> str:
    """Decrypt a stored credential. Raises ``InvalidToken`` when it cannot.

    Callers that serve requests must not let ``InvalidToken`` escape — an
    undecryptable key is a designed state (treated as unset), not a 500.
    """
    if not token:
        return ""
    return _fernet().decrypt(token.encode()).decode()
