"""Which won engagement uploads what (docs/crm/phase6-adapters.md 3.5, rules 1-5).

``build`` (inside the business transaction) only decides whether a row is
worth writing: an enabled account exists, and for a reopen, a conversion
was uploaded. Everything else is decided at send time from the current
rows (A5: a late retry sends today's truth, an erased person sends nothing):

1. the click: ``crm.touchpoints.google_click`` (the one attribution rule);
2. the window: ``closed_at - click time`` over ``click_window_days``;
3. the conversion action for the engagement's pipeline;
4. the amount: ``expected_value`` in the engagement's currency, or no value;
5. a reopen retracts; a re-win after a retraction is ``needs_review``.

The uploaded conversion is an ``ExternalRef`` (``source="google_ads"``,
``object_type="conversion"``, ``source_id`` = the order id = the engagement
number): a replayed row finds it and sends nothing.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from . import mapping, values


@dataclass(frozen=True)
class Planned:
    """What one outbox row will send: ``row`` (the JSON) or ``skip`` (a code)."""

    row: dict | None = None
    skip: str = ""
    value_unknown: bool = False
    click_kind: str = ""


def conversion_ref(engagement):
    from core.models import ExternalRef

    if not engagement.number:
        return None
    return ExternalRef._base_manager.filter(
        company_id=engagement.company_id, source=values.PROVIDER_ID, object_type=values.REF_OBJECT_TYPE,
        source_id=engagement.number,
    ).first()


def _load(event):
    from crm.models import Engagement

    return (Engagement._base_manager.select_related("pipeline", "party", "last_touch", "first_touch")
            .filter(pk=event.object_id, company_id=event.company_id).first())


def plan_upload(event, settings: dict) -> Planned:
    eng = _load(event)
    if eng is None or eng.status != "won" or eng.closed_at is None:
        return Planned(skip=values.NOT_WON)
    if eng.party is not None and eng.party.erased_at is not None:
        return Planned(skip=values.ERASED)
    if not eng.number:
        return Planned(skip=values.NO_ORDER_ID)
    ref = conversion_ref(eng)
    if ref is not None:
        if ref.metadata.get("retracted_at"):
            return Planned(skip=values.NEEDS_REVIEW)
        return Planned(skip="uploaded")  # a replay: Google already holds it
    action = mapping.action_resource(settings, eng.pipeline.key)
    if not action:
        return Planned(skip=values.PIPELINE_UNMAPPED)
    from crm.touchpoints import google_click

    click = google_click(eng)
    if click is None:
        return Planned(skip=values.NO_CLICK)
    if eng.closed_at - click.occurred_at > mapping.click_window(settings):
        return Planned(skip=values.CLICK_TOO_OLD)
    row = mapping.conversion_row(
        click_kind=click.click_id_kind, click_id=click.click_id, action=action, closed_at=eng.closed_at,
        order_id=eng.number, currency=eng.currency, value=eng.expected_value,
        consent_value=mapping.consent(settings),
    )
    return Planned(row=row, value_unknown=eng.expected_value is None, click_kind=click.click_id_kind)


def plan_retraction(event, settings: dict) -> Planned:
    eng = _load(event)
    if eng is None:
        return Planned(skip=values.NOT_UPLOADED)
    ref = conversion_ref(eng)
    if ref is None:
        return Planned(skip=values.NOT_UPLOADED)
    if ref.metadata.get("retracted_at"):
        return Planned(skip="retracted")  # a replay
    action = mapping.action_resource(settings, ref.metadata.get("pipeline", eng.pipeline.key))
    if not action:
        return Planned(skip=values.PIPELINE_UNMAPPED)
    at = datetime.datetime.fromisoformat(event.payload["at"])
    return Planned(row=mapping.retraction_row(action=action, order_id=eng.number, at=at))


def row_order(row: dict, event) -> tuple:
    """The order rows go to Google in (the handler and the dry run share it)."""
    return (row["orderId"], str(event.pk))


def chunks(indexes: list) -> list[list]:
    return [indexes[i:i + values.BATCH_LIMIT] for i in range(0, len(indexes), values.BATCH_LIMIT)]


def record_upload(event, planned: Planned) -> None:
    """The uploaded conversion's ref (in the handler's finalize step).

    A reopen that committed while the upload was in flight found no ref and
    enqueued no retraction: the engagement is re-read here, and one that is
    no longer won gets its retraction now.
    """
    from core import outbox
    from core.models import ExternalRef

    eng = _load(event)
    ref, created = ExternalRef._base_manager.get_or_create(
        company_id=eng.company_id, source=values.PROVIDER_ID, object_type=values.REF_OBJECT_TYPE,
        source_id=eng.number,
        defaults={
            "content_type": ContentType.objects.get_for_model(type(eng)), "object_id": eng.pk,
            "last_synced_at": timezone.now(),
            "metadata": {"pipeline": eng.pipeline.key, "click_id_kind": planned.click_kind,
                         **({values.VALUE_UNKNOWN: True} if planned.value_unknown else {})},
        },
    )
    if created and eng.status != "won":
        at = timezone.now()
        outbox.enqueue(values.CONSUMER, outbox.EventSpec(
            idempotency_key=f"retract:late:{ref.pk}", event=values.RETRACT,
            payload={"at": at.isoformat()}, company_id=eng.company_id, object_type="crm.engagement",
            object_id=eng.pk, occurred_at=at,
        ), ignore_suppression=True)


def record_retraction(event) -> None:
    eng = _load(event)
    ref = conversion_ref(eng)
    if ref is not None:
        ref.metadata = {**ref.metadata, "retracted_at": event.payload["at"]}
        ref.last_synced_at = timezone.now()
        ref.save(update_fields=["metadata", "last_synced_at", "updated_at"])
