"""The Google Ads offline-conversion adapter (docs/crm/phase6-adapters.md 3.5).

Outbound: one outbox consumer, ``google_ads.conversion``, hears
``engagement_won`` (an ``upload`` row, key ``conv:<change id>``) and
``engagement_reopened`` after an upload (a ``retract`` row, key
``retract:<change id>``). Rows batch per account and event; one request
carries up to ``values.BATCH_LIMIT`` rows with partial failure on, and each
row gets its own outcome: done, retried (a transient per-row code), dead (a
refused conversion) or cancelled with the reason it uploads nothing
(``no_click``, ``click_too_old``, ``pipeline_unmapped``, ``needs_review``,
``not_won``, ``erased``).

Idempotency (rule 4): Google dedupes click conversions by ``orderId`` (the
engagement number). Golden also records each upload as an ``ExternalRef``
(``google_ads`` / ``conversion`` / order id) when the response confirms it,
so a replayed row finds the ref and sends nothing.

Inbound: recorded visits (``{schema_version, provider, payload: {visits}}``)
go through ``ingest`` to ``crm.touchpoints.record_visit``, the same path as
``POST /api/v1/crm/touchpoints/``; the add-on registers the ``attribution``
token scope bound to that path for the site or tag manager that posts them.

Erasure: uploaded conversions carry a click id and an engagement number and
no personal data, so ``erasure_refs`` returns nothing; ``purge_parties`` is
the hook ``erase_party`` calls (step 8) to drop this consumer's pending rows
about erased parties, after its own purge by ``party_id``, and
the handler refuses to upload for an erased party.
"""

from __future__ import annotations

import json
import logging

from django.db import transaction
from django.utils.crypto import salted_hmac

from core.plugins.crm_adapter import (
    AdapterError,
    BaseCrmAdapter,
    Capability,
    CredentialsRejected,
    IngestOutcome,
    RateLimit,
    RateLimited,
    Throttled,
    Transient,
    account_for,
)

from . import conversions, mapping, values

logger = logging.getLogger("golden_erp.integrations")

ADAPTER_VERSION = "1.0.0"


class GoogleAdsAdapter(BaseCrmAdapter):
    provider_id = values.PROVIDER_ID
    schema_version = values.ENVELOPE_VERSION
    capabilities = frozenset({Capability.PUSH_CONVERSIONS})
    # A token request and an upload per batch; Google's quota is per developer
    # token and far above this (confirm before raising it, docs/crm/adapters.md).
    rate_limit = RateLimit(requests=5, per_seconds=1)
    docs_url = values.DOCS
    settings_keys = values.SETTINGS_KEYS
    credential_kinds = values.CREDENTIAL_KINDS
    push_dedupe = "orderId: Google dedupes click conversions by order id (the engagement number)"
    base_url = values.BASE_URL
    oauth_token_url = values.TOKEN_URL

    # -- configuration --------------------------------------------------------

    def validate_settings(self, settings: dict) -> list[str]:
        errors = super().validate_settings(settings)
        if isinstance(settings, dict):
            errors += mapping.validate_settings(settings)
        return errors

    # -- HTTP -------------------------------------------------------------------

    def _access_token(self) -> str:
        """An access token from the account's offline refresh token (one call per batch)."""
        refresh = self.secret("oauth_refresh").get()
        response, payload = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        if response.status == 400:
            raise CredentialsRejected(self.provider_id, 3600)
        return self._token_set(payload).access_token

    def _headers(self) -> dict:
        settings = self.account.settings or {}
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "developer-token": self.secret("api_key").get(),
            "Content-Type": "application/json",
        }
        login = str(settings.get("login_customer_id", "")).replace("-", "")
        if login:
            headers["login-customer-id"] = login
        return headers

    def send(self, event: str, rows: list[dict]) -> dict[int, str]:
        """Upload ``rows`` in one request; ``{row index: error code}`` of the rows Google refused."""
        response = self.http("POST", mapping.request_path(event, self.account.settings or {}),
                             headers=self._headers(), json=mapping.request_body(event, rows))
        try:
            payload = response.json() or {}
        except ValueError:
            payload = {}
        return mapping.failed_rows(payload if isinstance(payload, dict) else {})

    # -- the batch ------------------------------------------------------------------

    def handle_batch(self, events: list) -> list:
        """One outcome per event (same order). See the module docstring."""
        from core import outbox

        settings = self.account.settings or {}
        plans = [
            conversions.plan_upload(e, settings) if e.event == values.UPLOAD
            else conversions.plan_retraction(e, settings)
            for e in events
        ]
        outcomes: list = [None] * len(events)
        sendable, twins = [], {}
        for i, plan in enumerate(plans):
            if plan.row is not None:
                # Two rows for one order (won, reopened before the upload ran,
                # won again) would be a duplicate at Google: send it once.
                first = twins.setdefault((events[i].event, plan.row["orderId"]), i)
                if first == i:
                    sendable.append(i)
            elif plan.skip in ("uploaded", "retracted"):
                outcomes[i] = outbox.Done()  # a replay: Google already holds it
            else:
                outcomes[i] = outbox.Cancel(plan.skip)
        # Rows in order id order: the outbox hands rows over in claim order
        # (uuid pks), and the request must be byte-identical across runs.
        sendable.sort(key=lambda i: conversions.row_order(plans[i].row, events[i]))
        for chunk in conversions.chunks(sendable):
            event = events[chunk[0]].event
            failed = self.send(event, [plans[i].row for i in chunk])
            for position, i in enumerate(chunk):
                code = failed.get(position) or failed.get(-1)
                if code is None or code in values.ALREADY_DONE_ERRORS:
                    with transaction.atomic():
                        if event == values.UPLOAD:
                            conversions.record_upload(events[i], plans[i])
                        else:
                            conversions.record_retraction(events[i])
                    outcomes[i] = outbox.Done()
                elif code in values.RETRYABLE_ERRORS or code == "PARTIAL_FAILURE_UNLOCATED":
                    outcomes[i] = outbox.Retry(error=code)
                else:
                    outcomes[i] = outbox.Dead(code)
        for i, outcome in enumerate(outcomes):
            if outcome is None:  # a twin: it shares its first row's outcome
                first = twins[(events[i].event, plans[i].row["orderId"])]
                outcomes[i] = outcomes[first]
        return outcomes

    # -- outbound wiring -------------------------------------------------------------

    @classmethod
    def outbox_consumers(cls) -> list:
        from core import outbox

        return [*super().outbox_consumers(), outbox.OutboxConsumer(
            name=values.CONSUMER, signals=("engagement_won", "engagement_reopened"),
            build=_build, handle=_handle_one, handle_batch=_handle_batch,
            batch_key=lambda event: (str(event.company_id), event.event), batch_size=values.BATCH_LIMIT,
            lease_seconds=300, provider=cls.provider_id, enabled=_any_enabled_account,
        )]

    # -- inbound: recorded visits -----------------------------------------------------

    def ingest(self, envelope: dict) -> list[IngestOutcome]:
        from django.core.exceptions import ValidationError

        from crm.touchpoints import record_visit

        payload = self.read_envelope(envelope)
        visits = payload.get("visits") if isinstance(payload, dict) else None
        if not isinstance(visits, list):
            raise AdapterError(f"{self.provider_id}: a visit envelope carries payload.visits — cause: "
                               f"another shape was given. Fix: send {{\"visits\": [...]}}. {values.DOCS}")
        outcomes = []
        for visit in visits:
            visit = dict(visit) if isinstance(visit, dict) else {}
            # Rule 3: a replayed envelope writes nothing. A visit without the
            # tag manager's event id is keyed by its own canonical content.
            # Keyed (HMAC): the content includes the email, which must not be
            # recoverable from a party-less row by guessing. The salt keeps
            # the pre-#120 module path on purpose: changing it would re-key
            # every stored event id and let a replay through.
            visit.setdefault("event_id", "h:" + salted_hmac(
                "addons.google_ads.visit", json.dumps(visit, sort_keys=True, default=str),
                algorithm="sha256").hexdigest()[:40])
            try:
                touch, created = record_visit(self.account.company, visit, source=f"plugin:{self.provider_id}")
            except ValidationError:
                outcomes.append(IngestOutcome(changed=False, skip=True, reason="invalid_visit"))
                continue
            if touch is None:
                outcomes.append(IngestOutcome(changed=False, skip=True, reason="erased"))
            else:
                outcomes.append(IngestOutcome(changed=bool(created), created=bool(created)))
        return outcomes

    # -- erasure (3.5: the collector returns nothing) ---------------------------------

    def erasure_refs(self, footprint) -> list:
        return []

    @classmethod
    def purge_parties(cls, company_id, party_ids) -> int:
        """Delete this consumer's pending or dead rows about ``party_ids``.

        ``erase_party`` calls it for every installed adapter (step 8,
        ``core.integrations.erasure.purge``), after its own outbox purge."""
        from core.models import OutboxEvent

        deleted, _ = OutboxEvent.objects.filter(
            company_id=company_id, consumer=values.CONSUMER, party_id__in=list(party_ids),
            state__in=("pending", "dead"),
        ).delete()
        return deleted


# -- consumer callables (module level: the outbox registry pickles nothing, but
# keeps them stable across autoreload) --------------------------------------------


def _any_enabled_account() -> bool:
    from core.models import IntegrationAccount

    return IntegrationAccount.objects.filter(provider=values.PROVIDER_ID, enabled=True).exists()


def _build(signal_name, sender, obj, kwargs) -> list:
    from core import outbox

    engagement, change = kwargs.get("engagement"), kwargs.get("change")
    if engagement is None or change is None:
        return []
    account = account_for(GoogleAdsAdapter, engagement.company_id)
    if account is None or not account.enabled:
        return []
    common = dict(company_id=engagement.company_id, object_type="crm.engagement", object_id=engagement.pk,
                  occurred_at=change.changed_at)
    if signal_name == "engagement_won":
        # party_id: erasure deletes a pending upload about the person (4.2).
        return [outbox.EventSpec(idempotency_key=f"conv:{change.pk}", event=values.UPLOAD,
                                 payload={"change": str(change.pk)}, party_id=engagement.party_id, **common)]
    ref = conversions.conversion_ref(engagement)
    if ref is None or ref.metadata.get("retracted_at"):
        return []  # nothing at Google to retract
    # No party_id: a retraction names nobody and must still run after an erasure.
    return [outbox.EventSpec(idempotency_key=f"retract:{change.pk}", event=values.RETRACT,
                             payload={"change": str(change.pk), "at": change.changed_at.isoformat()},
                             **common)]


def _with_adapter(events, func):
    """``account_handler``'s rules for a batch: one outcome per event."""
    from core import outbox
    from core.integrations import ratelimit
    from core.integrations.transport import get_transport

    account = account_for(GoogleAdsAdapter, events[0].company_id)
    if account is None or not account.enabled:
        return [outbox.Cancel("account_disabled")] * len(events)
    wait = ratelimit.paused_for(account)
    if wait > 0:
        return [outbox.Retry(after=wait, error="account_paused")] * len(events)
    adapter = GoogleAdsAdapter(account, get_transport(account))
    try:
        outcomes = func(adapter, events)
    except (RateLimited, Throttled, CredentialsRejected) as exc:
        return [outbox.Retry(after=exc.after, error=type(exc).__name__.lower())] * len(events)
    except Transient as exc:
        return [outbox.Retry(error=exc.code[:64])] * len(events)
    except AdapterError as exc:
        logger.warning("[integrations] google_ads conversion batch dead: %s", adapter.redact(str(exc)))
        return [outbox.Dead(getattr(exc, "code", "") or type(exc).__name__)] * len(events)
    type(account)._base_manager.filter(pk=account.pk).update(last_success_at=_now())
    return outcomes


def _now():
    from django.utils import timezone

    return timezone.now()


def _handle_batch(events):
    return _with_adapter(list(events), lambda adapter, evs: adapter.handle_batch(evs))


def _handle_one(event):
    return _handle_batch([event])[0]
