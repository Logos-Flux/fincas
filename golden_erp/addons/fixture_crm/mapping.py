"""Fixture provider objects <-> golden values. Pure functions, no database.

A real adapter keeps its field mapping here so ``tests/test_mapping.py`` can
cover it without a tenant.
"""

from __future__ import annotations

from datetime import datetime

from core.plugins.payroll import AdapterError

from .values import DOCS, MESSAGE_TYPES


def parse_instant(value) -> datetime:
    if not isinstance(value, str):
        raise AdapterError(f"Fixture message has no occurred_at — cause: a malformed payload. "
                           f"Fix: send an ISO 8601 instant. {DOCS}")
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AdapterError(f"Fixture occurred_at {value[:40]!r} is not ISO 8601 — cause: a malformed "
                           f"payload. Fix: send an ISO 8601 instant. {DOCS}") from None
    if when.tzinfo is None:
        raise AdapterError(f"Fixture occurred_at has no offset — cause: a naive time. "
                           f"Fix: send UTC with Z. {DOCS}")
    return when


def message_fields(payload: dict) -> dict | None:
    """The ``communications.ingest.record`` arguments for a message, or None when not a message."""
    if not isinstance(payload, dict):
        return None
    direction = MESSAGE_TYPES.get(payload.get("type"))
    if direction is None or not isinstance(payload.get("id"), str) or not payload["id"]:
        return None
    recipients = payload.get("to") or []
    if not isinstance(recipients, list):
        recipients = []
    participants = [("from", str(payload.get("from") or ""))] + [("to", str(a)) for a in recipients]
    return {
        "external_id": payload["id"],
        "direction": direction,
        "occurred_at": parse_instant(payload.get("occurred_at")),
        "subject": str(payload.get("subject") or "")[:255],
        "body_text": str(payload.get("body") or "")[:100_000],
        "thread_key": f"fixture:{payload['thread']}" if isinstance(payload.get("thread"), str) else "",
        "participants": [(role, address) for role, address in participants if address],
    }


def contact_body(party, email: str) -> dict:
    """The fixture provider's contact for a party: golden id (the dedupe key), name, email."""
    return {"golden_id": str(party.pk), "name": party.display_name, "email": email}
