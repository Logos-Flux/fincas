"""Encryption at rest for integration credentials (docs/crm/phase6-adapters.md 2.7).

A ``MultiFernet`` over ``settings.GOLDEN_SECRET_KEYS``: a list of Fernet keys
from the environment (``GOLDEN_SECRET_KEYS``, comma-separated), **newest
first**. :func:`encrypt` always uses the first key; :func:`decrypt` accepts a
token made with any key in the list. Rotating:

1. put the new key first: ``GOLDEN_SECRET_KEYS=<new>,<old>``, restart;
2. ``manage.py rotate_secrets`` re-encrypts every stored credential under
   the new key;
3. drop the old key from the list. A token it made no longer decrypts.

Unlike ``llmhub/crypto.py`` there is **no fallback derived from
``SECRET_KEY``**. Encrypting with no key configured raises
``ImproperlyConfigured``; system check ``core.E040`` fails ``manage.py
check`` when stored credentials were written under a key that is no longer
configured. A provider token that silently becomes undecryptable on a
``SECRET_KEY`` rotation would be a broken integration with no error.

Threat model, stated as llmhub states it: this protects a database dump, a
replica or a backup. It does not protect against anyone who can read the
process environment, because the keys live there.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Nothing here logs, and no error message carries a key, a token or a
plaintext.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "InvalidToken",
    "Revealed",
    "configured_key_ids",
    "decrypt",
    "encrypt",
    "key_id",
    "primary_key_id",
    "rotate",
]

SETTING = "GOLDEN_SECRET_KEYS"
DOCS = "See docs/crm/adapters.md#keys."


def _raw_keys() -> list[str]:
    value = getattr(settings, SETTING, None) or []
    if isinstance(value, str):
        value = value.split(",")
    return [str(k).strip() for k in value if str(k).strip()]


def _fernets() -> list[tuple[str, Fernet]]:
    out = []
    for index, raw in enumerate(_raw_keys()):
        try:
            fernet = Fernet(raw.encode())
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                f"{SETTING}[{index}] is not a valid Fernet key (urlsafe base64, 32 bytes) — "
                "cause: a malformed or truncated value in the environment. Fix: generate one "
                f"with Fernet.generate_key(). {DOCS}"
            ) from exc
        out.append((key_id(raw), fernet))
    return out


def key_id(raw_key: str) -> str:
    """A 16-character fingerprint of a key: which key encrypted a row, never the key."""
    return hashlib.sha256(b"golden-secret-key-id:" + raw_key.strip().encode()).hexdigest()[:16]


def configured_key_ids() -> list[str]:
    """Fingerprints of the configured keys, newest first (validates them)."""
    return [kid for kid, _ in _fernets()]


def primary_key_id() -> str:
    fernets = _fernets()
    if not fernets:
        raise _no_key()
    return fernets[0][0]


def _no_key() -> ImproperlyConfigured:
    return ImproperlyConfigured(
        f"No encryption key configured — cause: {SETTING} is empty, and integration "
        "credentials are never stored unencrypted or under a key derived from SECRET_KEY. "
        f"Fix: set {SETTING} in the environment (newest key first). {DOCS}"
    )


def _multi() -> MultiFernet:
    fernets = _fernets()
    if not fernets:
        raise _no_key()
    return MultiFernet([f for _, f in fernets])


def encrypt(plaintext: str) -> tuple[str, str]:
    """``(ciphertext, key_id)`` under the newest key. Raises ``ImproperlyConfigured`` with no key."""
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("A credential must be a non-empty string.")
    fernets = _fernets()
    if not fernets:
        raise _no_key()
    kid, fernet = fernets[0]
    return fernet.encrypt(plaintext.encode()).decode(), kid


def decrypt(ciphertext: str) -> str:
    """The plaintext. Raises ``InvalidToken`` when no configured key made it."""
    if not ciphertext:
        raise InvalidToken
    return _multi().decrypt(ciphertext.encode()).decode()


def rotate(ciphertext: str) -> tuple[str, str]:
    """Re-encrypt under the newest key: ``(ciphertext, key_id)``. ``InvalidToken`` if unreadable."""
    multi = _multi()
    return multi.rotate(ciphertext.encode()).decode(), primary_key_id()


class Revealed:
    """A decrypted secret that will not print itself.

    ``str()``, ``repr()`` and f-strings give ``<redacted:kind>``, so a secret
    passed by mistake into a log line, an exception message or an error
    tracker's frame locals shows nothing. Call :meth:`get` at the one place
    the value is needed (an auth header).
    """

    __slots__ = ("_value", "kind")

    def __init__(self, value: str, kind: str):
        self._value = value
        self.kind = kind

    def get(self) -> str:
        return self._value

    def __str__(self) -> str:
        return f"<redacted:{self.kind}>"

    __repr__ = __str__

    def __format__(self, spec) -> str:
        return str(self)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other) -> bool:
        return isinstance(other, Revealed) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __reduce__(self):  # never pickled into a cache or a queue
        raise TypeError("A revealed secret cannot be pickled.")
