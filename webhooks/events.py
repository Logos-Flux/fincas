"""Webhook event catalogue: payload builders and schema versions.

Every event golden delivers from a lifecycle signal is listed in
:data:`EVENTS`. A payload builder returns the event's **latest** schema;
:func:`payload_for` derives an older schema from it for a subscriber pinned
to one (``"customer.registered@1"`` in ``subscribed_events``).

Rules (docs/webhooks.md section 1.1):

- A new version only ever **adds** keys. An older version is the latest
  payload filtered to that version's keys, in that version's order, so a
  pinned subscriber receives the same ``data`` bytes it did before.
- An entry without ``@<n>`` (a bare name, ``*`` or ``prefix.*``) gets the
  latest version, with one exception for the transition release: an
  *existing* subscription (created before ``WEBHOOKS_VERSIONING_CUTOFF``)
  keeps ``legacy_default`` until that field is cleared.
- An explicit pin wins over a bare or wildcard entry. Two pins for the same
  event deliver once, at the lower version (the serializer refuses this).
- A version listed in ``deprecated`` still delivers, and logs a warning on
  every delivery.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.dateparse import parse_datetime

logger = logging.getLogger("golden_erp.webhooks")

# Subscriptions created before this instant are "existing" for the 0.3
# transition (see ``EventSchema.legacy_default``). The default is the day
# customer.registered v2 merged; a deployment that upgrades later sets
# WEBHOOKS_VERSIONING_CUTOFF to its own upgrade time (ISO 8601, with zone).
DEFAULT_VERSIONING_CUTOFF = dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class EventSchema:
    """One webhook event and the payload versions golden still serves."""

    latest: int = 1
    # Older versions: version -> payload keys, in delivery order.
    versions: dict[int, tuple[str, ...]] = field(default_factory=dict)
    # Delivered with a warning logged on every delivery.
    deprecated: frozenset[int] = frozenset()
    # The version an existing subscription gets when it pins none (None: latest).
    legacy_default: int | None = None

    def supported(self) -> tuple[int, ...]:
        return tuple(sorted({*self.versions, self.latest}))


CUSTOMER_REGISTERED_V1_KEYS = ("customer_id", "email", "name", "customer_type")

EVENTS: dict[str, EventSchema] = {
    "order.placed": EventSchema(),
    "order.status_changed": EventSchema(),
    "payment.completed": EventSchema(),
    "inventory.adjusted": EventSchema(),
    # v2 (0.3.0) adds schema_version, party_id, account_holder_party_id,
    # company_id. Timeline: 0.3 existing subscriptions keep v1 unless they
    # pin; 0.4 @1 is deprecated (logged) and legacy_default goes; 0.5 v1 is
    # removed.
    "customer.registered": EventSchema(
        latest=2, versions={1: CUSTOMER_REGISTERED_V1_KEYS}, legacy_default=1,
    ),
    "party.created": EventSchema(),
    "party.updated": EventSchema(),
    "party.merged": EventSchema(),
    "party.unmerged": EventSchema(),
    "party.erased": EventSchema(),
    # CRM Phase 3 (docs/crm/phase3-engagement.md 5.1), new in 0.4: ids, keys
    # and timestamps only, never titles, subjects, bodies or addresses.
    "engagement.created": EventSchema(),
    "engagement.stage_changed": EventSchema(),
    "engagement.won": EventSchema(),
    "engagement.lost": EventSchema(),
    "engagement.reopened": EventSchema(),
    "task.created": EventSchema(),
    "task.completed": EventSchema(),
    "task.due": EventSchema(),
    "communication.logged": EventSchema(),
}

_DEFAULT_SCHEMA = EventSchema()


def schema(event_name: str) -> EventSchema:
    return EVENTS.get(event_name, _DEFAULT_SCHEMA)


def parse_entry(entry: Any) -> tuple[str, int | None] | None:
    """``"name@2"`` -> ``("name", 2)``; ``"name"`` -> ``("name", None)``.

    None for an entry that is not a string or has a malformed ``@`` suffix.
    """
    if not isinstance(entry, str):
        return None
    name, sep, version = entry.partition("@")
    if not sep:
        return name, None
    # isdecimal() alone accepts non-ASCII digits that int() then rejects.
    if not (version.isascii() and version.isdecimal()) or "*" in name or not name:
        return None
    return name, int(version)


def versioning_cutoff() -> dt.datetime:
    value = getattr(settings, "WEBHOOKS_VERSIONING_CUTOFF", None)
    if value is None:
        return DEFAULT_VERSIONING_CUTOFF
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            raise ImproperlyConfigured(f"WEBHOOKS_VERSIONING_CUTOFF {value!r} is not an ISO 8601 datetime")
        value = parsed
    if not isinstance(value, dt.datetime):
        raise ImproperlyConfigured("WEBHOOKS_VERSIONING_CUTOFF must be an ISO 8601 string or a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def default_version(event_name: str, created_at: dt.datetime | None) -> int:
    """The version an unpinned entry receives."""
    spec = schema(event_name)
    if spec.legacy_default is not None and created_at is not None and created_at < versioning_cutoff():
        return spec.legacy_default
    return spec.latest


def resolve_version(entries: Iterable[Any], event_name: str, created_at: dt.datetime | None = None) -> int | None:
    """Which version of ``event_name`` these subscription entries receive; None if none."""
    entries = list(entries or [])
    spec = schema(event_name)
    pins = []
    matched = False
    for entry in entries:
        parsed = parse_entry(entry)
        if parsed is None:
            continue
        name, version = parsed
        if version is not None:
            if name != event_name:
                continue
            if version in spec.supported():
                pins.append(version)
            else:
                logger.warning("Webhook entry %r names an unsupported version; ignored", entry)
            continue
        if name == "*" or name == event_name or (name.endswith(".*") and event_name.startswith(name[:-1])):
            matched = True
    if pins:
        return min(pins)
    if matched:
        return default_version(event_name, created_at)
    return None


def payload_for(event_name: str, payload: dict[str, Any], version: int) -> dict[str, Any]:
    """The ``data`` a subscriber on ``version`` receives (``payload`` is the latest)."""
    spec = schema(event_name)
    if version == spec.latest or version not in spec.versions:
        return payload
    return {key: payload[key] for key in spec.versions[version] if key in payload}


def validate_entries(entries: Any) -> list[str]:
    """Error messages for a ``subscribed_events`` value (empty when valid)."""
    if not isinstance(entries, list):
        return ["Must be a list of event names."]
    errors = []
    pinned: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, str):
            errors.append(f"{entry!r}: event names are strings.")
            continue
        if "@" not in entry:
            continue
        parsed = parse_entry(entry)
        if parsed is None:
            errors.append(f"{entry!r}: write a version as <event>@<number>; wildcards cannot be pinned.")
            continue
        name, version = parsed
        if name not in EVENTS:
            errors.append(f"{entry!r}: unknown event {name!r}.")
            continue
        supported = EVENTS[name].supported()
        if version not in supported:
            errors.append(f"{entry!r}: {name} serves versions {', '.join(map(str, supported))}.")
            continue
        if name in pinned and pinned[name] != version:
            errors.append(f"{entry!r}: {name} is already pinned to @{pinned[name]}.")
        pinned[name] = version
    return errors


# --- payload builders (latest version) ---------------------------------------


def _id(value) -> str | None:
    value = getattr(value, "pk", value)
    return None if value is None else str(value)


def order_placed_payload(order) -> dict[str, Any]:
    return {
        "order_id": str(getattr(order, "id", "")),
        "order_number": getattr(order, "order_number", ""),
        "total_amount": str(getattr(order, "total_amount", "0.00")),
        "status": getattr(order, "status", "pending"),
    }


def order_status_changed_payload(order, old_status, new_status) -> dict[str, Any]:
    return {
        "order_id": str(getattr(order, "id", "")),
        "order_number": getattr(order, "order_number", ""),
        "old_status": old_status,
        "new_status": new_status,
    }


def payment_completed_payload(payment) -> dict[str, Any]:
    return {
        "payment_id": str(getattr(payment, "id", "")),
        "payment_number": getattr(payment, "payment_number", ""),
        "amount": str(getattr(payment, "amount", "0.00")),
        "payment_method": getattr(payment, "payment_method", ""),
        "status": getattr(payment, "status", "settled"),
    }


def inventory_adjusted_payload(warehouse_stock, delta, reason) -> dict[str, Any]:
    return {
        "sku": getattr(getattr(warehouse_stock, "variant", None), "sku", ""),
        "warehouse_code": getattr(getattr(warehouse_stock, "warehouse", None), "warehouse_code", ""),
        "delta": float(delta),
        "current_quantity": float(getattr(warehouse_stock, "quantity", 0)),
        "reason": reason,
    }


def customer_registered_payload(customer) -> dict[str, Any]:
    """customer.registered v2. The v1 keys come first, in v1 order."""
    return {
        "customer_id": str(getattr(customer, "id", "")),
        "email": getattr(customer, "email", ""),
        "name": getattr(customer, "full_name", ""),
        "customer_type": getattr(customer, "customer_type", "individual"),
        "schema_version": 2,
        "party_id": _id(getattr(customer, "party_id", None)),
        "account_holder_party_id": _id(getattr(customer, "account_holder_id", None)),
        "company_id": _id(getattr(customer, "company_id", None)),
    }


def party_payload(party) -> dict[str, Any]:
    """party.created / party.updated v1."""
    return {
        "schema_version": 1,
        "party_id": _id(party.pk),
        "company_id": _id(party.company_id),
        "kind": party.kind,
        "display_name": party.display_name,
        "is_active": bool(party.is_active),
        "merged_into_party_id": _id(party.merged_into_id),
        "updated_at": party.updated_at.isoformat() if party.updated_at else None,
    }


def party_updated_payload(party, update_fields) -> dict[str, Any]:
    """``updated_fields``: the columns the save wrote, not a diff of what changed."""
    return {**party_payload(party), "updated_fields": sorted(update_fields or [])}


def party_merge_payload(survivor, others, merge_id, *, others_key: str) -> dict[str, Any]:
    """party.merged (``merged_party_ids``) / party.unmerged (``restored_party_ids``) v1."""
    return {
        "schema_version": 1,
        "survivor_party_id": _id(survivor),
        others_key: [_id(p) for p in (others or [])],
        "merge_id": _id(merge_id),
        "company_id": _id(getattr(survivor, "company_id", None)),
    }


def party_erased_payload(party_id, company_id) -> dict[str, Any]:
    return {"schema_version": 1, "party_id": _id(party_id), "company_id": _id(company_id)}


# --- CRM Phase 3 (docs/crm/phase3-engagement.md 5.1) --------------------------
# v1 payloads. Ids, registry keys and timestamps only: an engagement's title
# names its party, a task's title and a communication's subject are free text,
# so none of them leave the server here.


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _decimal(value) -> str | None:
    return None if value is None else str(value)


def _key(row) -> str | None:
    return getattr(row, "key", None) if row is not None else None


def engagement_payload(engagement) -> dict[str, Any]:
    """The keys every ``engagement.*`` event carries, in this order."""
    return {
        "schema_version": 1,
        "engagement_id": _id(engagement.pk),
        "company_id": _id(engagement.company_id),
        "number": engagement.number,
        "pipeline_key": _key(engagement.pipeline if engagement.pipeline_id else None),
        "stage_key": _key(engagement.stage if engagement.stage_id else None),
        "status": engagement.status,
        "party_id": _id(engagement.party_id),
        "owner_id": _id(engagement.owner_id),
        "source": engagement.source,
        "origin": engagement.origin,
        "expected_value": _decimal(engagement.expected_value),
        "currency": engagement.currency,
        "expected_close_date": _iso(engagement.expected_close_date),
        "created_at": _iso(engagement.created_at),
    }


def _change_keys(change) -> dict[str, Any]:
    return {
        "change_id": _id(getattr(change, "pk", None)),
        "changed_at": _iso(getattr(change, "changed_at", None)),
        "change_source": getattr(change, "source", None),
        "changed_by_id": _id(getattr(change, "changed_by_id", None)),
    }


def engagement_stage_changed_payload(engagement, from_stage, to_stage, change) -> dict[str, Any]:
    return {
        **engagement_payload(engagement),
        "from_pipeline_key": _key(change.from_pipeline if change.from_pipeline_id else None),
        "from_stage_key": _key(from_stage),
        "to_stage_key": _key(to_stage),
        **_change_keys(change),
    }


def engagement_closed_payload(engagement, change, **extra) -> dict[str, Any]:
    """engagement.won (no extra) / engagement.lost (``loss_reason``)."""
    return {**engagement_payload(engagement), "closed_at": _iso(engagement.closed_at),
            **extra, **_change_keys(change)}


def engagement_reopened_payload(engagement, change, previous_status) -> dict[str, Any]:
    return {**engagement_payload(engagement), "previous_status": previous_status,
            **_change_keys(change)}


def task_payload(task) -> dict[str, Any]:
    """task.created / task.completed / task.due v1."""
    return {
        "schema_version": 1,
        "task_id": _id(task.pk),
        "company_id": _id(task.company_id),
        "engagement_id": _id(task.engagement_id),
        "party_id": _id(task.party_id),
        "kind": task.kind,
        "status": task.status,
        "priority": task.priority,
        "assignee_id": _id(task.assignee_id),
        "due_at": _iso(task.due_at),
        "all_day": bool(task.all_day),
        "due_tz": task.due_tz,
        "completed_at": _iso(task.completed_at),
        "source": task.source,
        "created_at": _iso(task.created_at),
    }


def communication_logged_payload(communication) -> dict[str, Any]:
    """communication.logged v1: no subject, body, summary or address."""
    return {
        "schema_version": 1,
        "communication_id": _id(communication.pk),
        "company_id": _id(communication.company_id),
        "kind": communication.kind,
        "direction": communication.direction,
        "status": communication.status,
        "provider": communication.provider,
        "party_id": _id(communication.party_id),
        "engagement_id": _id(communication.engagement_id),
        "occurred_at": _iso(communication.occurred_at),
    }
