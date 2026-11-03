"""The live CRM adapter contract (docs/crm/phase6-adapters.md section 2).

One class per provider, ``BaseCrmAdapter``, built per account with an
injected :class:`~core.integrations.transport.Transport`::

    adapter = AdapterCls(account, transport)

What it declares (class attributes) and what core does with it:

- ``provider_id`` (<= 32 characters): ``Communication.provider``,
  ``ExternalRef.source``, ``IntegrationAccount.provider`` and the prefix of
  its outbox consumers (``<provider>.inbound``).
- ``capabilities``: what it implements. Every declared capability's methods
  must be overridden (``CAPABILITY_METHODS``); ``assert_crm_adapter_contract``
  checks it.
- ``rate_limit``: requests per period per account (2.9), raised per account
  by ``settings["rate_limit"]``.
- ``settings_keys`` / ``credential_kinds``: every ``IntegrationAccount.settings``
  key it reads and every ``IntegrationCredential.kind`` it needs. The
  downstream wiring guide lists exactly these.

What it inherits:

- :meth:`http`: the one call path to the provider. It spends a rate-limit
  token, adds nothing secret to anything it raises, and applies the 2.9
  policy to the answer: 429 pauses the account (or the grant) for
  ``Retry-After`` and raises :class:`Throttled`; 401 pauses the account and
  raises :class:`CredentialsRejected`; 5xx and transport errors raise
  :class:`Transient`; any other 4xx raises :class:`ProviderRejected` with
  the status only. 2xx and the statuses the caller names in ``expect`` are
  returned.
- :meth:`secret`: a decrypted credential as ``core.secrets.Revealed``. It is
  registered with the transport so a ``FixtureTransport`` records
  ``<redacted:kind>`` in its place, and with :meth:`redact` so no
  ``AdapterError`` text the adapter builds can carry it.
- OAuth helpers for ``OAUTH_USER`` adapters (2.7): :meth:`authorization_url`,
  :meth:`exchange_code`, :meth:`store_grant`, :meth:`access_token` (refreshes,
  and revokes **one** user's grant on ``invalid_grant``).
- :meth:`outbox_consumers`: ``<provider>.inbound`` for ``WEBHOOK`` (calls
  :meth:`ingest` on the stored envelope inside ``core.outbox.origin``) and
  ``<provider>.revoke`` for ``OAUTH_USER``. Subclasses add their push
  consumers with :func:`account_handler`.

Errors are ``AdapterError`` (problem + cause + fix + docs link), the payroll
convention reused as ``core.plugins.crm_export`` reuses it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets as _stdlib_secrets
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode

from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from core.integrations import ratelimit
from core.integrations.ratelimit import RateLimit
from core.integrations.transport import Response, TransportError
from core.plugins.payroll import AdapterError, require_known_version

__all__ = [
    "AdapterError",
    "BaseCrmAdapter",
    "Capability",
    "CredentialsRejected",
    "GrantRevoked",
    "IngestOutcome",
    "MissingCredential",
    "PollPage",
    "ProviderRef",
    "ProviderRejected",
    "RateLimit",
    "RateLimited",
    "Throttled",
    "Transient",
    "WebhookCheck",
    "account_handler",
    "assert_crm_adapter_contract",
    "envelope",
    "require_known_version",
]

logger = logging.getLogger("golden_erp.integrations")

PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: How long a 401 pauses an account. Saving a new credential lifts it.
CREDENTIALS_REJECTED_PAUSE = timedelta(hours=1)
#: Pause after ``invalid_grant`` on one user's refresh (the grant is revoked too).
DOCS_DEFAULT = "See docs/crm/adapters.md."


class Capability(str, Enum):
    POLL = "poll"                    # inbound by polling a cursor
    WEBHOOK = "webhook"              # inbound by provider webhook
    PUSH_TASKS = "push_tasks"        # outbound task writes
    PUSH_PROFILES = "push_profiles"  # outbound person/consent writes
    PUSH_CONVERSIONS = "push_conversions"
    ERASE = "erase"                  # provider-side deletion or scrub
    OAUTH_USER = "oauth_user"        # per-user OAuth grants


PUSH_CAPABILITIES = frozenset({Capability.PUSH_TASKS, Capability.PUSH_PROFILES, Capability.PUSH_CONVERSIONS})

#: Methods an adapter must override for each capability it declares (rule 1).
CAPABILITY_METHODS: dict[Capability, tuple[str, ...]] = {
    Capability.POLL: ("poll", "ingest"),
    Capability.WEBHOOK: ("verify_webhook", "ingest"),
    Capability.PUSH_TASKS: ("outbox_consumers",),
    Capability.PUSH_PROFILES: ("outbox_consumers",),
    Capability.PUSH_CONVERSIONS: ("outbox_consumers",),
    Capability.ERASE: ("erasure_refs", "erase"),
    Capability.OAUTH_USER: ("oauth_subject",),
}

#: Settings keys core itself reads on every account (2.4, 2.9).
CORE_SETTINGS_KEYS = frozenset({"rate_limit", "initial_since"})


# -- value types ------------------------------------------------------------


@dataclass(frozen=True)
class WebhookCheck:
    """``verify_webhook``'s answer. ``timestamp`` set when the provider signs one (replay window)."""

    ok: bool
    delivery_id: str = ""
    timestamp: datetime | None = None
    reason: str = ""  # a code for the log: bad_signature, missing_signature


@dataclass(frozen=True)
class PollPage:
    """One page of a poll: envelopes to ingest, the cursor after them, and whether more remain."""

    items: list[dict]
    cursor: dict
    more: bool = False


@dataclass(frozen=True)
class IngestOutcome:
    """What ``ingest`` did with one item. ``skip`` = a permanent refusal (the cursor may pass it)."""

    changed: bool
    created: bool = False
    skip: bool = False
    reason: str = ""
    external_id: str = ""


@dataclass(frozen=True)
class ProviderRef:
    object_type: str
    source_id: str


@dataclass
class TokenSet:
    # repr=False: an error tracker's frame locals must not print the tokens.
    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    expires_in: int | None = None
    scopes: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)


def envelope(provider: str, payload: Any, *, schema_version: int = 1) -> dict:
    """The versioned envelope every inbound item travels in (rule 2)."""
    return {"schema_version": schema_version, "provider": provider, "payload": payload}


# -- errors (all AdapterError: problem + cause + fix + docs) -----------------


class RateLimited(AdapterError):
    """The account's local token bucket is empty; ``after`` seconds until the next token."""

    def __init__(self, provider: str, after: float):
        self.after = after
        super().__init__(
            f"{provider}: request not sent — cause: the account's rate limit is spent. "
            f"Fix: nothing; it retries in {after:.1f} s. {DOCS_DEFAULT}"
        )


class Throttled(AdapterError):
    """The provider answered 429; the account (or grant) is paused for ``after`` seconds."""

    def __init__(self, provider: str, after: float):
        self.after = after
        super().__init__(
            f"{provider}: the provider is throttling this account (HTTP 429) — cause: its rate "
            f"limit for the plan. Fix: nothing; paused for {after:.0f} s. {DOCS_DEFAULT}"
        )


class CredentialsRejected(AdapterError):
    def __init__(self, provider: str, after: float):
        self.after = after
        super().__init__(
            f"{provider}: credentials rejected (HTTP 401) — cause: a revoked, expired or wrong "
            "key. Fix: enter a new credential under Connected accounts; the account is paused "
            f"until then. {DOCS_DEFAULT}"
        )


class Transient(AdapterError):
    def __init__(self, provider: str, code: str):
        self.code = code
        super().__init__(
            f"{provider}: the provider did not answer usefully ({code}) — cause: an outage or a "
            f"network error. Fix: nothing; the outbox retries with backoff. {DOCS_DEFAULT}"
        )


class ProviderRejected(AdapterError):
    def __init__(self, provider: str, status: int, code: str = ""):
        self.status = status
        self.code = code or f"http_{status}"
        super().__init__(
            f"{provider}: the provider refused the request (HTTP {status}) — cause: the request "
            "or the account's settings do not suit the provider. Fix: check the account's "
            f"settings; requeue after fixing. {DOCS_DEFAULT}"
        )


class MissingCredential(AdapterError):
    def __init__(self, provider: str, kind: str):
        self.kind = kind
        super().__init__(
            f"{provider}: no active {kind} credential — cause: none was entered, or it was "
            f"revoked. Fix: add it under Connected accounts. {DOCS_DEFAULT}"
        )


class GrantRevoked(AdapterError):
    def __init__(self, provider: str):
        super().__init__(
            f"{provider}: the user's authorization was withdrawn (invalid_grant) — cause: the "
            "user revoked access or the grant expired. Fix: the user reconnects the account. "
            f"{DOCS_DEFAULT}"
        )


class Undecryptable(AdapterError):
    def __init__(self, provider: str, kind: str):
        super().__init__(
            f"{provider}: the stored {kind} credential cannot be decrypted — cause: it was "
            "written under a key no longer in GOLDEN_SECRET_KEYS. Fix: put that key back, or "
            f"enter the credential again (system check core.E040). {DOCS_DEFAULT}"
        )


# -- the base class ---------------------------------------------------------


class BaseCrmAdapter(ABC):
    """Interface every live CRM adapter implements (2.2)."""

    provider_id: str = ""
    schema_version: int = 1
    capabilities: frozenset = frozenset()
    rate_limit: RateLimit = RateLimit(requests=1, per_seconds=1)
    docs_url: str = ""
    settings_keys: frozenset = frozenset()
    credential_kinds: frozenset = frozenset()
    #: Rule 4: how a retried create finds the record a lost response made
    #: (native idempotency key, natural-key upsert, server-side dedupe field,
    #: or a golden marker to search for). Required with any PUSH_* capability.
    push_dedupe: str = ""
    #: Base URL the adapter's relative paths resolve against.
    base_url: str = ""
    # OAUTH_USER (2.7)
    oauth_authorize_url: str = ""
    oauth_token_url: str = ""
    oauth_revoke_url: str = ""
    oauth_scopes: tuple = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.provider_id:
            raise TypeError(f"{cls.__name__} must set provider_id (e.g. 'clickup').")

    def __init__(self, account, transport):
        self.account = account
        self.transport = transport
        self._revealed: dict[str, str] = {}  # plaintext -> kind, for redaction

    # -- configuration -------------------------------------------------------

    @classmethod
    def accepted_settings_keys(cls) -> frozenset:
        return frozenset(cls.settings_keys) | CORE_SETTINGS_KEYS

    def validate_settings(self, settings: dict) -> list[str]:
        """Errors (problem + cause + fix) for ``settings``; empty when valid.

        The base refuses a non-object, unknown keys and a malformed
        ``rate_limit``; subclasses extend it with ``super()``.
        """
        if not isinstance(settings, dict):
            return ["Settings must be a JSON object — cause: another JSON value was saved. "
                    "Fix: enter {} or an object of the keys this provider reads."]
        errors = []
        unknown = sorted(set(settings) - self.accepted_settings_keys())
        if unknown:
            errors.append(
                f"Unknown setting(s) {', '.join(unknown)} — cause: {self.provider_id} does not read "
                f"them. Fix: remove them; accepted keys: {', '.join(sorted(self.accepted_settings_keys()))}."
            )
        if "rate_limit" in settings:
            raw = settings["rate_limit"]
            try:
                if not isinstance(raw, dict):
                    raise TypeError
                RateLimit(requests=raw.get("requests"), per_seconds=raw.get("per_seconds"))
            except (TypeError, ValueError):
                errors.append("rate_limit must be {\"requests\": <positive int>, \"per_seconds\": "
                              "<positive number>} — cause: a malformed value. Fix: correct or remove it.")
        if "initial_since" in settings:
            raw = settings["initial_since"]
            if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= 3650:
                errors.append("initial_since is a number of days, 1-3650 — cause: a malformed value. "
                              "Fix: correct or remove it (default 90).")
        return errors

    # -- credentials ----------------------------------------------------------

    def credential(self, kind: str, owner=None):
        """The active credential of ``kind`` (for ``owner``'s grant when given), or None."""
        from core.models import IntegrationCredential

        return IntegrationCredential.objects.filter(
            account=self.account, kind=kind, owner=owner, revoked_at__isnull=True,
        ).first()

    def secret(self, kind: str, owner=None):
        """The decrypted ``kind`` credential (a ``Revealed``); ``MissingCredential`` if none."""
        from core.secrets import InvalidToken

        cred = self.credential(kind, owner)
        if cred is None:
            raise MissingCredential(self.provider_id, kind)
        try:
            revealed = cred.reveal()
        except InvalidToken:
            raise Undecryptable(self.provider_id, kind) from None
        self._register(revealed.get(), kind)
        return revealed

    def _register(self, value: str, kind: str) -> None:
        if not value:
            return
        self._revealed[value] = kind
        hook = getattr(self.transport, "register_secret", None)
        if hook is not None:
            hook(value, kind)

    def revealed_values(self) -> list[str]:
        """Every secret this instance decrypted, longest first (for ``redact(extra=)``)."""
        return sorted(self._revealed, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for value in self.revealed_values():
            text = text.replace(value, f"<redacted:{self._revealed[value]}>")
        return text

    # -- HTTP -----------------------------------------------------------------

    def url(self, path: str) -> str:
        if path.startswith("https://") or path.startswith("http://"):
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def rate_key(self) -> str:
        """One bucket per account: per-user grants share the account's limit."""
        return f"{self.provider_id}:{self.account.pk}"

    @sensitive_variables("headers", "data", "json", "params")
    def http(self, method: str, path: str, *, headers: dict | None = None, params: dict | None = None,
             json: Any = None, data: Any = None, timeout: float = 10.0, expect: Iterable[int] = (),
             grant=None, rate_limited: bool = True, pause_on_401: bool = True) -> Response:
        """Send one request through the transport and apply the 2.9 policy (module docstring).

        ``grant`` is the per-user credential the call is made for: a 429 or a
        401 then pauses that grant instead of the whole account.
        ``pause_on_401=False`` (OAuth connect, refresh, revoke) turns a 401
        into ``ProviderRejected`` and pauses nothing: one user's failed
        connect must not stop every other user's sync.
        """
        if rate_limited:
            limit = ratelimit.effective_limit(self.rate_limit, self.account.settings)
            wait = ratelimit.take(self.rate_key(), limit)
            if wait > 0:
                raise RateLimited(self.provider_id, wait)
        try:
            response = self.transport.request(
                method, self.url(path), headers=dict(headers or {}), params=params, json=json,
                data=data, timeout=timeout,
            )
        except TransportError as exc:
            raise Transient(self.provider_id, f"transport_{exc}") from None
        if response.ok or response.status in set(expect):
            return response
        if response.status == 429:
            after = ratelimit.retry_after_seconds(response.header("retry-after"))
            after = 60.0 if after is None else after
            ratelimit.pause(grant if grant is not None else self.account, after,
                            reason="" if grant is not None else "rate_limited")
            raise Throttled(self.provider_id, after)
        if response.status == 401 and pause_on_401:
            after = CREDENTIALS_REJECTED_PAUSE.total_seconds()
            if grant is not None:
                ratelimit.pause(grant, after)
            else:
                ratelimit.pause(self.account, after, reason="credentials_rejected")
            raise CredentialsRejected(self.provider_id, after)
        if response.status >= 500:
            raise Transient(self.provider_id, f"http_{response.status}")
        raise ProviderRejected(self.provider_id, response.status)

    # -- inbound --------------------------------------------------------------

    def poll(self, cursor: dict, *, credential=None) -> PollPage:  # POLL
        raise NotImplementedError

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookCheck:  # WEBHOOK
        raise NotImplementedError

    def webhook_envelope(self, headers: Mapping[str, str], body: bytes) -> dict:
        """The envelope stored for a verified body. Default: the JSON body, versioned."""
        return envelope(self.provider_id, json.loads(body.decode("utf-8")),
                        schema_version=self.schema_version)

    def ingest(self, envelope: dict) -> list[IngestOutcome]:  # POLL / WEBHOOK
        raise NotImplementedError

    def read_envelope(self, item: dict) -> Any:
        """Rule 2: refuse a foreign or newer envelope before touching the payload."""
        if not isinstance(item, dict) or not {"schema_version", "provider", "payload"} <= set(item):
            raise AdapterError(
                f"{self.provider_id}: inbound item is not an envelope — cause: the reader was handed "
                "a bare payload. Fix: wrap it as {schema_version, provider, payload}. "
                f"{self.docs_url or DOCS_DEFAULT}"
            )
        if item["provider"] != self.provider_id:
            raise AdapterError(
                f"{self.provider_id}: envelope of provider {item['provider']!r} — cause: another "
                f"provider's item reached this adapter. Fix: route it to its own adapter. {DOCS_DEFAULT}"
            )
        require_known_version(item, self.schema_version)
        return item["payload"]

    @staticmethod
    def raw_envelope(item: dict) -> dict:
        """The three keys ``communications.ingest.record`` accepts (``request`` dropped)."""
        return {k: item[k] for k in ("schema_version", "provider", "payload")}

    # -- outbound -------------------------------------------------------------

    @classmethod
    def outbox_consumers(cls) -> list:
        """``OutboxConsumer``s this adapter owns. Subclasses extend with ``super()``."""
        from core import outbox

        consumers = []
        caps = set(cls.capabilities)
        if Capability.WEBHOOK in caps:
            consumers.append(outbox.OutboxConsumer(
                name=f"{cls.provider_id}.inbound", handle=account_handler(cls, _handle_inbound),
                provider=cls.provider_id,
            ))
        if Capability.OAUTH_USER in caps:
            consumers.append(outbox.OutboxConsumer(
                name=f"{cls.provider_id}.revoke", handle=account_handler(cls, _handle_revoke),
                provider=cls.provider_id,
            ))
        if Capability.ERASE in caps:
            # One erase consumer for every adapter (phase6-adapters.md 4.1,
            # step 8): erase_party enqueues it, it never cancels.
            from core.integrations.erasure import erase_consumer

            consumers.append(erase_consumer(cls))
        return consumers

    # -- erasure (section 4; wired into erase_party in step 8) ----------------

    def erasure_refs(self, footprint) -> list[ProviderRef]:  # ERASE
        raise NotImplementedError

    def erase(self, refs: list[ProviderRef], *, done: Callable[[ProviderRef], None] | None = None):  # ERASE
        """Scrub or delete ``refs`` at the provider, calling ``done(ref)`` after
        each one the provider has accepted (or no longer has). Runs from the
        shared ``<provider>.erase`` consumer (core.integrations.erasure), with
        no transaction open; raise the 2.9 errors to retry. Returning an
        outbox outcome other than Done is honoured."""
        raise NotImplementedError

    @classmethod
    def purge_parties(cls, company_id, party_ids) -> int:
        """Delete this adapter's own queued local work about erased ``party_ids``.

        Every installed adapter's is called inside ``erase_party``, ERASE or
        not, after the generic outbox purge (core.integrations.erasure.purge);
        override it for rows that purge cannot find (state an adapter keeps
        outside ``party_id``). Local only, never a provider call. Returns the
        number of rows removed."""
        return 0

    # -- connection test ------------------------------------------------------

    def test_connection(self) -> dict:
        """A ``core.integration_checks.result`` dict. Default: credentials present and decryptable."""
        from core.integration_checks import result

        missing = []
        for kind in sorted(self.credential_kinds):
            if kind in ("oauth_refresh", "oauth_access"):
                continue  # per-user grants, not the account's
            try:
                self.secret(kind)
            except AdapterError:
                missing.append(kind)
        if missing:
            return result(False, f"Missing or unreadable credential(s): {', '.join(missing)}.",
                          error_class="integration_credentials_missing")
        return result(True, "Credentials present (no provider call made).")

    # -- OAuth (OAUTH_USER, 2.7) ---------------------------------------------

    def oauth_client(self) -> tuple[str, Any]:
        """``(client_id, Revealed client_secret)`` from the account's ``oauth_client`` credential.

        The credential's plaintext is JSON ``{"client_id": ..., "client_secret": ...}``.
        """
        from core.secrets import Revealed

        raw = self.secret("oauth_client").get()
        try:
            data = json.loads(raw)
            client_id, client_secret = str(data["client_id"]), str(data["client_secret"])
        except (ValueError, KeyError, TypeError):
            raise AdapterError(
                f"{self.provider_id}: the OAuth client credential is not "
                '{"client_id": ..., "client_secret": ...} — cause: it was entered in another '
                f"shape. Fix: enter it again as that JSON object. {DOCS_DEFAULT}"
            ) from None
        self._register(client_secret, "oauth_client")
        return client_id, Revealed(client_secret, "oauth_client")

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        client_id, _secret = self.oauth_client()
        params = {
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
            "scope": " ".join(self.oauth_scopes), "state": state,
            "code_challenge": code_challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent",
        }
        sep = "&" if "?" in self.oauth_authorize_url else "?"
        return self.oauth_authorize_url + sep + urlencode(params)

    @sensitive_variables()
    def _token_request(self, form: dict, *, grant=None) -> tuple[Response, dict]:
        client_id, client_secret = self.oauth_client()
        body = {**form, "client_id": client_id, "client_secret": client_secret.get()}
        response = self.http("POST", self.oauth_token_url, data=body, expect=(400,), grant=grant,
                             pause_on_401=False,
                             headers={"Accept": "application/json"})
        try:
            payload = response.json() or {}
        except ValueError:
            payload = {}
        return response, payload if isinstance(payload, dict) else {}

    def _token_set(self, payload: dict) -> TokenSet:
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise AdapterError(
                f"{self.provider_id}: the token endpoint answered without an access token — cause: "
                f"an unexpected response. Fix: retry the connection. {DOCS_DEFAULT}"
            )
        self._register(access, "oauth_access")
        refresh = payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) else ""
        if refresh:
            self._register(refresh, "oauth_refresh")
        expires = payload.get("expires_in")
        scope = payload.get("scope")
        return TokenSet(
            access_token=access, refresh_token=refresh,
            expires_in=expires if isinstance(expires, int) and not isinstance(expires, bool) else None,
            scopes=scope.split() if isinstance(scope, str) else list(self.oauth_scopes), raw=payload,
        )

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        response, payload = self._token_request({
            "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        })
        if response.status == 400:
            raise ProviderRejected(self.provider_id, 400, _oauth_error_code(payload))
        return self._token_set(payload)

    def oauth_subject(self, tokens: TokenSet) -> str:  # OAUTH_USER
        """What the grant is for, shown to staff (the mailbox address). One provider call at most."""
        raise NotImplementedError

    @sensitive_variables()
    def store_grant(self, owner, tokens: TokenSet, *, subject: str):
        """Replace ``owner``'s grant with ``tokens`` (refresh + access credentials)."""
        from core.models import IntegrationCredential

        now = timezone.now()
        with transaction.atomic():
            IntegrationCredential.objects.filter(
                account=self.account, owner=owner, revoked_at__isnull=True,
                kind__in=["oauth_refresh", "oauth_access"],
            ).update(revoked_at=now)
            created = []
            for kind, value, expires in (
                ("oauth_refresh", tokens.refresh_token, None),
                ("oauth_access", tokens.access_token,
                 now + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None),
            ):
                if not value:
                    continue
                cred = IntegrationCredential(
                    account=self.account, company_id=self.account.company_id, owner=owner, kind=kind,
                    subject=subject[:255], scopes=list(tokens.scopes), expires_at=expires,
                )
                cred.set_secret(value)
                cred.save()
                created.append(cred)
        return created

    @sensitive_variables()
    def access_token(self, owner):
        """A live access token (``Revealed``) for ``owner``'s grant, refreshed when expired.

        ``invalid_grant`` revokes this owner's grant only (``revoked_at``) and
        raises :class:`GrantRevoked`; every other user's grant is untouched.
        """
        from core.models import IntegrationCredential
        from core.secrets import Revealed

        now = timezone.now()
        access = self.credential("oauth_access", owner)
        if access is not None and (access.expires_at is None or access.expires_at > now + timedelta(seconds=60)):
            return self.secret("oauth_access", owner)
        refresh_cred = self.credential("oauth_refresh", owner)
        if refresh_cred is None:
            raise GrantRevoked(self.provider_id)
        refresh = self.secret("oauth_refresh", owner)
        response, payload = self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh.get()}, grant=refresh_cred)
        if response.status == 400:
            if _oauth_error_code(payload) == "invalid_grant":
                IntegrationCredential.objects.filter(
                    account=self.account, owner=owner, revoked_at__isnull=True,
                    kind__in=["oauth_refresh", "oauth_access"],
                ).update(revoked_at=now)
                logger.warning("[integrations] %s grant revoked by provider (invalid_grant): account=%s owner=%s",
                               self.provider_id, self.account.pk, getattr(owner, "pk", None))
                raise GrantRevoked(self.provider_id)
            raise ProviderRejected(self.provider_id, 400, _oauth_error_code(payload))
        tokens = self._token_set(payload)
        self._refreshed(owner, tokens, refresh_cred, rotated=bool(tokens.refresh_token)
                        and tokens.refresh_token != refresh.get())
        return Revealed(tokens.access_token, "oauth_access")

    @sensitive_variables()
    def _refreshed(self, owner, tokens: TokenSet, refresh_cred, *, rotated: bool) -> None:
        """Store a refreshed access token; keep the grant row (its cursor and pause) unless rotated.

        The refresh row carries the grant's poll cursor (``sync_state``) and
        its pause: a provider that rotates refresh tokens gets a new row
        with both copied, so a refresh never restarts a backfill.
        """
        from core.models import IntegrationCredential

        now = timezone.now()
        with transaction.atomic():
            IntegrationCredential.objects.filter(
                account=self.account, owner=owner, kind="oauth_access", revoked_at__isnull=True,
            ).update(revoked_at=now)
            access = IntegrationCredential(
                account=self.account, company_id=self.account.company_id, owner=owner, kind="oauth_access",
                subject=refresh_cred.subject, scopes=list(tokens.scopes),
                expires_at=now + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None,
            )
            access.set_secret(tokens.access_token)
            access.save()
            if rotated:
                IntegrationCredential.objects.filter(pk=refresh_cred.pk).update(revoked_at=now)
                fresh = IntegrationCredential(
                    account=self.account, company_id=self.account.company_id, owner=owner,
                    kind="oauth_refresh", subject=refresh_cred.subject, scopes=list(tokens.scopes),
                    sync_state=refresh_cred.sync_state, paused_until=refresh_cred.paused_until,
                )
                fresh.set_secret(tokens.refresh_token)
                fresh.save()

    def revoke_grant(self, credential) -> None:
        """Best-effort revocation at the provider (RFC 7009) when ``oauth_revoke_url`` is set."""
        if not self.oauth_revoke_url:
            return
        from core.secrets import InvalidToken

        try:
            token = credential.reveal()
        except InvalidToken:
            return
        self._register(token.get(), credential.kind)
        self.http("POST", self.oauth_revoke_url, data={"token": token.get()}, expect=(400,),
                  pause_on_401=False)


def _oauth_error_code(payload: dict) -> str:
    code = payload.get("error") if isinstance(payload, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else "oauth_error"


def pkce_pair() -> tuple[str, str]:
    """``(verifier, S256 challenge)`` for an authorization-code flow."""
    verifier = _stdlib_secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


# -- running adapter code from the outbox -----------------------------------


def account_for(adapter_cls, company_id):
    from core.models import IntegrationAccount

    if company_id is None:
        return None
    return IntegrationAccount.objects.filter(company_id=company_id, provider=adapter_cls.provider_id).first()


def account_handler(adapter_cls, func: Callable[[Any, Any], Any], *,
                    inactive: Callable[[Any, Any], Any] | None = None) -> Callable[[Any], Any]:
    """An outbox ``handle`` that builds ``adapter_cls`` for the row's account and calls ``func(adapter, event)``.

    Shared rules for every adapter consumer: a missing or disabled account
    cancels the row (``Cancel("account_disabled")``: switched off, not an
    alarm; requeue after enabling if the work still matters); a
    paused account retries when the pause ends without calling the provider;
    the 2.9 errors become outcomes (``RateLimited``/``Throttled``/
    ``CredentialsRejected`` -> ``Retry(after)``, ``Transient`` -> ``Retry``,
    any other ``AdapterError`` -> ``Dead`` with its class name). The error
    stored on the row is a code, never a message.

    ``inactive(event, account_or_None)`` replaces the cancel for a missing or
    disabled account: the erase consumer retries instead, because erasure
    work must never be dropped (core.integrations.erasure).
    """
    from core import outbox
    from core.integrations.transport import get_transport

    def handle(event):
        account = account_for(adapter_cls, event.company_id)
        if account is None or not account.enabled:
            if inactive is not None:
                return inactive(event, account)
            return outbox.Cancel("account_disabled")
        wait = ratelimit.paused_for(account)
        if wait > 0:
            return outbox.Retry(after=wait, error="account_paused")
        adapter = adapter_cls(account, get_transport(account))
        try:
            outcome = func(adapter, event)
        except (RateLimited, Throttled, CredentialsRejected) as exc:
            return outbox.Retry(after=exc.after, error=_code(exc))
        except Transient as exc:
            return outbox.Retry(error=exc.code[:64])
        except AdapterError as exc:
            logger.warning("[integrations] %s %s dead: %s", adapter_cls.provider_id, event.consumer,
                           adapter.redact(str(exc)))
            return outbox.Dead(getattr(exc, "code", "") or _code(exc))
        _mark_success(account)
        return outcome if outcome is not None else outbox.Done()

    handle.__qualname__ = f"{adapter_cls.__name__}.{getattr(func, '__name__', 'handle')}"
    return handle


def _code(exc) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()


def _mark_success(account) -> None:
    type(account)._base_manager.filter(pk=account.pk).update(last_success_at=timezone.now())


def _handle_inbound(adapter, event):
    """``<provider>.inbound``: ``ingest`` the verified envelope, as a live inbound write."""
    from core import outbox

    with outbox.origin(adapter.provider_id), transaction.atomic():
        adapter.ingest(event.payload)
    return outbox.Done()


def _handle_revoke(adapter, event):
    """``<provider>.revoke``: best-effort provider revocation of a disconnected grant."""
    from core import outbox
    from core.models import IntegrationCredential

    cred = IntegrationCredential.objects.filter(
        account=adapter.account, pk=(event.payload or {}).get("credential_id"),
    ).first()
    if cred is not None:
        adapter.revoke_grant(cred)
    return outbox.Done()


# -- the contract test ---------------------------------------------------------


def _overrides(cls, name: str) -> bool:
    mine = getattr(cls, name, None)
    base = getattr(BaseCrmAdapter, name, None)
    return getattr(mine, "__func__", mine) is not getattr(base, "__func__", base)


_LABEL_READER = re.compile(r"\bcore\s*\.\s*labels\b|from\s+core\s+import\s+[^\n]*\blabels\b|\bget_label\(")


def check_adapter_class(adapter_cls) -> None:
    """Rules 1, 4, 6 and 7 of 2.2 on the class itself. Raises AssertionError naming the breach."""
    import importlib
    from pathlib import Path

    assert isinstance(adapter_cls, type) and issubclass(adapter_cls, BaseCrmAdapter), (
        f"{adapter_cls!r} is not a BaseCrmAdapter subclass")
    pid = adapter_cls.provider_id
    assert isinstance(pid, str) and PROVIDER_ID.match(pid), (
        f"provider_id {pid!r} must match [a-z][a-z0-9_]{{0,31}}")
    version = adapter_cls.schema_version
    assert isinstance(version, int) and not isinstance(version, bool) and version >= 1, (
        "schema_version must be a positive int")
    caps = set(adapter_cls.capabilities)
    assert caps <= set(Capability), f"unknown capabilities: {caps - set(Capability)}"
    for cap in caps:
        for method in CAPABILITY_METHODS[cap]:
            assert _overrides(adapter_cls, method), (
                f"{adapter_cls.__name__} declares {cap.value} but does not override {method}()")
    if caps & PUSH_CAPABILITIES:
        assert adapter_cls.push_dedupe, (
            f"{adapter_cls.__name__} pushes but names no provider-side dedupe key (push_dedupe, rule 4)")
    if Capability.OAUTH_USER in caps:
        assert adapter_cls.oauth_authorize_url and adapter_cls.oauth_token_url, (
            "an OAUTH_USER adapter sets oauth_authorize_url and oauth_token_url")
        assert {"oauth_client", "oauth_refresh"} <= set(adapter_cls.credential_kinds), (
            "an OAUTH_USER adapter needs the oauth_client and oauth_refresh credential kinds")
    if Capability.WEBHOOK in caps:
        assert "webhook_secret" in adapter_cls.credential_kinds, (
            "a WEBHOOK adapter verifies with a webhook_secret credential")
    from core.models import IntegrationCredential

    unknown = set(adapter_cls.credential_kinds) - set(IntegrationCredential.Kind.values)
    assert not unknown, f"unknown credential kinds {sorted(unknown)}"
    assert isinstance(adapter_cls.rate_limit, RateLimit), "rate_limit must be a RateLimit"
    module = importlib.import_module(adapter_cls.__module__)
    package = Path(module.__file__).parent
    for path in sorted(package.rglob("*.py")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not _LABEL_READER.search(text), (
            f"{path.name}: an adapter reads no display labels (the label reader); rule 7")


def assert_crm_adapter_contract(adapter_cls, fixture_dirs, *, seed_hook=None) -> list:
    """The test every adapter ships (5.3). Raises AssertionError naming the breach.

    For each fixture case: build the tenant, run the steps, compare the
    recorded requests and the snapshot with ``requests.expected.json`` and
    ``db.expected.json`` byte for byte, run the steps again and assert
    nothing changed (no new rows, ``changed=False``, no requests beyond the
    case's ``second_run_requests``), and that the other tenant holds nothing.
    A case with ``"expect": "refused"`` must raise ``AdapterError`` at the
    steps marked ``raises``; at least one such case is a ``newer-schema``
    envelope (rule 2). Set ``GOLDEN_FIXTURES_WRITE=1`` to (re)write the
    expected files while authoring a case; never in CI.

    Must run inside a database test (``TestCase``).
    """
    import os
    from pathlib import Path

    from core.testing.integration_fixtures import canonical_json, load_json, run_case

    check_adapter_class(adapter_cls)
    cases = [Path(d) for d in fixture_dirs]
    assert cases, "ship at least one fixture case"
    write = os.environ.get("GOLDEN_FIXTURES_WRITE") == "1"
    refused_newer = False
    results = []
    for case_dir in cases:
        case = load_json(case_dir / "case.json")
        with transaction.atomic():  # each case starts from an empty database
            result = run_case(adapter_cls, case_dir, seed_hook=seed_hook)
            transaction.set_rollback(True)
        results.append(result)
        where = f"{adapter_cls.__name__} case {case_dir.name}"
        if case.get("expect") == "refused":
            assert result.errors, f"{where}: expected a refusal, every step succeeded"
            assert len(result.second_errors) == len(result.errors), f"{where}: the refusal is not stable"
            for _index, exc in result.errors:
                assert isinstance(exc, AdapterError), f"{where}: refusals are AdapterError (rule 6)"
            if "newer" in case_dir.name:
                refused_newer = True
        for name, actual in (("requests.expected.json", result.requests), ("db.expected.json", result.db)):
            path = case_dir / name
            text = canonical_json(actual)
            if write:
                path.write_text(text, encoding="utf-8")
                continue
            assert path.exists(), f"{where}: missing {name} (author it with GOLDEN_FIXTURES_WRITE=1)"
            expected = path.read_text(encoding="utf-8")
            assert expected == text, f"{where}: {name} differs:\n--- expected\n{expected}\n--- actual\n{text}"
        assert result.second_db == result.db, f"{where}: a second run changed the database (not idempotent)"
        assert all(not o.changed for batch in result.second_outcomes for o in batch), (
            f"{where}: a second ingest reported changed=True (rule 3)")
        expected_repeat = case.get("second_run_requests", [])
        assert result.second_requests == expected_repeat, (
            f"{where}: the second run made requests {result.second_requests!r}, "
            f"expected {expected_repeat!r}")
        assert result.other_tenant_empty, f"{where}: the case wrote rows into another tenant"
    assert refused_newer, (
        f"{adapter_cls.__name__}: ship a refused 'newer-schema' case (rule 2: a newer envelope is refused)")
    return results
