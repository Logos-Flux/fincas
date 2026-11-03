"""Klaviyo resources <-> golden values. Pure functions: no database, no network.

Shapes follow the pinned revision (``values.API_REVISION``); the request
bodies are JSON:API documents. An inbound item is the envelope
``{schema_version, provider, payload}`` whose payload is one event with its
metric and profile resolved from the page's ``included`` list
(:func:`page_items`), so a polled page and any later webhook body reach
``ingest`` in the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import values

# --- outbound bodies -----------------------------------------------------------


def profile_attributes(party, email: str) -> dict:
    """The profile golden upserts: email, names, E.164 phone, golden party id.

    Nothing else about the person leaves golden (no addresses, no custom fields).
    """
    attributes = {"email": email, "external_id": str(party.pk)}
    if party.given_name:
        attributes["first_name"] = party.given_name
    if party.family_name:
        attributes["last_name"] = party.family_name
    if party.primary_phone_e164:
        attributes["phone_number"] = party.primary_phone_e164
    return attributes


def profile_import_body(attributes: dict, profile_id: str = "") -> dict:
    data = {"type": "profile", "attributes": dict(attributes)}
    if profile_id:
        data["id"] = profile_id
    return {"data": data}


def _profile_ref(profile_id: str = "", email: str = "", subscriptions: dict | None = None) -> dict:
    attributes: dict[str, Any] = {}
    if email:
        attributes["email"] = email
    if subscriptions is not None:
        attributes["subscriptions"] = subscriptions
    data: dict[str, Any] = {"type": "profile", "attributes": attributes}
    if profile_id:
        data["id"] = profile_id
    return data


def subscribe_body(list_id: str, *, email: str, profile_id: str, consented_at: str) -> dict:
    """Subscribe one profile to email marketing on ``list_id``, with golden's consent time."""
    marketing = {"consent": "SUBSCRIBED"}
    attributes: dict[str, Any] = {"custom_source": "golden"}
    if consented_at:
        # Golden's own consent time; the API takes a past time only as a historical import.
        marketing["consented_at"] = consented_at
        attributes["historical_import"] = True
    attributes["profiles"] = {"data": [_profile_ref(profile_id, email, {"email": {"marketing": marketing}})]}
    return {"data": {
        "type": "profile-subscription-bulk-create-job",
        "attributes": attributes,
        "relationships": {"list": {"data": {"type": "list", "id": list_id}}},
    }}


def unsubscribe_body(*, email: str, profile_id: str) -> dict:
    """Withdraw email marketing for one profile (every list)."""
    return {"data": {
        "type": "profile-subscription-bulk-delete-job",
        "attributes": {"profiles": {"data": [_profile_ref(profile_id, email)]}},
    }}


def suppress_body(*, email: str) -> dict:
    """Add one address to Klaviyo's suppressions (golden's hard bounce or complaint)."""
    return {"data": {
        "type": "profile-suppression-bulk-create-job",
        "attributes": {"profiles": {"data": [_profile_ref(email=email)]}},
    }}


def list_members_body(profile_id: str) -> dict:
    """One profile, to add to or take off a segment list (3b): membership only, consent untouched."""
    return {"data": [{"type": "profile", "id": profile_id}]}


def deletion_body(profile_id: str) -> dict:
    """A data-privacy deletion request for one profile (erasure, section 4)."""
    return {"data": {
        "type": "data-privacy-deletion-job",
        "attributes": {"profile": {"data": {"type": "profile", "id": profile_id}}},
    }}


@dataclass(frozen=True)
class Refusal:
    """What a profile's ``subscriptions.email.marketing`` says against marketing."""

    withdraw: bool = False
    reason: str = ""             # golden Suppression.reason, or ""
    when: datetime | None = None  # Klaviyo's time for the newest refusal, when it gives one


def marketing_refusal(body: Any) -> Refusal:
    """The refusal in a profile resource, if any.

    Only refusals are read: an unsubscribe is a withdrawal plus an opt-out;
    a suppression maps through ``values.SUPPRESSION_REASONS`` (a complaint or
    a user suppression is also a withdrawal). A Klaviyo ``SUBSCRIBED`` means
    nothing to golden and returns an empty ``Refusal``.
    """
    from parties.consent import SUPPRESSION_RANK

    data = body.get("data") if isinstance(body, dict) else None
    attributes = data.get("attributes") if isinstance(data, dict) else None
    subs = attributes.get("subscriptions") if isinstance(attributes, dict) else None
    email = subs.get("email") if isinstance(subs, dict) else None
    marketing = email.get("marketing") if isinstance(email, dict) else None
    if not isinstance(marketing, dict):
        return Refusal()
    withdraw = marketing.get("consent") == values.CONSENT_UNSUBSCRIBED
    reason = "opt_out" if withdraw else ""
    times = [parse_time(marketing.get("timestamp"))] if withdraw else []
    for entry in marketing.get("suppression") or []:
        code = entry.get("reason") if isinstance(entry, dict) else None
        mapped = values.SUPPRESSION_REASONS.get(code) if isinstance(code, str) else None
        if mapped is None:
            continue
        times.append(parse_time(entry.get("timestamp")))
        if mapped in ("complaint", "opt_out"):
            withdraw = True
        if SUPPRESSION_RANK.get(mapped, 0) > SUPPRESSION_RANK.get(reason, 0):
            reason = mapped
    known = [t for t in times if t is not None]
    when = max(known) if known and len(known) == len(times) else None  # any unknown time: unknown
    return Refusal(withdraw=withdraw, reason=reason, when=when)


def profile_id_of(body: Any) -> str:
    """The profile id in a profile-import answer, or ``""``."""
    data = body.get("data") if isinstance(body, dict) else None
    pid = data.get("id") if isinstance(data, dict) else None
    return pid if isinstance(pid, str) else ""


# --- inbound -------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One Klaviyo event, as golden reads it."""

    event_id: str
    metric: str
    occurred_at: datetime
    profile_id: str
    email: str
    properties: dict = field(default_factory=dict)

    @property
    def message_id(self) -> str:
        value = self.properties.get(values.PROP_MESSAGE)
        return value if isinstance(value, str) else ""

    @property
    def subject(self) -> str:
        value = self.properties.get(values.PROP_SUBJECT)
        return value[:255] if isinstance(value, str) else ""

    @property
    def hard_bounce(self) -> bool:
        value = self.properties.get(values.PROP_BOUNCE_TYPE)
        return isinstance(value, str) and value.strip().lower() == values.HARD_BOUNCE


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def parse_event(payload: Any) -> Event | None:
    """The :class:`Event` in an envelope payload, or None when it is not one golden reads."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    metric = payload.get("metric")
    profile = payload.get("profile")
    if not isinstance(event, dict) or not isinstance(metric, dict):
        return None
    event_id = event.get("id")
    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    occurred = parse_time(attributes.get("datetime"))
    name = (metric.get("attributes") or {}).get("name") if isinstance(metric.get("attributes"), dict) else None
    if not isinstance(event_id, str) or not event_id or occurred is None or not isinstance(name, str):
        return None
    profile = profile if isinstance(profile, dict) else {}
    profile_id = profile.get("id") if isinstance(profile.get("id"), str) else ""
    p_attrs = profile.get("attributes") if isinstance(profile.get("attributes"), dict) else {}
    email = p_attrs.get("email") if isinstance(p_attrs.get("email"), str) else ""
    props = attributes.get("event_properties")
    return Event(event_id=event_id, metric=name, occurred_at=occurred, profile_id=profile_id,
                 email=email.strip(), properties=props if isinstance(props, dict) else {})


def page_items(body: Any) -> tuple[list[dict], str]:
    """``(payloads, next page cursor)`` of one events page.

    Each payload is ``{"event", "metric", "profile"}`` with the related
    resources looked up in ``included``. The cursor is the ``page[cursor]``
    parameter of ``links.next`` (``""`` on the last page).
    """
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise ValueError("events page has no data list")
    included = {}
    for resource in body.get("included") or []:
        if isinstance(resource, dict) and isinstance(resource.get("id"), str):
            included[(resource.get("type"), resource["id"])] = resource
    items = []
    for event in body["data"]:
        if not isinstance(event, dict):
            continue
        rel = event.get("relationships") if isinstance(event.get("relationships"), dict) else {}
        items.append({
            "event": {k: v for k, v in event.items() if k != "relationships"},
            "metric": included.get(("metric", _rel_id(rel, "metric")), {}),
            "profile": included.get(("profile", _rel_id(rel, "profile")), {}),
        })
    links = body.get("links") if isinstance(body.get("links"), dict) else {}
    return items, next_cursor(links.get("next"))


def _rel_id(relationships: dict, name: str) -> str:
    data = (relationships.get(name) or {}).get("data") if isinstance(relationships.get(name), dict) else None
    value = data.get("id") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def next_cursor(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    values_ = parse_qs(urlsplit(url).query).get("page[cursor]") or []
    return values_[0] if values_ else ""


def events_filter(since: str) -> str:
    """The ``filter`` of the events poll: at or after ``since`` (ISO 8601).

    Inclusive, so an event sharing the last page's final timestamp is never
    skipped; ``ingest`` is idempotent per event id, so the overlap is a no-op.
    """
    return f"greater-or-equal(datetime,{since})"


def iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")
