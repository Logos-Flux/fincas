"""The Klaviyo adapter (docs/crm/phase6-adapters.md 3.2; steps 3 and 3b).

The consent matrix, as built:

======================  =========================================  ============================
golden ``can_contact``   Klaviyo profile ref exists                 no ref
======================  =========================================  ============================
``granted``             upsert profile (if changed), subscribe to   upsert by email (creates the
                        ``list_id`` (once), segment lists (3b)       ref), subscribe
``no_consent`` /        unsubscribe from email marketing (once)     **nothing**: golden never
``withdrawn`` /                                                     uploads a person without
``not_marketable``                                                  consent
``suppressed:opt_out``  unsubscribe (once)                          nothing
/ ``legal`` / erasure
``suppressed:hard_      unsubscribe and add the address to          nothing
bounce`` / ``complaint`` Klaviyo's suppressions (each once)
======================  =========================================  ============================

"Once" is kept on the profile's ``ExternalRef.metadata`` (``state``,
``list``, ``lists``, ``suppressed``, ``fp`` — never personal data; ``address``
is the sha256 the suppression list already keys on), so a repeated sync or
reconcile of an unchanged party sends nothing.

Inbound (events poll; Klaviyo webhooks are not relied on):

=====================================  ===================================================
Klaviyo metric                         golden
=====================================  ===================================================
Received Email                         ``Communication(kind="marketing_email", outbound)``,
                                       no body, no engagement link
Opened / Clicked Email                 counters and first/last times on that communication
Unsubscribed                           withdrawal + ``Suppression(opt_out)``; **never a grant**
Bounced Email (hard)                   ``Suppression(hard_bounce)`` (supersedes an opt-out)
Marked Email as Spam                   ``Suppression(complaint)`` + withdrawal
Subscribed to Email Marketing          **nothing** but a counted marker: not golden consent
=====================================  ===================================================

An event reaches a party through the profile's ref, else an exact match-1 on
its email (2.4); an unknown profile, an erased profile ref and an erased
address write nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from communications.ingest import record
from communications.participants import Participant
from core.integrations.ratelimit import RateLimit
from core.models import ExternalRef
from core.plugins.crm_adapter import (
    AdapterError,
    BaseCrmAdapter,
    Capability,
    IngestOutcome,
    PollPage,
    ProviderRef,
    ProviderRejected,
    account_handler,
    envelope,
)
from core.provenance import is_erased_source
from parties.consent import address_hash, can_contact, record_suppression, record_withdrawal
from parties.erasure import erased_address

from . import mapping, segments, values

logger = logging.getLogger("golden_erp.integrations.klaviyo")

KIND = "marketing_email"
OBJECT_PROFILE = "profile"
OBJECT_EVENT = "event"
PROFILE_CONSUMER = "klaviyo.profile"
RECONCILE_CONSUMER = "klaviyo.reconcile"

#: Party columns whose change can change what Klaviyo holds or may hold (``display_name`` is
#: derived on every save, so it is not one of them: the names are).
RELEVANT_FIELDS = frozenset({
    "given_name", "family_name", "primary_email_normalized", "primary_phone_e164",
    "marketable", "kind", "erased_at", "merged_into",
})
#: Suppression reasons golden also pushes into Klaviyo's suppression list.
PUSHED_SUPPRESSIONS = frozenset({"hard_bounce", "complaint"})
#: ``ConsentRecord.source`` for provider write-backs (``ConsentSource`` has no
#: integration value; ``source_detail`` names the provider and event).
CONSENT_SOURCE = "api"
#: Marker on the event ref of an ignored Klaviyo-side subscribe (the reconcile report counts them).
IGNORED_SUBSCRIBE = "provider_subscribe"
#: The reconcile window starts this much before the last run's start.
RECONCILE_SKEW_SECONDS = 300


def _party_ct():
    from parties.models import Party

    return ContentType.objects.get_for_model(Party)


class KlaviyoAdapter(BaseCrmAdapter):
    """Profiles and consent out, sends and opt-outs in. See the module docstring."""

    provider_id = values.PROVIDER
    schema_version = values.SCHEMA_VERSION
    capabilities = frozenset({Capability.POLL, Capability.PUSH_PROFILES, Capability.ERASE})
    rate_limit = RateLimit(requests=3, per_seconds=1)
    docs_url = values.DOCS
    base_url = values.BASE_URL
    settings_keys = frozenset({"list_id", "segment_lists", "log_sends"})
    credential_kinds = frozenset({"api_key"})
    push_dedupe = ("profile-import upserts by email (Klaviyo's natural key), so a retried create adopts "
                   "the profile a lost response made; subscription, unsubscribe and suppression jobs "
                   "are idempotent per profile and list")

    # -- configuration -------------------------------------------------------

    def validate_settings(self, settings: dict) -> list[str]:
        errors = super().validate_settings(settings)
        if not isinstance(settings, dict):
            return errors
        if "list_id" in settings and (not isinstance(settings["list_id"], str) or not settings["list_id"].strip()):
            errors.append("list_id must be the Klaviyo list id — cause: an empty or non-string value. "
                          "Fix: paste the marketing list's id from Klaviyo.")
        if "segment_lists" in settings:
            main = settings.get("list_id") if isinstance(settings.get("list_id"), str) else ""
            errors += segments.validate(settings["segment_lists"], main)
        if "log_sends" in settings and not isinstance(settings["log_sends"], bool):
            errors.append("log_sends must be true or false — cause: another JSON value. "
                          "Fix: true logs each campaign send on the timeline (the default).")
        return errors

    def _headers(self, *, body: bool = False) -> dict:
        headers = {
            "Authorization": f"{values.AUTH_SCHEME} {self.secret('api_key').get()}",
            "revision": values.API_REVISION,
            "accept": values.CONTENT_TYPE,
        }
        if body:
            headers["content-type"] = values.CONTENT_TYPE
        return headers

    def test_connection(self) -> dict:
        from core.integration_checks import result

        response = self.http("GET", values.EVENTS, headers=self._headers(), params={"page[size]": 1})
        return result(True, "Connected (events readable).", http_status=response.status)

    # -- inbound --------------------------------------------------------------

    def poll(self, cursor: dict, *, credential=None) -> PollPage:
        since = cursor.get("since") or ""
        if not since:
            raise AdapterError(f"klaviyo: events poll without a starting time — cause: an empty cursor. "
                               f"Fix: run integrations_poll, which sets the first-sync horizon. {values.DOCS}")
        since = mapping.iso(mapping.parse_time(since)) or since  # one format, whoever wrote the cursor
        # Re-read an overlap before the cursor: a bounce or an API-sent event can
        # surface after a later one; ingest is idempotent per event id.
        start = mapping.parse_time(since)
        window = mapping.iso(start - timedelta(seconds=values.EVENTS_OVERLAP_SECONDS)) if start else since
        params = {"filter": mapping.events_filter(window), "sort": values.EVENTS_SORT,
                  "include": values.EVENTS_INCLUDE, "page[size]": values.EVENTS_PAGE_SIZE}
        if cursor.get("page"):
            params["page[cursor]"] = cursor["page"]
        response = self.http("GET", values.EVENTS, headers=self._headers(), params=params)
        try:
            payloads, next_page = mapping.page_items(response.json())
        except ValueError:
            raise AdapterError(f"klaviyo: events page has no data list — cause: an unexpected response. "
                               f"Fix: confirm the API revision {values.API_REVISION}. {values.DOCS}") from None
        items = [envelope(self.provider_id, p, schema_version=self.schema_version) for p in payloads]
        if next_page:
            return PollPage(items=items, cursor={"since": since, "page": next_page}, more=True)
        latest = mapping.parse_time(since)
        for payload in payloads:
            stamp = mapping.parse_time(((payload.get("event") or {}).get("attributes") or {}).get("datetime"))
            if stamp is not None and (latest is None or stamp > latest):
                latest = stamp
        return PollPage(items=items, cursor={"since": mapping.iso(latest) or since}, more=False)

    def ingest(self, item: dict) -> list[IngestOutcome]:
        payload = self.read_envelope(item)  # rule 2: a newer revision is refused before any read
        ev = mapping.parse_event(payload)
        if ev is None:
            return [IngestOutcome(changed=False, skip=True, reason="unreadable_event")]
        handler = {
            values.METRIC_RECEIVED: self._received,
            values.METRIC_OPENED: self._engaged,
            values.METRIC_CLICKED: self._engaged,
            values.METRIC_UNSUBSCRIBED: self._unsubscribed,
            values.METRIC_BOUNCED: self._bounced,
            values.METRIC_SPAM: self._spam,
            values.METRIC_SUBSCRIBED: self._subscribed,
        }.get(ev.metric)
        if handler is None:
            return [self._skip(ev, "unsupported_metric")]
        if ev.metric != values.METRIC_RECEIVED and self._event_ref(ev) is not None:
            return [IngestOutcome(changed=False, external_id=ev.event_id)]  # already applied
        party, reason = self._party_for(ev)
        if party is None:
            return [self._skip(ev, reason)]
        with transaction.atomic():  # a withdrawal, its suppression and the event marker land together
            return [handler(ev, party, item)]

    @staticmethod
    def _skip(ev, reason: str) -> IngestOutcome:
        return IngestOutcome(changed=False, skip=True, reason=reason, external_id=ev.event_id)

    def _party_for(self, ev):
        """The event's party (2.4): tombstones first, then the profile ref, then match-1 by email."""
        from parties.match import MATCH_1, find

        company = self.account.company
        if ev.profile_id and is_erased_source(company.pk, self.provider_id, OBJECT_PROFILE, ev.profile_id):
            return None, "erased_source"
        if ev.email and erased_address(company.pk, ev.email):
            return None, "erased_address"
        party = None
        if ev.profile_id:
            ref = self._profile_ref(ev.profile_id)
            if ref is not None:
                party = _live_person(company, ref.object_id)
        if party is None and ev.email:
            result = find(company, email=ev.email)
            if result.state == MATCH_1:
                party = result.party
        if party is None:
            return None, "unknown_profile"  # a Klaviyo-only address never becomes a party
        return party, ""

    def _profile_ref(self, profile_id: str):
        return ExternalRef._base_manager.filter(
            company=self.account.company, source=self.provider_id, object_type=OBJECT_PROFILE,
            source_id=profile_id, content_type=_party_ct(),
        ).exclude(metadata__has_key="erased").first()

    def _event_ref(self, ev):
        return ExternalRef._base_manager.filter(
            company=self.account.company, source=self.provider_id, object_type=OBJECT_EVENT,
            source_id=ev.event_id,
        ).first()

    def _mark_event(self, ev, target, *, metadata=None) -> None:
        ExternalRef.objects.create(
            company=self.account.company, source=self.provider_id, object_type=OBJECT_EVENT,
            source_id=ev.event_id, content_type=ContentType.objects.get_for_model(type(target)),
            object_id=target.pk, last_synced_at=timezone.now(), metadata=metadata or {},
        )

    def _received(self, ev, party, item) -> IngestOutcome:
        if self.account.settings.get("log_sends", True) is False:
            return self._skip(ev, "send_logging_off")
        address = ev.email or primary_email(party)
        meta = {"klaviyo.campaign_id": ev.message_id} if ev.message_id else {}
        result = record(
            self.account.company, KIND, direction="outbound", occurred_at=ev.occurred_at,
            participants=[Participant("to", address)] if address else [],
            provider=self.provider_id, external_id=ev.event_id, envelope=self.raw_envelope(item),
            known_max_version=self.schema_version, subject=ev.subject, party=party,
            thread_key=_send_key(ev, party), metadata=meta, link_engagement=False,
        )
        return IngestOutcome(changed=result.changed, created=result.created, external_id=ev.event_id)

    def _engaged(self, ev, party, item) -> IngestOutcome:
        """Opened / Clicked: fold into the send's communication; no row of its own."""
        from communications.ingest import ERASED_PAYLOAD
        from communications.models import Communication, CommunicationRaw

        comm = Communication.objects.select_for_update().filter(
            company=self.account.company, provider=self.provider_id, kind=KIND, thread_key=_send_key(ev, party),
            party=party,
        ).first()
        if comm is None:
            return self._skip(ev, "send_unknown")
        raw = CommunicationRaw._base_manager.filter(communication=comm).first()
        if raw is not None and raw.payload == ERASED_PAYLOAD:
            return self._skip(ev, "erased_communication")
        what = "opens" if ev.metric == values.METRIC_OPENED else "clicks"
        meta = dict(comm.metadata or {})
        meta[f"klaviyo.{what}"] = int(meta.get(f"klaviyo.{what}") or 0) + 1
        first_key, last_key = f"klaviyo.first_{what[:-1]}_at", f"klaviyo.last_{what[:-1]}_at"
        first, last = mapping.parse_time(meta.get(first_key)), mapping.parse_time(meta.get(last_key))
        if first is None or ev.occurred_at < first:
            meta[first_key] = mapping.iso(ev.occurred_at)
        if last is None or ev.occurred_at > last:
            meta[last_key] = mapping.iso(ev.occurred_at)
        Communication.objects.filter(pk=comm.pk).update(metadata=meta)
        self._mark_event(ev, comm)
        return IngestOutcome(changed=True, external_id=ev.event_id)

    def _withdraw_and_suppress(self, ev, party, reason: str, *, withdraw: bool) -> bool:
        address = ev.email or primary_email(party)
        detail = f"{self.provider_id}:{ev.event_id}"
        changed = False
        if withdraw:
            changed |= _withdraw(party, address, occurred_at=ev.occurred_at, detail=detail)
        if address:
            _suppression, created = record_suppression(
                self.account.company_id, "email", address, reason, source=CONSENT_SOURCE, source_detail=detail,
            )
            changed |= created
        # What Klaviyo now holds, so the next push is a no-op (no echo).
        ref = self._profile_ref(ev.profile_id) if ev.profile_id else None
        if ref is not None:
            meta = {**(ref.metadata or {}), "state": "unsubscribed"}
            if reason in PUSHED_SUPPRESSIONS:
                meta["suppressed"] = True
            ExternalRef._base_manager.filter(pk=ref.pk).update(metadata=meta)
        self._mark_event(ev, party)
        return changed

    def _unsubscribed(self, ev, party, item) -> IngestOutcome:
        self._withdraw_and_suppress(ev, party, "opt_out", withdraw=True)
        return IngestOutcome(changed=True, external_id=ev.event_id)

    def _bounced(self, ev, party, item) -> IngestOutcome:
        if not ev.hard_bounce:
            return self._skip(ev, "soft_bounce")
        self._withdraw_and_suppress(ev, party, "hard_bounce", withdraw=False)
        return IngestOutcome(changed=True, external_id=ev.event_id)

    def _spam(self, ev, party, item) -> IngestOutcome:
        self._withdraw_and_suppress(ev, party, "complaint", withdraw=True)
        return IngestOutcome(changed=True, external_id=ev.event_id)

    def _subscribed(self, ev, party, item) -> IngestOutcome:
        """A Klaviyo-side subscribe is **not** golden consent (A12): no ConsentRecord, ever.

        The event is marked so the reconcile report can count it and staff
        can record a real, evidenced grant.
        """
        self._mark_event(ev, party, metadata={"ignored": IGNORED_SUBSCRIBE})
        logger.info("[integrations] klaviyo subscribe ignored (not golden consent): account=%s party=%s",
                    self.account.pk, party.pk)
        return IngestOutcome(changed=True, external_id=ev.event_id, reason="provider_subscribe_ignored")

    # -- outbound -------------------------------------------------------------

    @classmethod
    def outbox_consumers(cls) -> list:
        from core import outbox

        return super().outbox_consumers() + [
            outbox.OutboxConsumer(
                name=PROFILE_CONSUMER, signals=("party_created", "party_updated", "party_merged"),
                build=build_profile_sync, enabled=_any_enabled_account,
                handle=account_handler(cls, sync_profile), provider=cls.provider_id,
            ),
            outbox.OutboxConsumer(name=RECONCILE_CONSUMER, handle=account_handler(cls, run_reconcile),
                                  provider=cls.provider_id, max_attempts=3),
        ]

    # -- reconcile (problem 10: consent and suppression rows send no signal) --

    def reconcile(self, *, full: bool = False) -> dict:
        """Enqueue a profile sync for every party whose Klaviyo state may have changed.

        Since the last reconcile (``sync_state["reconcile_at"]``): parties
        with a new email-marketing ``ConsentRecord``, a new profile ref, or a
        suppression created or lifted on an address golden pushed (any of
        their profile refs) or on a consent holder's primary address; and, for
        3b, subscribed profiles whose segment lists differ from the segments.
        ``full`` (or a first run) takes every party with a profile ref or an
        email-marketing consent record. Nothing is enqueued before the
        account's first poll has pulled Klaviyo's opt-outs. No provider call.
        """
        from core import outbox
        from parties.models import ConsentRecord, Party, Suppression

        account = self.account
        company = account.company
        if not (account.sync_state or {}).get("initial_sync_done"):
            return {"enqueued": 0, "waiting_for_first_poll": 1}
        started = timezone.now()
        since = None if full else mapping.parse_time((account.sync_state or {}).get("reconcile_at"))
        if since is not None:
            since -= timedelta(seconds=RECONCILE_SKEW_SECONDS)  # rows committed late, stamped early
        refs = list(ExternalRef._base_manager.filter(
            company=company, source=self.provider_id, object_type=OBJECT_PROFILE, content_type=_party_ct(),
        ).exclude(metadata__has_key="erased").values_list("object_id", "metadata", "first_seen_at"))
        consents = ConsentRecord.objects.filter(
            company=company, channel="email", purpose="marketing", party__isnull=False)
        ids: set = set()
        if since is None:
            ids |= {r[0] for r in refs}
            ids |= set(consents.values_list("party_id", flat=True))
        else:
            ids |= set(consents.filter(recorded_at__gte=since).values_list("party_id", flat=True))
            ids |= {r[0] for r in refs if r[2] and r[2] >= since}
            changed = set(Suppression.objects.filter(company=company, channel="email").filter(
                Q(created_at__gte=since) | Q(lifted_at__gte=since)).values_list("address_hash", flat=True))
            if changed:
                ids |= {r[0] for r in refs if (r[1] or {}).get("address") in changed}
                for pk, email in Party.objects.filter(
                        company=company, pk__in=consents.values("party_id")).exclude(
                        primary_email_normalized="").values_list("pk", "primary_email_normalized"):
                    if address_hash(email) in changed:
                        ids.add(pk)
        ids |= self._segment_drift(refs)
        live = Party.objects.filter(company=company, pk__in=ids, kind=Party.Kind.PERSON,
                                    merged_into__isnull=True, erased_at__isnull=True)
        stamp = started.isoformat()
        specs = [outbox.EventSpec(
            idempotency_key=f"{pk}:reconcile:{stamp}", payload={"reason": "reconcile"}, event="sync",
            company_id=company.pk, object_type="parties.party", object_id=pk, party_id=pk,
            coalesce_key=f"party:{pk}", occurred_at=started,
        ) for pk in sorted(live.values_list("pk", flat=True), key=str)]
        enqueued = outbox.enqueue_many(PROFILE_CONSUMER, specs) if specs else 0
        _merge_sync_state(account, reconcile_at=stamp)
        ignored = ExternalRef._base_manager.filter(
            company=company, source=self.provider_id, object_type=OBJECT_EVENT,
            metadata__ignored=IGNORED_SUBSCRIBE).count()
        return {"enqueued": enqueued, "provider_subscribes_ignored": ignored}

    def _segment_drift(self, refs) -> set:
        """Subscribed profiles whose 3b segment lists differ from the segments' members.

        Only profiles golden holds as subscribed can drift: anyone else is
        either not pushed at all (no consent, suppressed) or already off
        every list, and re-checking them hourly would change nothing.
        """
        mapping_ = (self.account.settings or {}).get("segment_lists") or {}
        if not mapping_:
            return set()
        company = self.account.company
        held = {}
        for object_id, meta, _seen in refs:
            if (meta or {}).get("state") == "subscribed":
                held.setdefault(object_id, set()).update((meta or {}).get("lists") or [])
        if not held:
            return set()
        drift = set()
        for key, list_id in mapping_.items():
            qs = segments.members(company, key)
            members = set(qs.filter(pk__in=list(held)).values_list("pk", flat=True)) if qs is not None else set()
            drift |= {pk for pk in members if list_id not in held[pk]}
            drift |= {pk for pk, lists in held.items() if list_id in lists and pk not in members}
        return drift

    # -- erasure (section 4; called from erase_party by step 8) ---------------

    def erasure_refs(self, footprint) -> list[ProviderRef]:
        """The Klaviyo profile ids golden wrote for the person, read before ``_flag_refs`` hashes them.

        ``footprint`` is ``parties.erasure``'s: ``refs`` (ExternalRef pks) and
        ``parties`` (Party pks). Already-hashed (erased) ids are not ids any more.
        """
        from core.provenance import ERASED_PREFIX

        q = Q(pk__in=getattr(footprint, "refs", ()) or ())
        q |= Q(content_type=_party_ct(), object_id__in=getattr(footprint, "parties", ()) or ())
        ids = ExternalRef._base_manager.filter(
            q, company=self.account.company, source=self.provider_id, object_type=OBJECT_PROFILE,
        ).values_list("source_id", flat=True)
        return [ProviderRef(OBJECT_PROFILE, pid) for pid in sorted(set(ids)) if not pid.startswith(ERASED_PREFIX)]

    def erase(self, refs, *, done=None) -> None:
        """File one data-privacy deletion request per profile (Klaviyo deletes the profile and its history).

        A 404 is a profile already gone: done. ``done(ref)`` is called after
        each one, so a retry after a partial failure resumes where it stopped.
        """
        for ref in refs:
            object_type = ref.object_type if isinstance(ref, ProviderRef) else (ref or {}).get("object_type")
            source_id = ref.source_id if isinstance(ref, ProviderRef) else (ref or {}).get("source_id")
            if object_type == OBJECT_PROFILE and isinstance(source_id, str) and source_id:
                self.http("POST", values.DELETION, headers=self._headers(body=True),
                          json=mapping.deletion_body(source_id), expect=(202, 404))
            if done is not None:
                done(ref)


# --- outbox handlers --------------------------------------------------------------


def _any_enabled_account() -> bool:
    from core.models import IntegrationAccount

    return IntegrationAccount.objects.filter(provider=values.PROVIDER, enabled=True).exists()


def build_profile_sync(signal_name, sender, obj, kwargs) -> list:
    """``party_created`` / ``party_updated`` / ``party_merged`` -> one coalesced profile sync.

    Runs in the emitting transaction: one query (the account). A person only;
    an update that wrote none of :data:`RELEVANT_FIELDS` is no change for Klaviyo.
    """
    from core import outbox
    from core.models import IntegrationAccount
    from parties.models import Party

    party = kwargs.get("survivor") or kwargs.get("party") or obj
    if not isinstance(party, Party) or party.kind != Party.Kind.PERSON:
        return []
    update_fields = kwargs.get("update_fields") or ()
    if signal_name == "party_updated" and update_fields and not (set(update_fields) & RELEVANT_FIELDS):
        return []
    if not IntegrationAccount.objects.filter(company_id=party.company_id, provider=values.PROVIDER,
                                             enabled=True).exists():
        return []
    now = timezone.now()
    marker = kwargs.get("merge_id") or (party.updated_at or now).isoformat()
    return [outbox.EventSpec(
        idempotency_key=f"{party.pk}:{signal_name}:{marker}", payload={"reason": signal_name},
        event="sync", company_id=party.company_id, object_type="parties.party", object_id=party.pk,
        party_id=party.pk, coalesce_key=f"party:{party.pk}", occurred_at=now,
    )]


def primary_email(party) -> str:
    """The party's primary address (normalised): the denormalised column, else its oldest email."""
    if party.primary_email_normalized:
        return party.primary_email_normalized
    return party.emails.order_by("created_at", "pk").values_list("email_normalized", flat=True).first() or ""


def _live_person(company, party_id):
    from parties.models import Party

    party = Party._base_manager.filter(company=company, pk=party_id).first()
    seen = set()
    while party is not None and party.merged_into_id and party.pk not in seen:
        seen.add(party.pk)
        party = Party._base_manager.filter(company=company, pk=party.merged_into_id).first()
    if party is None or party.erased_at is not None or party.kind != Party.Kind.PERSON:
        return None
    return party


def _fingerprint(attributes: dict) -> str:
    """What golden last sent, to skip an unchanged upsert. The party id is constant per ref, so left out."""
    sent = {k: v for k, v in attributes.items() if k != "external_id"}
    return hashlib.sha256(json.dumps(sent, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _save_meta(ref, **changes) -> None:
    ref.metadata = {**(ref.metadata or {}), **changes}
    with transaction.atomic():
        ExternalRef._base_manager.filter(pk=ref.pk).update(metadata=ref.metadata, last_synced_at=timezone.now())


def _granted_at(party, email: str) -> str:
    from parties.models import ConsentRecord

    row = ConsentRecord.objects.filter(
        party=party, channel="email", purpose="marketing", state=ConsentRecord.State.GRANTED,
        address_normalized__in=["", email],
    ).order_by("-occurred_at", "-recorded_at").first()
    return mapping.iso(row.occurred_at) if row is not None else ""


def sync_profile(adapter: KlaviyoAdapter, event):
    """``klaviyo.profile``: bring Klaviyo in line with golden's consent for one party (2.5, 3.2).

    State-based (A5): reads the party now, so a late retry sends today's
    truth and a row about an erased or merged-away party sends nothing. Waits
    for the account's first poll, so Klaviyo's existing opt-outs are in golden
    before golden subscribes anyone.
    """
    from core import outbox

    account = adapter.account
    company = account.company
    party = _live_person(company, event.object_id)
    if party is None or str(party.pk) != str(event.object_id):
        # Erased, merged away (the survivor has its own row) or gone: nothing personal to send.
        return outbox.Cancel("party_gone")
    if not (account.sync_state or {}).get("initial_sync_done"):
        # Nothing is lost: the first reconcile after the first poll is a full one.
        return outbox.Cancel("waiting_for_first_poll")
    refs = list(ExternalRef._base_manager.filter(
        company=company, source=adapter.provider_id, object_type=OBJECT_PROFILE, content_type=_party_ct(),
        object_id=party.pk,
    ).exclude(metadata__has_key="erased").order_by("first_seen_at", "pk"))
    email = primary_email(party)
    digest = address_hash(email) if email else ""
    decision = can_contact(party, "email", "marketing", email) if email else None
    mine = [r for r in refs if digest and (r.metadata or {}).get("address") == digest]
    others = [r for r in refs if r not in mine]
    if decision is not None and decision.allowed:
        outcome = _subscribe(adapter, party, email, digest, mine[0] if mine else None)
    else:
        reason = decision.reason if decision is not None else "no_address"
        suppressed_reason = reason.split(":", 1)[1] if reason.startswith("suppressed:") else ""
        for ref in mine:  # none: golden never uploads a person without consent
            _unsubscribe(adapter, ref, email=email)
            if suppressed_reason in PUSHED_SUPPRESSIONS and not (ref.metadata or {}).get("suppressed"):
                adapter.http("POST", values.SUPPRESS, headers=adapter._headers(body=True),
                             json=mapping.suppress_body(email=email), expect=(202,))
                _save_meta(ref, suppressed=True)
        outcome = outbox.Done()
    # A profile for another address (an old email, a merged-in duplicate) is
    # never marketed to: golden's consent answers for the primary address only.
    # After the primary, and one refusal never blocks the rest of the party's sync.
    for ref in others:
        try:
            _unsubscribe(adapter, ref, email="")
        except ProviderRejected as exc:
            _save_meta(ref, unsub_failed=exc.status)
            logger.warning("[integrations] klaviyo refused to unsubscribe an old-address profile by id "
                           "(HTTP %s): account=%s ref=%s", exc.status, account.pk, ref.pk)
    return outcome


def _unsubscribe(adapter, ref, *, email: str) -> None:
    """Unsubscribe one profile from email marketing, once (the ref remembers)."""
    if (ref.metadata or {}).get("state") == "unsubscribed":
        return
    adapter.http("POST", values.UNSUBSCRIBE, headers=adapter._headers(body=True),
                 json=mapping.unsubscribe_body(email=email, profile_id=ref.source_id), expect=(202,))
    _save_meta(ref, state="unsubscribed")


def _import(adapter, attributes: dict, profile_id: str):
    """``(status, profile id)`` of a profile-import upsert. A stale id (404) is retried by email alone."""
    response = adapter.http("POST", values.PROFILE_IMPORT, headers=adapter._headers(body=True),
                            json=mapping.profile_import_body(attributes, profile_id), expect=(200, 201, 404))
    if response.status == 404 and profile_id:
        return _import(adapter, attributes, "")
    found = mapping.profile_id_of(response.json()) if response.status != 404 else ""
    if not found:
        raise AdapterError(f"klaviyo: profile-import answered without a profile id — cause: an unexpected "
                           f"response. Fix: confirm the API revision {values.API_REVISION}. {values.DOCS}")
    return response.status, found


def _klaviyo_opt_out(adapter, profile_id: str):
    """Klaviyo's own email-marketing refusal for a profile golden did not create.

    ``mapping.Refusal`` (withdraw, suppression reason, when). Read before golden
    subscribes a profile it adopted: an opt-out recorded in Klaviyo before
    golden was connected (or older than the first poll's horizon) must come
    into golden, never be overwritten.
    """
    response = adapter.http("GET", values.PROFILE.format(profile_id=profile_id), headers=adapter._headers(),
                            params={"additional-fields[profile]": "subscriptions"})
    return mapping.marketing_refusal(response.json())


def _subscribe(adapter, party, email, digest, ref):
    from core import outbox

    settings = adapter.account.settings or {}
    list_id = settings.get("list_id")
    if not isinstance(list_id, str) or not list_id:
        return outbox.Dead("no_list_id")
    attributes = mapping.profile_attributes(party, email)
    fp = _fingerprint(attributes)
    if ref is None or (ref.metadata or {}).get("fp") != fp:
        previous = ref.source_id if ref is not None else ""
        status, profile_id = _import(adapter, attributes, previous)
        ref = _adopt_ref(adapter, party, profile_id, digest)
        if ref is None:
            return outbox.Done()  # erased while this sync was in flight: a klaviyo.erase row deletes it
        changes = {"fp": fp}
        if status == 200 and (profile_id != previous or not (ref.metadata or {}).get("state")):
            # A profile Klaviyo already had, newly ours: its own opt-out is read
            # before anything is subscribed. Persisted, so a failed read is
            # retried, never skipped.
            changes["optout_check"] = "pending"
        _save_meta(ref, **changes)
    if (ref.metadata or {}).get("optout_check") == "pending":
        refusal = _klaviyo_opt_out(adapter, ref.source_id)
        pulled = _pull_opt_out(adapter, party, email, ref.source_id, refusal)
        meta = {**(ref.metadata or {})}
        meta.pop("optout_check", None)
        if pulled:
            meta.update(state="unsubscribed", suppressed=refusal.reason in PUSHED_SUPPRESSIONS)
        _replace_meta(ref, meta)
        if pulled:
            return outbox.Done()
    meta = ref.metadata or {}
    if meta.get("state") != "subscribed" or meta.get("list") != list_id:
        adapter.http("POST", values.SUBSCRIBE, headers=adapter._headers(body=True),
                     json=mapping.subscribe_body(list_id, email=email, profile_id=ref.source_id,
                                                 consented_at=_granted_at(party, email)),
                     expect=(202,))
        _save_meta(ref, state="subscribed", list=list_id)
    # 3b: segment lists are membership only (consent rides on the main list's
    # subscription), and only ever while can_contact allows: this is the granted branch.
    wanted = segments.lists_for(adapter.account.company, party, settings.get("segment_lists") or {})
    wanted.discard(list_id)
    have = set((ref.metadata or {}).get("lists") or [])
    for segment_list in sorted(wanted - have):
        adapter.http("POST", values.LIST_PROFILES.format(list_id=segment_list),
                     headers=adapter._headers(body=True), json=mapping.list_members_body(ref.source_id),
                     expect=(204,))
        have.add(segment_list)
        _save_meta(ref, lists=sorted(have))
    for segment_list in sorted(have - wanted):
        adapter.http("DELETE", values.LIST_PROFILES.format(list_id=segment_list),
                     headers=adapter._headers(body=True), json=mapping.list_members_body(ref.source_id),
                     expect=(204,))
        have.discard(segment_list)
        _save_meta(ref, lists=sorted(have))
    return outbox.Done()


def _pull_opt_out(adapter, party, email, profile_id, refusal) -> bool:
    """Bring a refusal Klaviyo already held into golden: withdrawal and suppression, never a grant.

    Dated at Klaviyo's own time for the change when it gives one. A refusal
    older than golden's latest grant was overtaken by a newer golden opt-in
    (golden owns consent): only an address fault (a hard bounce, a complaint)
    is still recorded then. Unknown time: recorded now (fails safe). Returns
    whether golden must not subscribe.
    """
    if not (refusal.withdraw or refusal.reason):
        return False
    granted = mapping.parse_time(_granted_at(party, email))
    overtaken = refusal.when is not None and granted is not None and refusal.when < granted
    reason = refusal.reason
    if overtaken and reason not in PUSHED_SUPPRESSIONS:
        return False
    detail = f"{adapter.provider_id}:profile:{profile_id}"
    with transaction.atomic():
        if refusal.withdraw and not overtaken:
            _withdraw(party, email, occurred_at=refusal.when or timezone.now(), detail=detail)
        if reason:
            record_suppression(adapter.account.company_id, "email", email, reason, source=CONSENT_SOURCE,
                               source_detail=detail)
    return True


def _withdraw(party, address: str, *, occurred_at, detail: str) -> bool:
    """A provider refusal as golden withdrawals: party-wide, and for ``address`` and the
    primary address too, because ``can_contact`` answers from an address-specific
    record before a party-wide one. Never a grant. Returns whether anything was written."""
    created_any = False
    for target in dict.fromkeys(["", address, primary_email(party)]):
        if target is None:
            continue
        _record, created = record_withdrawal(party, "email", "marketing", occurred_at=occurred_at,
                                             source=CONSENT_SOURCE, source_detail=detail, address=target)
        created_any |= created
    return created_any


def _replace_meta(ref, meta: dict) -> None:
    ref.metadata = meta
    with transaction.atomic():
        ExternalRef._base_manager.filter(pk=ref.pk).update(metadata=meta, last_synced_at=timezone.now())


def _adopt_ref(adapter, party, profile_id: str, digest: str):
    """The profile's ref, created or re-pointed at ``party`` (rule 4: the upsert found or made it).

    Re-pointing starts the bookkeeping afresh: what Klaviyo was told for the
    previous holder says nothing about this party.

    None when the party was erased while this sync was in flight (step 8): the
    upsert reached Klaviyo after ``erase_party`` collected the person's
    profile ids. The party lock serialises the two (erasure holds it until it
    commits); the ref is then written tombstoned and a ``klaviyo.erase`` row
    files the deletion.
    """
    from parties.models import Party

    with transaction.atomic():
        erased = Party._base_manager.select_for_update().filter(pk=party.pk).values_list(
            "erased_at", flat=True).first()
        if erased is not None:
            from core.integrations.erasure import erase_late
            from core.provenance import erased_source_id

            ExternalRef._base_manager.update_or_create(
                company=adapter.account.company, source=adapter.provider_id, object_type=OBJECT_PROFILE,
                source_id=erased_source_id(profile_id),
                defaults={"content_type": _party_ct(), "object_id": party.pk,
                          "metadata": {"erased": True, "erased_at": timezone.now().isoformat()}},
            )
            erase_late(adapter, [ProviderRef(OBJECT_PROFILE, profile_id)], party_id=party.pk,
                       reason="push_in_flight")
            return None
        ref, created = ExternalRef._base_manager.select_for_update().get_or_create(
            company=adapter.account.company, source=adapter.provider_id, object_type=OBJECT_PROFILE,
            source_id=profile_id,
            defaults={"content_type": _party_ct(), "object_id": party.pk, "last_synced_at": timezone.now(),
                      "source_url": values.PROFILE_URL.format(profile_id=profile_id),
                      "metadata": {"address": digest}},
        )
        if not created and (ref.object_id != party.pk or (ref.metadata or {}).get("address") != digest):
            ref.object_id = party.pk
            ref.content_type = _party_ct()
            ref.metadata = {"address": digest}
            ref.save(update_fields=["object_id", "content_type", "metadata"])
    return ref


def run_reconcile(adapter: KlaviyoAdapter, event):
    """``klaviyo.reconcile``: the sweep as an outbox row (no provider call)."""
    from core import outbox

    adapter.reconcile(full=bool((event.payload or {}).get("full")))
    return outbox.Done()


def _merge_sync_state(account, **changes) -> None:
    """Set keys of ``account.sync_state`` without overwriting a concurrent poll's cursor."""
    model = type(account)
    with transaction.atomic():
        row = model._base_manager.select_for_update().get(pk=account.pk)
        state = {**(row.sync_state or {}), **changes}
        model._base_manager.filter(pk=account.pk).update(sync_state=state)
    account.sync_state = state


def _send_key(ev, party) -> str:
    """One send to one recipient: the thread its opens and clicks fold into."""
    return f"klaviyo:{ev.message_id or ev.event_id}:{ev.profile_id or party.pk}"[:255]
