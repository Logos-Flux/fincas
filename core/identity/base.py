"""Provider backend base and shared JWT machinery (identity steps 4 and 6).

Split out of ``core.identity.providers`` so a backend module (``oidc``) can
subclass :class:`Provider` without importing the registry, which builds
itself at import and may need that very backend. ``providers`` re-exports
every name here.
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

import jwt
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from .providers import ProviderConfig

DEFAULT_JWT_LEEWAY_S = 30

class TokenRejected(Exception):
    """A bearer token failed verification. ``str()`` is safe to show the client."""


class InviteFailed(Exception):
    """The provider refused or failed an invite. Carries an HTTP status for the view."""

    def __init__(self, message: str, status: int = 502, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


# ── backends ────────────────────────────────────────────────────────────────


class Provider:
    """Base class for a provider backend.

    Subclasses declare which entry points they can serve and which ``options``
    keys they accept. An instance is bound to one :class:`ProviderConfig`.
    """

    entry_points: tuple[str, ...] = ()
    #: Accepted option keys. A key ending in ``_setting`` / ``_env`` names a
    #: Django setting / environment variable holding the value; see ``option``.
    option_names: tuple[str, ...] = ()
    #: Options whose value is a secret: they may only be given indirectly.
    secret_options: tuple[str, ...] = ()
    #: Bearer-token providers are dispatched by the portal authentication class.
    bearer = False
    #: True when :meth:`invite` is implemented.
    can_invite = False

    def __init__(self, config: ProviderConfig):
        self.config = config

    # Link-time checks against the provider's own records. The resolver calls
    # them only when creating or linking an account, never per request, and
    # treats ``None`` ("cannot tell") as a refusal.
    @classmethod
    def can_confirm(cls, options: dict) -> bool:
        """Whether these options let the backend ask the provider about a subject.

        ``self_register`` and ``link_by_verified_email`` require it: the
        registry refuses a config that would fall back to a token claim.
        """
        return False

    def confirms_email(self, subject: str, email: str):
        return None

    def subject_status(self, subject: str):
        """``"gone"``, ``"unconfirmed"`` (never confirmed any email), ``"live"`` or None."""
        return None

    @classmethod
    def validate_options(cls, options: dict, where: str) -> None:
        allowed = set()
        for name in cls.option_names:
            allowed |= {name, f"{name}_setting", f"{name}_env"}
        for secret in cls.secret_options:
            allowed.discard(secret)
        for key, value in options.items():
            if key not in allowed:
                hint = (
                    f" ({key} is a secret: name it with {key}_setting or {key}_env)"
                    if key in cls.secret_options else ""
                )
                raise ImproperlyConfigured(f"{where} has unknown option {key!r}{hint}.")
            if key.endswith(("_setting", "_env")) and not (isinstance(value, str) and value):
                raise ImproperlyConfigured(f"{where} option {key!r} must name a setting/variable.")
        for name in cls.option_names:
            given = [k for k in (name, f"{name}_setting", f"{name}_env") if k in options]
            if len(given) > 1:
                raise ImproperlyConfigured(f"{where} sets {name!r} more than once ({given}).")

    @classmethod
    def validate_config(cls, config: ProviderConfig, where: str) -> None:
        """Backend checks that need the whole entry (``trusted``, entry points)."""

    def option(self, name: str, default=None):
        """Resolve an option now: literal, ``<name>_setting`` or ``<name>_env``."""
        options = self.config.options
        if f"{name}_setting" in options:
            value = getattr(settings, options[f"{name}_setting"], None)
        elif f"{name}_env" in options:
            value = os.environ.get(options[f"{name}_env"])
        else:
            value = options.get(name)
        return default if value in (None, "") else value

    def declares(self, name: str) -> bool:
        options = self.config.options
        return any(k in options for k in (name, f"{name}_setting", f"{name}_env"))


JWKS_REFRESH_MIN_INTERVAL_S = 60


class _ThrottledJWKClient(jwt.PyJWKClient):
    """``PyJWKClient`` that refetches the key set for an unknown ``kid`` at most
    once per ``JWKS_REFRESH_MIN_INTERVAL_S``.

    The stock client refetches on every miss, so a stream of tokens with random
    ``kid`` headers (no key needed to forge one) would turn into one request to
    the provider each. The key set itself is still cached for ``lifespan``.
    """

    def __init__(self, uri: str):
        super().__init__(uri, cache_keys=False, cache_jwk_set=True, lifespan=300, timeout=10)
        self._last_refresh = 0.0
        self._refresh_lock = threading.Lock()

    def get_signing_key(self, kid):
        signing_key = self.match_kid(self.get_signing_keys(), kid)
        if signing_key is None:
            with self._refresh_lock:
                now = time.monotonic()
                if now - self._last_refresh >= JWKS_REFRESH_MIN_INTERVAL_S:
                    self._last_refresh = now
                    signing_key = self.match_kid(self.get_signing_keys(refresh=True), kid)
        if signing_key is None:
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return signing_key


_jwks_clients: dict[str, jwt.PyJWKClient] = {}
_jwks_lock = threading.Lock()


def _jwks_client(url: str) -> jwt.PyJWKClient:
    with _jwks_lock:
        client = _jwks_clients.get(url)
        if client is None:
            client = _jwks_clients[url] = _ThrottledJWKClient(url)
        return client


def jwt_leeway() -> int:
    return int(getattr(settings, "GOLDEN_IDENTITY_JWT_LEEWAY_S", DEFAULT_JWT_LEEWAY_S))
