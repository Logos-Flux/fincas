"""Erasure across adapters (docs/crm/phase6-adapters.md section 4, step 8).

``parties.erasure.erase_party`` calls :func:`collect` inside its transaction,
after it scrubbed the person's CRM rows and **before** ``_flag_refs`` re-keys
their refs to a hash, and :func:`enqueue` after its outbox purge. Nothing here
calls a provider: the provider work is the ``<provider>.erase`` outbox row,
handled by :func:`handle_erase` with no transaction open. So:

- a provider that is down, throttled or rejecting the credentials never blocks
  or rolls back the local erasure; the row retries (capped backoff), goes
  ``dead`` after :data:`ERASE_MAX_ATTEMPTS`, and ``check_erasure`` /
  ``check_outbox`` report it until it is done. It is never cancelled: a
  disabled or deleted account keeps the row waiting (``account_disabled``)
  instead of the ``Cancel`` other adapter rows get;
- each adapter's collector runs in its own savepoint. A collector that raises
  (a bug, a database error) does not fail the erasure: the row is written
  from the generic collector instead (every ref of that provider on the
  footprint's rows, read in clear), flagged ``collector_failed``, and the
  failure is logged at ERROR;
- an account whose adapter is no longer installed, but which has refs on the
  footprint, still gets its row (with no handler): ``check_outbox`` reports
  it as orphaned and ``check_erasure`` as pending, so PII left at a provider
  golden can no longer reach is visible, not forgotten.

The provider ids travel in clear in the row until the handler is done with
each one (the row then keeps ``{"erased": true, "count": n}``): the one window
in which golden holds a key to the person's provider records (4.1).
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import ProtectedError, Q
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger("golden_erp.integrations")

ERASE_SUFFIX = ".erase"
NO_ACCOUNT = "none"
EVENT = "erase"
#: About two days of capped backoff before an erase row goes dead.
ERASE_MAX_ATTEMPTS = 20
#: A sweep makes a few calls per record (ClickUp: rename, then clear a field).
ERASE_LEASE_S = 300
#: How long an erase row for a disabled or missing account waits between checks.
INACTIVE_RETRY = timedelta(hours=1)
#: ``check_erasure``'s default alarm age for a pending row.
DEFAULT_MAX_AGE = timedelta(hours=24)


def consumer_name(provider: str) -> str:
    return f"{provider}{ERASE_SUFFIX}"


@dataclass(frozen=True)
class Sweep:
    """What one provider must erase: its account and the refs, in clear."""

    provider: str
    account_id: uuid.UUID | None  # None: the provider holds records but the tenant has no account any more
    refs: tuple
    collector_failed: bool = False
    installed: bool = True


# -- collect (inside erase_party's transaction) ---------------------------------------


def collect(fp, company_id) -> list[Sweep]:
    """One :class:`Sweep` per account of the tenant whose provider holds records about the person.

    Every account counts, enabled or not: a disabled account's provider still
    holds what golden wrote while it was on. Reads only (no row locks), so
    erasure's lock order (Customer -> Party -> User) is unchanged.
    """
    from core.models import IntegrationAccount
    from core.plugins import plugin_registry
    from core.plugins.crm_adapter import Capability

    installed = plugin_registry.get_crm_adapters()
    out = []
    accounts = list(IntegrationAccount.objects.filter(company_id=company_id).order_by("provider"))
    for account in accounts:
        cls = installed.get(account.provider)
        if cls is None:
            refs = generic_refs(fp, company_id, account.provider)
            if refs:
                logger.error("[erasure] %s: no installed adapter, %d ref(s) to erase at the provider",
                             account.provider, len(refs))
                out.append(Sweep(account.provider, account.pk, refs, installed=False))
            continue
        if Capability.ERASE not in cls.capabilities:
            continue
        failed = False
        try:
            with transaction.atomic():  # a database error inside must not abort erase_party
                refs = _clean(cls(account, None).erasure_refs(fp))
        except Exception:
            logger.exception("[erasure] %s collector failed; erasing every ref of the provider instead",
                             account.provider)
            refs, failed = generic_refs(fp, company_id, account.provider), True
        if refs:
            out.append(Sweep(account.provider, account.pk, refs, collector_failed=failed))
    # An installed ERASE adapter whose account was deleted still has refs on
    # the person's rows: its provider holds what golden wrote. The row is
    # written with no account and waits (``account_missing``) where staff see it.
    have = {a.provider for a in accounts}
    for provider in sorted(_footprint_sources(fp, company_id) - have):
        cls = installed.get(provider)
        if cls is None or Capability.ERASE not in cls.capabilities:
            continue  # an importer's source (no provider to call), or not an erasing adapter
        refs = generic_refs(fp, company_id, provider)
        if refs:
            logger.error("[erasure] %s: refs to erase but no account in this company", provider)
            out.append(Sweep(provider, None, refs))
    return out


def _footprint_sources(fp, company_id) -> set:
    from core.models import ExternalRef

    q = _footprint_q(fp)
    if q is None:
        return set()
    return set(ExternalRef._base_manager.filter(q, company_id=company_id).values_list("source", flat=True))


def _clean(refs) -> tuple:
    """ProviderRefs with a clear, non-empty id, de-duplicated in order."""
    from core.plugins.crm_adapter import ProviderRef
    from core.provenance import ERASED_PREFIX

    seen, out = set(), []
    for ref in refs or ():
        if not isinstance(ref, ProviderRef):
            raise TypeError(f"erasure_refs returned {ref!r}; expected ProviderRef")
        sid = str(ref.source_id or "")
        if not sid or sid.startswith(ERASED_PREFIX):
            continue
        key = (str(ref.object_type), sid)
        if key not in seen:
            seen.add(key)
            out.append(ProviderRef(object_type=key[0], source_id=sid))
    return tuple(out)


def generic_refs(fp, company_id, provider: str) -> tuple:
    """Every ``provider`` ref of the tenant on a row of the footprint: the
    person's own rows and the CRM rows erasure scrubbed. The fallback when an
    adapter's collector fails or the adapter is gone: a superset is safe, the
    handler ignores object types it does not write."""
    from core.models import ExternalRef
    from core.plugins.crm_adapter import ProviderRef

    targets = _footprint_q(fp)
    if targets is None:
        return ()
    rows = ExternalRef._base_manager.filter(targets, company_id=company_id, source=provider).order_by(
        "object_type", "source_id").values_list("object_type", "source_id")
    return _clean(ProviderRef(object_type=ot, source_id=sid) for ot, sid in rows)


def _footprint_q(fp):
    """ExternalRefs on the footprint's rows, plus ``fp.refs`` (which holds refs
    a merge moved onto a survivor outside the tree). None when empty."""
    from django.contrib.contenttypes.models import ContentType

    from communications.models import Communication
    from crm.models import Lead, Task, Touchpoint
    from customers.models import Customer, CustomerAddress, CustomerContact
    from parties.models import Party

    addresses = set(CustomerAddress._base_manager.filter(customer_id__in=fp.customers).values_list("pk", flat=True))
    pairs = ((Party, fp.parties), (Customer, fp.customers), (CustomerAddress, addresses),
             (CustomerContact, fp.contacts), (Lead, fp.leads), (Task, getattr(fp, "tasks", ())),
             (Communication, getattr(fp, "communications", ())), (Touchpoint, getattr(fp, "touchpoints", ())))
    ct = ContentType.objects.get_for_models(*(m for m, _ids in pairs))
    targets, empty = Q(), True
    for model, ids in pairs:
        if ids:
            targets |= Q(content_type=ct[model], object_id__in=ids)
            empty = False
    if getattr(fp, "refs", None):
        targets |= Q(pk__in=fp.refs)
        empty = False
    return None if empty else targets


# -- purge (inside erase_party's transaction, after the outbox purge) ------------------


def purge(fp, company_id) -> int:
    """Each installed adapter's ``purge_parties`` hook, ERASE or not.

    ``_scrub_outbox`` has already deleted the pending and dead rows with the
    person's ``party_id``; this lets an adapter drop local queued work that
    purge cannot see. A hook that raises is logged and skipped inside its own
    savepoint: it is local cleanup, and the generic purge already ran, so it
    never fails the erasure.
    """
    from core.plugins import plugin_registry

    party_ids = sorted(fp.parties, key=str)
    if not party_ids:
        return 0
    removed = 0
    for provider, cls in sorted(plugin_registry.get_crm_adapters().items()):
        hook = getattr(cls, "purge_parties", None)
        if hook is None:
            continue
        try:
            with transaction.atomic():
                removed += int(hook(company_id, party_ids) or 0)
        except Exception:
            logger.exception("[erasure] %s purge_parties failed; the generic outbox purge already ran", provider)
    return removed


# -- enqueue (inside erase_party's transaction, after the outbox purge) ----------------


def payload_for(sweep: Sweep, erasure_id) -> dict:
    payload = {"erasure_id": str(erasure_id), "count_total": len(sweep.refs),
               # The account that wrote the records; "none" when it was already
               # gone, so no account created later is taken for it.
               "account_id": str(sweep.account_id) if sweep.account_id else NO_ACCOUNT,
               "refs": [{"object_type": r.object_type, "source_id": r.source_id} for r in sweep.refs]}
    if sweep.collector_failed:
        payload["collector_failed"] = True
    return payload


def enqueue(sweeps, *, erasure_id, party_id, company_id) -> int:
    """Write one ``<provider>.erase`` row per sweep (``idempotency_key="erase:<erasure id>"``)."""
    from core import outbox
    from core.models import OutboxEvent

    written = 0
    for sweep in sweeps:
        spec = outbox.EventSpec(
            idempotency_key=f"erase:{erasure_id}", payload=payload_for(sweep, erasure_id), event=EVENT,
            company_id=company_id, object_type="parties.party" if party_id else "", object_id=party_id,
            party_id=party_id,
        )
        name = consumer_name(sweep.provider)
        consumer = outbox.get_consumer(name) if sweep.installed else None
        if consumer is not None:
            written += bool(outbox.enqueue(consumer, spec, ignore_suppression=True))
            continue
        # No handler (the adapter is gone, or an older one registered no
        # erase consumer): the row is written anyway so it is reported.
        logger.error("[erasure] %s has no registered consumer; the row waits for an operator", name)
        now = timezone.now()
        created = OutboxEvent.objects.bulk_create([OutboxEvent(
            company_id=company_id, consumer=name, event=EVENT, idempotency_key=spec.idempotency_key,
            object_type=spec.object_type, object_id=party_id, party_id=party_id, payload=spec.payload,
            max_attempts=ERASE_MAX_ATTEMPTS, next_attempt_at=now, occurred_at=now, created_at=now,
        )], ignore_conflicts=True)
        written += OutboxEvent.objects.filter(consumer=name, idempotency_key=spec.idempotency_key,
                                              pk=created[0].pk).exists()
    return written


def erase_late(adapter, refs, *, party_id=None, reason: str) -> int:
    """A ``<provider>.erase`` row for provider records golden learns about
    only **after** an erasure committed.

    Two ways that happens: a push that was in flight while the person was
    erased (it read the row before the erasure and created or updated the
    record at the provider afterwards), and an inbound record naming a golden
    row erasure scrubbed that golden never had a ref for (a create whose answer
    was lost). The adapter calls this inside the transaction that would have
    written the ref, so the row commits with whatever tombstone it writes.
    """
    refs = _clean(refs)
    if not refs:
        return 0
    logger.warning("[erasure] %s: %d record(s) to erase found after the erasure (%s)",
                   adapter.provider_id, len(refs), reason)
    sweep = Sweep(adapter.provider_id, adapter.account.pk, refs)
    return enqueue([sweep], erasure_id=uuid.uuid4(), party_id=party_id, company_id=adapter.account.company_id)


def tombstoned(event) -> bool:
    """Whether an erasure replaced this outbox row's payload since it was claimed."""
    from core.models import OutboxEvent

    if event is None or getattr(event, "pk", None) is None:
        return False
    return OutboxEvent.objects.filter(pk=event.pk, payload={"erased": True}).exists()


# -- the consumer (process_outbox, no transaction open) --------------------------------


def erase_consumer(adapter_cls):
    """The ``<provider>.erase`` consumer every ``ERASE`` adapter gets from ``BaseCrmAdapter``."""
    from core import outbox
    from core.plugins.crm_adapter import account_handler

    return outbox.OutboxConsumer(
        name=consumer_name(adapter_cls.provider_id),
        handle=account_handler(adapter_cls, handle_erase, inactive=_inactive),
        provider=adapter_cls.provider_id, max_attempts=ERASE_MAX_ATTEMPTS, lease_seconds=ERASE_LEASE_S,
    )


def _inactive(event, account):
    """A disabled or missing account keeps its erase row waiting: never Cancel,
    and the wait spends no attempt (the row would otherwise go dead after 20
    hours of a disabled account). ``check_erasure`` reports it late after a day."""
    from core import outbox

    _refund_attempt(event)
    return outbox.Retry(after=INACTIVE_RETRY, error="account_missing" if account is None else "account_disabled")


def _refund_attempt(event) -> None:
    from django.db.models import F

    from core.models import OutboxEvent

    if OutboxEvent.objects.filter(pk=event.pk, state="running", locked_by=event.locked_by, attempts__gt=0).update(
            attempts=F("attempts") - 1):
        event.attempts -= 1  # finalize decides dead-or-retry from the in-memory count


class _LeaseLost(Exception):
    """The row's lease passed to another worker mid-sweep."""


def handle_erase(adapter, event):
    """Call ``adapter.erase(refs, done=...)``; each ref leaves the row once the provider has it.

    A retry after a partial failure (a 429 after two of five profiles) resumes
    with the refs still to do and never re-sends a finished one. Once every
    ref is done the row keeps only ``{"erased": true, "count": n}``.
    """
    from core import outbox
    from core.models import OutboxEvent
    from core.plugins.crm_adapter import ProviderRef

    payload = dict(event.payload or {})
    if payload.get("erased"):
        return outbox.Done()
    wanted = payload.get("account_id") or ""
    if wanted and wanted != str(adapter.account.pk):
        # The account that wrote the records was deleted and another created
        # (or reconnected elsewhere): this one cannot reach them, and a 404
        # here would read as "already gone". An operator decides.
        return outbox.Dead("account_changed")
    refs = [r for r in payload.get("refs") or [] if isinstance(r, dict) and r.get("source_id")]
    total = payload.get("count_total", len(refs))
    remaining = list(refs)
    mine = OutboxEvent.objects.filter(pk=event.pk, state="running", locked_by=event.locked_by)

    def done(ref):
        key = {"object_type": ref.object_type, "source_id": ref.source_id}
        if key in remaining:
            remaining.remove(key)
        # Each finished record also renews the lease, so a long sweep keeps its
        # row; a worker whose lease was taken over stops instead of racing it.
        if not mine.update(payload={**payload, "refs": remaining, "count_total": total},
                           locked_until=timezone.now() + timedelta(seconds=ERASE_LEASE_S)):
            raise _LeaseLost

    try:
        outcome = adapter.erase([ProviderRef(object_type=str(r.get("object_type") or ""),
                                             source_id=str(r["source_id"])) for r in refs], done=done)
    except _LeaseLost:
        return outbox.Retry(error="lease_lost")  # another worker holds the row now; this outcome is ignored
    if isinstance(outcome, outbox.Cancel):
        # Erasure work is never dropped quietly: a cancelled row is pruned and
        # unreported. Dead keeps it in check_erasure until an operator acts.
        return outbox.Dead(f"cancelled:{outcome.error}"[:64])
    if isinstance(outcome, (outbox.Retry, outbox.Dead)):
        return outcome
    mine.update(payload={"erased": True, "count": total})
    return outbox.Done()


# -- reporting (check_erasure) ----------------------------------------------------------


def pending(*, company_id=None, now=None, max_age=DEFAULT_MAX_AGE) -> dict:
    """Provider sweeps not finished: ``{"rows": [...], "late": n, "dead": n, "orphaned": n}``.

    A row is *late* when it has waited longer than ``max_age`` since it was
    written; ``dead`` rows need an operator (``manage.py outbox requeue``);
    *orphaned* rows have no registered consumer.
    """
    from core import outbox
    from core.models import OutboxEvent

    now = now or timezone.now()
    known = set(outbox.consumers())
    qs = OutboxEvent.objects.filter(consumer__endswith=ERASE_SUFFIX).exclude(state="done").exclude(
        state="cancelled", last_error="cancelled_by_operator")
    if company_id is not None:
        qs = qs.filter(company_id=company_id)
    rows, late, dead, orphaned = [], 0, 0, 0
    for row in qs.order_by("created_at", "pk"):
        refs = len((row.payload or {}).get("refs") or [])
        is_late = row.state not in ("dead", "cancelled") and now - row.created_at > max_age
        is_orphan = row.consumer not in known
        late += is_late
        dead += row.state in ("dead", "cancelled")
        orphaned += is_orphan
        rows.append({
            "id": str(row.pk), "consumer": row.consumer, "state": row.state, "company_id": str(row.company_id),
            "party_id": str(row.party_id or ""), "refs_left": refs, "attempts": row.attempts,
            "last_error": row.last_error, "created_at": row.created_at.isoformat(),
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else "",
            "late": is_late, "orphaned": is_orphan,
            "collector_failed": bool((row.payload or {}).get("collector_failed")),
        })
    return {"rows": rows, "late": late, "dead": dead, "orphaned": orphaned}


# -- an account golden still needs for erasure is not deleted -----------------------------

_allow_delete = threading.local()


class AccountHoldsRecords(ProtectedError):
    """Deleting this account would leave records at its provider that erasure could no longer reach."""


def account_holds_records(account) -> bool:
    """Whether the tenant still holds refs of ``account.provider`` that no erasure handled,
    or an erase row of that provider is unfinished."""
    from core.models import ExternalRef, OutboxEvent

    refs = ExternalRef._base_manager.filter(company_id=account.company_id, source=account.provider)
    if refs.count() > refs.filter(metadata__erased=True).count():
        return True
    return OutboxEvent.objects.filter(company_id=account.company_id, consumer=consumer_name(account.provider)) \
        .exclude(state__in=("done", "cancelled")).exists()


@contextmanager
def allow_account_delete():
    """Delete an account that still holds records anyway: for an operator who
    erased them at the provider by hand, or who accepts they stay there."""
    previous = getattr(_allow_delete, "on", False)
    _allow_delete.on = True
    try:
        yield
    finally:
        _allow_delete.on = previous


@receiver(pre_delete, sender="core.IntegrationAccount")
def _refuse_delete_with_records(sender, instance, **kwargs):
    """Disable an account instead of deleting it while its provider holds what
    golden wrote: erasure finds a provider's records through its account, and
    without one an uninstalled adapter's records would be unreachable and unreported."""
    if getattr(_allow_delete, "on", False) or not account_holds_records(instance):
        return
    raise AccountHoldsRecords(
        f"{instance.provider} account {instance.pk}: golden still holds records of this provider "
        "(an erasure would need the account to reach them). Disable it instead; delete it only "
        "inside core.integrations.erasure.allow_account_delete() after handling them.", {instance})
