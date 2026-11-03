"""``AircallAdapter``: calls in, recordings left at the provider (docs/crm/phase6-adapters.md 3.4).

- **Inbound.** Webhook (``call.ended``, ``call.commented``; the body's shared
  token checked against the account's ``webhook_secret``) and a poll of
  ``GET /v1/calls`` since the cursor as the backstop. Both reach
  :meth:`AircallAdapter.ingest`, which writes one ``call`` communication per
  Aircall call id through ``communications.ingest.record``.
- **Matching (2.4).** An erased call ref, then an erasure suppression of the
  external number, drop the call: nothing is written, raw included. The
  external number is normalised to E.164 (``parties.normalize``) and
  resolved by ``record``'s participants (exactly one party holds it). An
  unknown number is written **party-less** (a new number is often a new
  lead). No party is ever created.
- **Agent.** ``user.email`` -> ``performed_by`` through the importer's owner
  rule (``crm_import.match.OwnerIndex`` / ``match_owner``: the one active
  staff profile of the company with that email). An archived, unknown or
  ambiguous agent leaves ``performed_by`` empty and sets
  ``metadata.aircall.agent_unmatched``.
- **Recordings.** ``recording``, ``voicemail`` and ``asset`` stay verbatim in
  ``CommunicationRaw`` only (an opaque, expiring provider reference). Golden
  never requests them; the contract test fails on any request not in a
  case's ``requests.expected.json``.
- **Erasure.** Golden writes nothing to Aircall, so the collector names
  nothing and ``erase`` sends nothing (4.4: recordings at Aircall are the
  downstream's to handle). Erased numbers and calls are tombstoned inbound.
"""

from __future__ import annotations

import base64
import hmac
import json
import time

from django.utils import timezone

from communications.ingest import record
from communications.models import Communication
from communications.participants import Participant
from core.integrations.ratelimit import RateLimit
from core.models import ExternalRef
from core.plugins.crm_adapter import (
    AdapterError,
    BaseCrmAdapter,
    Capability,
    IngestOutcome,
    PollPage,
    WebhookCheck,
    envelope,
)
from core.plugins.dispatch import is_suppressed
from core.provenance import is_erased_source

from . import mapping, values


def erased_number(company_id, number: str) -> bool:
    """Whether ``number`` belongs to a person erased in this tenant (an active erasure suppression).

    The phone twin of ``parties.erasure.erased_address``: erasure suppresses
    a person's numbers on both phone channels (``PHONE_CHANNELS``).
    """
    from parties.consent import active_suppression
    from parties.erasure import PHONE_CHANNELS, is_erasure_suppression

    if not number or not number.startswith("+"):
        return False
    for channel in PHONE_CHANNELS:
        suppression = active_suppression(company_id, channel, number)
        if suppression is not None and is_erasure_suppression(suppression):
            return True
    return False


class AircallAdapter(BaseCrmAdapter):
    provider_id = values.PROVIDER
    schema_version = values.SCHEMA_VERSION
    capabilities = frozenset({Capability.POLL, Capability.WEBHOOK, Capability.ERASE})
    rate_limit = RateLimit(requests=values.RATE_REQUESTS, per_seconds=values.RATE_PER_SECONDS)
    docs_url = values.DOCS
    base_url = values.BASE_URL
    settings_keys = frozenset()
    credential_kinds = frozenset({"api_key", "webhook_secret"})

    def __init__(self, account, transport):
        super().__init__(account, transport)
        self._owners = None

    # -- auth ------------------------------------------------------------------

    def _auth(self) -> dict:
        """Basic auth from the ``api_key`` credential, stored as ``<api_id>:<api_token>``."""
        raw = self.secret("api_key").get().strip()
        if ":" not in raw:
            raise AdapterError(
                "aircall: the api_key credential is not '<api_id>:<api_token>' — cause: only one "
                f"half was entered. Fix: store both, joined by a colon. {values.DOCS}"
            )
        encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        self._register(encoded, "api_key")  # the header carries the encoded form
        return {"Authorization": f"Basic {encoded}"}

    # -- inbound: webhook ------------------------------------------------------

    def verify_webhook(self, headers, body: bytes) -> WebhookCheck:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return WebhookCheck(ok=False, reason="malformed")
        token = data.get(values.TOKEN_FIELD) if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            return WebhookCheck(ok=False, reason="missing_signature")
        secret = self.secret("webhook_secret").get()
        if not hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8")):
            return WebhookCheck(ok=False, reason="bad_signature")
        # No signed timestamp: replay protection is the outbox key (2.8) and
        # ingest's idempotency.
        return WebhookCheck(ok=True, delivery_id=mapping.delivery_id(data))

    def webhook_envelope(self, headers, body: bytes) -> dict:
        """The verified body without its token: the shared secret never reaches the outbox."""
        data = json.loads(body.decode("utf-8"))
        payload = {k: v for k, v in data.items() if k != values.TOKEN_FIELD}
        return envelope(self.provider_id, payload, schema_version=self.schema_version)

    # -- inbound: poll ---------------------------------------------------------

    def poll(self, cursor: dict, *, credential=None) -> PollPage:
        """One page of ``GET /v1/calls`` (oldest first) from ``cursor["from"]`` (unix seconds).

        The cursor ends a sweep at the last call seen, or at the oldest call
        still in progress (held back at most ``MAX_HOLD_SECONDS``), so the
        backstop re-reads a call once it ends; the boundary call is re-read
        and is a no-op.
        """
        start = cursor.get("from")
        if not isinstance(start, int):
            since = cursor.get("since")
            start = int(_parse_iso(since).timestamp()) if since else 0
        page = cursor.get("page") if isinstance(cursor.get("page"), int) else 1
        response = self.http("GET", values.CALLS_PATH, headers=self._auth(), params={
            "from": str(start), "order": "asc", "page": str(page), "per_page": str(values.PER_PAGE),
        })
        body = response.json() if response.body else None
        calls = body.get("calls") if isinstance(body, dict) else None
        if not isinstance(calls, list):
            raise AdapterError(f"aircall: the calls page has no calls list — cause: an unexpected "
                               f"response. Fix: check the Aircall API version. {values.DOCS}")
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        hold, last = cursor.get("hold"), cursor.get("last")
        floor = int(timezone.now().timestamp()) - values.MAX_HOLD_SECONDS
        for call in calls:
            started = call.get("started_at") if isinstance(call, dict) else None
            if isinstance(started, bool) or not isinstance(started, int):
                continue
            last = started if last is None else max(last, started)
            if not mapping.is_final(call) and started >= floor:
                hold = started if hold is None else min(hold, started)
        has_next = bool(meta.get("next_page_link"))
        within = page * values.PER_PAGE < values.MAX_LISTING
        if has_next and within:
            next_cursor = {"from": start, "page": page + 1, "hold": hold, "last": last}
            more = True
        else:
            # A capped listing moves on from the last call (asc order); a
            # held call is then left to its call.ended webhook.
            pick = last if (has_next or hold is None) else hold
            next_cursor = {"from": pick if pick is not None else start}
            more = False
        items = [envelope(self.provider_id, {"resource": "call", "event": values.POLL_EVENT, "data": call},
                          schema_version=self.schema_version) for call in calls if isinstance(call, dict)]
        return PollPage(items=items, cursor={k: v for k, v in next_cursor.items() if v is not None},
                        more=more)

    # -- inbound: one ingest -----------------------------------------------------

    def ingest(self, item: dict) -> list[IngestOutcome]:
        payload = self.read_envelope(item)  # rule 2: a newer envelope is refused here
        event, call = mapping.unwrap(payload)
        if call is None:
            reason = "malformed" if event in values.CALL_EVENTS and isinstance(payload, dict) and \
                payload.get("resource") == "call" else f"ignored_event:{event[:40]}"
            return [IngestOutcome(changed=False, skip=True, reason=reason)]
        try:
            fields = mapping.call_fields(call)
        except AdapterError:
            return [IngestOutcome(changed=False, skip=True, reason="malformed")]
        external_id = fields["external_id"]
        if not mapping.is_final(call):
            return [IngestOutcome(changed=False, reason="in_progress", external_id=external_id)]
        company = self.account.company
        # Tombstones first (2.4 steps 1-2): an erased call or number writes nothing, raw included.
        if is_erased_source(company.pk, self.provider_id, values.OBJECT_CALL, external_id):
            return [IngestOutcome(changed=False, skip=True, reason="erased_source", external_id=external_id)]
        if erased_number(company.pk, fields["external_number"]):
            return [IngestOutcome(changed=False, skip=True, reason="erased_number", external_id=external_id)]
        stale = self._unchanged_or_older(company, external_id, call)
        if stale:
            return [IngestOutcome(changed=False, reason=stale, external_id=external_id)]
        agent = self.agent(fields["agent_email"])
        metadata = dict(fields["metadata"])
        if fields["agent_email"] and agent is None:
            metadata["agent_unmatched"] = True
        result = record(
            company, "call", direction=fields["direction"], occurred_at=fields["occurred_at"],
            participants=[Participant(role, address) for role, address in fields["participants"]],
            provider=self.provider_id, external_id=external_id,
            envelope=envelope(self.provider_id, call, schema_version=self.schema_version),
            known_max_version=self.schema_version, summary=fields["summary"], status=fields["status"],
            ended_at=fields["ended_at"], duration_seconds=fields["duration_seconds"],
            performed_by=agent, metadata={self.provider_id: metadata},
        )
        if result.created:
            ExternalRef._base_manager.filter(
                company=company, source=self.provider_id, object_type=values.OBJECT_CALL,
                source_id=external_id,
            ).update(source_url=values.CALL_APP_URL.format(call_id=external_id))
        if result.changed:
            self._touch_in_backfill(result.communication)
        return [IngestOutcome(changed=result.changed, created=result.created, external_id=external_id)]

    def _unchanged_or_older(self, company, external_id: str, call: dict) -> str:
        """``"unchanged"`` / ``"older"`` when the stored copy makes this delivery a no-op, else ``""``.

        Aircall re-signs its media links on every read, so the raw payload of
        a re-read call never hashes the same: compared without them, a
        boundary re-read or a webhook-then-poll copy writes nothing. A copy
        with fewer comments than the stored one is a late, older delivery
        (an out-of-order ``call.commented`` or ``call.ended``) and does not
        roll the summary back. ``record`` keeps every other rule (an erased
        raw payload is never rewritten).
        """
        from django.contrib.contenttypes.models import ContentType

        from communications.models import CommunicationRaw

        ref = ExternalRef._base_manager.filter(
            company=company, source=self.provider_id, object_type=values.OBJECT_CALL, source_id=external_id,
            content_type=ContentType.objects.get_for_model(Communication),
        ).first()
        if ref is None:
            return ""
        raw = CommunicationRaw._base_manager.filter(communication_id=ref.object_id).first()
        stored = raw.payload if raw is not None else None
        if not isinstance(stored, dict) or stored.get("erased"):
            return ""
        if mapping.stable(stored) == mapping.stable(call):
            return "unchanged"
        if mapping.comment_count(call) < mapping.comment_count(stored):
            return "older"
        return ""

    def _touch_in_backfill(self, comm) -> None:
        """A first sync runs suppressed, so ``record`` leaves the stale clock alone: move it here (2.4)."""
        if not is_suppressed() or comm.engagement_id is None:
            return
        if comm.status != Communication.Status.COMPLETED or comm.occurred_at > timezone.now():
            return
        if not comm.kind_entry.counts_as_activity:
            return
        from crm.engagements import touch_activity

        touch_activity(comm.engagement_id, comm.occurred_at)

    def agent(self, email: str):
        """The one active staff profile of the company with ``email`` (Phase 5 owner rule), else None."""
        if not email:
            return None
        from core.models import UserProfile
        from crm_import.match import LINK, OwnerIndex, match_owner

        if self._owners is None:
            self._owners = OwnerIndex(self.account.company)
        decision = match_owner(self._owners, {"data": {"email": email}})
        if decision.decision != LINK or not decision.target:
            return None
        return UserProfile.objects.filter(pk=decision.target["id"], company=self.account.company).first()

    # -- erasure (4.1, 4.4) ------------------------------------------------------

    def erasure_refs(self, footprint) -> list:
        """Nothing: golden writes nothing to Aircall. Its recordings are the downstream's duty (4.4)."""
        return []

    def erase(self, refs, *, done=None):
        from core import outbox

        return outbox.Done()

    # -- connection test ---------------------------------------------------------

    def test_connection(self) -> dict:
        from core.integration_checks import result

        started = time.monotonic()
        response = self.http("GET", values.PING_PATH, headers=self._auth())
        return result(True, "Connected to Aircall.", http_status=response.status,
                      latency_ms=int((time.monotonic() - started) * 1000))


def _parse_iso(value: str):
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
