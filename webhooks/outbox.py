"""Golden's outbound webhooks on the outbox (docs/crm/phase6-adapters.md 1.6).

Two consumers, registered in ``WebhooksConfig.ready()``:

- ``webhooks`` hears the 19 lifecycle signals that have a webhook event.
  ``build`` runs in the emitting transaction and pins the event's latest
  payload (``webhooks.events``) into the row, so a delivery says what was true
  when the event happened. It writes nothing while
  ``WEBHOOKS_DELIVER_ON_SIGNALS`` is off. Its handler expands the row into one
  ``webhooks.deliver`` row per matching subscription (company rules of
  ``matching_subscriptions``), so a dead endpoint retries alone and never
  delays another.
- ``webhooks.deliver`` makes **one** HTTP attempt per run (10 s timeout, no
  sleep): 2xx done, 4xx dead (fail fast), 5xx or a transport error retried by
  the outbox's backoff. The row id is ``X-Golden-Delivery-Id``, stable across
  retries; every attempt writes its own ``WebhookDeliveryLog``.

The idempotency key of a ``webhooks`` row is a fresh uuid per emit (each emit
is one delivery: an order re-entering a status is a real second event);
subscribers drop repeats by ``X-Golden-Delivery-Id``. A ``webhooks.deliver``
key is ``<parent row id>:<subscription id>``, so re-running an expansion that
crashed half-way writes nothing twice.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from django.conf import settings

from core import outbox

from . import events

logger = logging.getLogger("golden_erp.webhooks")

CONSUMER = "webhooks"
DELIVER = "webhooks.deliver"
OWNER = "webhooks (core)"
DELIVERY_TIMEOUT_S = 10.0


def enabled() -> bool:
    """Signal-driven delivery is OFF unless ``WEBHOOKS_DELIVER_ON_SIGNALS`` is
    set. Delivery runs from the outbox now, so turning it on costs a request
    nothing; it stays off so a deployment whose subscriptions never received
    events starts them deliberately."""
    return bool(getattr(settings, "WEBHOOKS_DELIVER_ON_SIGNALS", False))


def _company(value) -> Any:
    return getattr(value, "pk", value)


def _party(party) -> Any:
    return getattr(party, "pk", party)


# signal name -> (event name, build(kwargs) -> (payload, company, party_id, object) or None)
def _order_placed(kw):
    order = kw.get("order")
    if not order:
        return None
    return events.order_placed_payload(order), getattr(order, "company_id", None), None, order


def _order_status_changed(kw):
    order = kw.get("order")
    if not order:
        return None
    payload = events.order_status_changed_payload(order, kw.get("old_status", ""), kw.get("new_status", ""))
    return payload, getattr(order, "company_id", None), None, order


def _payment_completed(kw):
    payment = kw.get("payment")
    if not payment:
        return None
    return events.payment_completed_payload(payment), getattr(payment, "company_id", None), None, payment


def _inventory_adjusted(kw):
    stock = kw.get("warehouse_stock")
    if not stock:
        return None
    warehouse = getattr(stock, "warehouse", None)
    payload = events.inventory_adjusted_payload(stock, kw.get("delta", 0), kw.get("reason", ""))
    return payload, getattr(warehouse, "company_id", None), None, stock


def _customer_registered(kw):
    customer = kw.get("customer")
    if not customer:
        return None
    # The latest schema (v2); the deliver row cuts it to v1 for "@1" subscribers.
    # It carries an email and a name, so the row names the party for erasure.
    payload = events.customer_registered_payload(customer)
    return payload, getattr(customer, "company_id", None), getattr(customer, "party_id", None), customer


def _party_created(kw):
    party = kw.get("party")
    if party is None:
        return None
    return events.party_payload(party), party.company_id, party.pk, party


def _party_updated(kw):
    party = kw.get("party")
    if party is None:
        return None
    payload = events.party_updated_payload(party, kw.get("update_fields", ()))
    return payload, party.company_id, party.pk, party


def _party_merge(others_key, list_key):
    def build(kw):
        survivor = kw.get("survivor")
        if survivor is None:
            return None
        payload = events.party_merge_payload(survivor, kw.get(list_key, ()), kw.get("merge_id"),
                                             others_key=others_key)
        return payload, getattr(survivor, "company_id", None), survivor.pk, survivor
    return build


def _party_erased(kw):
    party_id, company_id = kw.get("party_id"), kw.get("company_id")
    if party_id is None:
        return None
    if company_id is None:
        from parties.models import Party

        # Fallback: the scrubbed row keeps its tenant. Without one the event
        # reaches global subscriptions only (matching_subscriptions fails closed).
        company_id = Party._base_manager.filter(pk=party_id).values_list("company_id", flat=True).first()
    # Ids only: the row keeps no party_id, so erasure (4.2) never touches it.
    return events.party_erased_payload(party_id, company_id), company_id, None, None


def _engagement(builder):
    def build(kw):
        engagement = kw.get("engagement")
        if engagement is None:
            return None
        payload = builder(engagement, kw)
        if payload is None:
            return None
        return payload, engagement.company_id, None, engagement
    return build


def _stage_changed(engagement, kw):
    change = kw.get("change")
    if change is None:
        return None
    return events.engagement_stage_changed_payload(engagement, kw.get("from_stage"), kw.get("to_stage"), change)


def _task(kw):
    task = kw.get("task")
    if task is None:
        return None
    return events.task_payload(task), task.company_id, None, task


def _communication(kw):
    communication = kw.get("communication")
    if communication is None:
        return None
    return events.communication_logged_payload(communication), communication.company_id, None, communication


SIGNAL_EVENTS: dict[str, tuple[str, Callable]] = {
    "order_placed": ("order.placed", _order_placed),
    "order_status_changed": ("order.status_changed", _order_status_changed),
    "payment_completed": ("payment.completed", _payment_completed),
    "inventory_adjusted": ("inventory.adjusted", _inventory_adjusted),
    "customer_registered": ("customer.registered", _customer_registered),
    "party_created": ("party.created", _party_created),
    "party_updated": ("party.updated", _party_updated),
    "party_merged": ("party.merged", _party_merge("merged_party_ids", "merged")),
    "party_unmerged": ("party.unmerged", _party_merge("restored_party_ids", "restored")),
    "party_erased": ("party.erased", _party_erased),
    "engagement_created": ("engagement.created", _engagement(lambda e, kw: events.engagement_payload(e))),
    "engagement_stage_changed": ("engagement.stage_changed", _engagement(_stage_changed)),
    "engagement_won": ("engagement.won", _engagement(
        lambda e, kw: events.engagement_closed_payload(e, kw.get("change")))),
    "engagement_lost": ("engagement.lost", _engagement(
        lambda e, kw: events.engagement_closed_payload(e, kw.get("change"), loss_reason=kw.get("loss_reason", "")))),
    "engagement_reopened": ("engagement.reopened", _engagement(
        lambda e, kw: events.engagement_reopened_payload(e, kw.get("change"), kw.get("previous_status", "")))),
    # task_assigned and task_reopened have no webhook event (phase 3 spec 5.1).
    "task_created": ("task.created", _task),
    "task_completed": ("task.completed", _task),
    "task_due": ("task.due", _task),
    "communication_logged": ("communication.logged", _communication),
}


def _ordered(payload: dict) -> dict | None:
    """The pinned ``data`` in the order it was built. Postgres ``jsonb`` does
    not keep key order, and a pinned ``@1`` subscriber must receive the same
    bytes as before, so every row stores the key order next to the data."""
    data = payload.get("data")
    if data is None:
        return None
    keys = payload.get("keys") or []
    ordered = {key: data[key] for key in keys if key in data}
    ordered.update((key, value) for key, value in data.items() if key not in ordered)
    return ordered


def _object_type(obj) -> str:
    label = getattr(getattr(obj, "_meta", None), "label_lower", "")
    return label if isinstance(label, str) else ""


def build(signal_name: str, sender, obj, kwargs) -> list[outbox.EventSpec]:
    if not enabled():
        return []
    entry = SIGNAL_EVENTS.get(signal_name)
    if entry is None:
        return []
    event_type, builder = entry
    built = builder(kwargs)
    if built is None:
        return []
    payload, company, party_id, about = built
    return [outbox.EventSpec(
        idempotency_key=str(uuid.uuid4()),
        event=event_type,
        payload={"event": event_type, "data": payload, "keys": list(payload)},
        company_id=_company(company),
        object_type=_object_type(about),
        object_id=getattr(about, "pk", None),
        party_id=_party(party_id),
    )]


def expand(event) -> Any:
    """``webhooks`` handler: one ``webhooks.deliver`` row per subscription.

    The parent row is re-read under a row lock in the same short transaction
    that writes the children, so an erasure that scrubs it (4.2) either
    commits first (the expansion sees ``{"erased": true}`` and cancels) or
    waits for the children and then deletes them as pending rows of the
    person.
    """
    from django.db import transaction

    from core.models import OutboxEvent

    from .services import matching_subscriptions

    with transaction.atomic():
        current = OutboxEvent.objects.select_for_update().filter(pk=event.pk).values_list("payload", flat=True).first()
        payload = current if current is not None else event.payload
        event_type = payload.get("event") or event.event
        data = _ordered(payload)
        if data is None:  # scrubbed by erasure before it ran
            return outbox.Cancel("erased")
        specs = [
            outbox.EventSpec(
                idempotency_key=f"{event.pk}:{sub.pk}",
                event=event_type,
                payload=_pack(event_type, sub, version, events.payload_for(event_type, data, version)),
                company_id=event.company_id,
                object_type=event.object_type,
                object_id=event.object_id,
                party_id=event.party_id,
                occurred_at=event.occurred_at,
            )
            for sub, version in matching_subscriptions(event_type, event.company_id)
        ]
        # Not an announcement: written even if some caller suppressed signals.
        outbox.enqueue_many(DELIVER, specs, ignore_suppression=True)
    return outbox.Done()


def _pack(event_type, sub, version, data) -> dict:
    return {"event": event_type, "version": version, "subscription_id": str(sub.pk),
            "data": data, "keys": list(data)}


def deliver(event) -> Any:
    """``webhooks.deliver`` handler: one attempt, no sleep."""
    from . import services
    from .models import WebhookSubscription

    data = _ordered(event.payload)
    if data is None:
        return outbox.Cancel("erased")
    sub = WebhookSubscription.objects.filter(pk=event.payload.get("subscription_id")).first()
    if sub is None:
        return outbox.Cancel("subscription_deleted")
    if not sub.is_active:
        return outbox.Cancel("subscription_inactive")
    event_type = event.payload.get("event") or event.event
    # Re-checked at every attempt (retries span hours): a subscription moved
    # to another tenant, or no longer listening, receives nothing.
    if sub.company_id is not None and sub.company_id != event.company_id:
        return outbox.Cancel("subscription_tenant_changed")
    try:
        listening = sub.event_version(event_type) is not None
    except Exception:  # noqa: BLE001 - corrupt subscribed_events
        listening = False
    if not listening:
        return outbox.Cancel("subscription_unsubscribed")
    log = services.deliver_webhook(
        sub, event_type, data,
        timeout_s=DELIVERY_TIMEOUT_S, max_retries=0, delivery_id=event.pk,
    )
    if getattr(log, "success", False):
        return outbox.Done()
    code = getattr(log, "response_status_code", None)
    if isinstance(code, int) and 400 <= code < 500:
        return outbox.Dead(f"http_{code}")  # today's fail-fast rule
    return outbox.Retry(error=f"http_{code}" if isinstance(code, int) else "transport_error")


def register() -> None:
    outbox.register(outbox.OutboxConsumer(
        name=CONSUMER, signals=tuple(SIGNAL_EVENTS), build=build, handle=expand, enabled=enabled,
    ), OWNER)
    # The lease outlasts the 10 s request timeout by far; a server trickling
    # its answer past it causes a repeat with the same X-Golden-Delivery-Id.
    outbox.register(outbox.OutboxConsumer(name=DELIVER, handle=deliver, lease_seconds=120), OWNER)
