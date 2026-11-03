"""Aircall call object -> ``communications.ingest.record`` arguments. Pure functions, no database.

| Aircall call | golden |
|---|---|
| ``id`` | ``external_id`` (``ExternalRef`` object type ``call``) |
| ``direction`` | ``inbound`` / ``outbound`` |
| ``raw_digits`` (the external number) | participant ``caller`` (inbound) or ``callee`` (outbound), E.164 |
| ``number.digits`` (the Aircall line) | the other participant |
| ``user.email`` | ``performed_by`` (resolved by the adapter: active staff of the company) |
| ``started_at`` / ``ended_at`` / ``duration`` | ``occurred_at``, ``ended_at``, ``duration_seconds`` |
| ``answered_at``, ``missed_call_reason``, ``voicemail`` | ``status`` + ``metadata.aircall.outcome`` |
| ``comments[].content`` / ``tags[].name`` | ``summary`` / ``metadata.aircall.tags`` |
| ``recording``, ``voicemail``, ``asset`` | nothing outside the raw payload (only ``has_recording`` / ``has_voicemail`` flags) |
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone

from core.plugins.payroll import AdapterError
from parties.normalize import normalize_phone

from . import values

DIRECTIONS = ("inbound", "outbound")


def _malformed(what: str) -> AdapterError:
    return AdapterError(
        f"Aircall call {what} — cause: a malformed or unexpected payload. "
        f"Fix: check the Aircall API version the account sends. {values.DOCS}"
    )


def parse_unix(value, field: str, *, required: bool = True) -> datetime | None:
    """An Aircall unix-seconds timestamp as an aware UTC datetime."""
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _malformed(f"has no valid {field}")
    return datetime.fromtimestamp(value, tz=dt_timezone.utc)


def unwrap(payload) -> tuple[str, dict | None]:
    """``(event, call)`` from a webhook body or a poll item; ``call`` is None when not a call event."""
    if not isinstance(payload, dict):
        return "", None
    event = payload.get("event") if isinstance(payload.get("event"), str) else ""
    if payload.get("resource") != "call":
        return event, None
    if event not in values.CALL_EVENTS and event != values.POLL_EVENT:
        return event, None
    call = payload.get("data")
    return event, call if isinstance(call, dict) else None


def call_id(call: dict) -> str:
    raw = call.get("id")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)) or not str(raw).strip():
        raise _malformed("has no id")
    text = str(raw).strip()
    if len(text) > 64 or not text.isalnum():
        raise _malformed("id is not an Aircall id")
    return text


def is_final(call: dict) -> bool:
    return call.get("status") == values.FINAL_STATUS


def phone(raw) -> str:
    """The E.164 form of ``raw`` (Phase 2 normaliser), else the raw text (it then matches nobody)."""
    text = str(raw or "").strip()[:64]
    return normalize_phone(text)[0] or text


def outcome(call: dict, direction: str) -> tuple[str, str]:
    """``(Communication.status, metadata outcome)``: answered -> completed; everything else failed."""
    if call.get("voicemail"):
        return "failed", values.VOICEMAIL
    reason = call.get("missed_call_reason")
    if isinstance(reason, str) and reason:
        return "failed", values.ABANDONED if reason in values.ABANDONED_REASONS else values.MISSED
    if call.get("answered_at"):
        return "completed", values.ANSWERED
    return "failed", values.MISSED if direction == "inbound" else values.NOT_ANSWERED


def summary(call: dict) -> str:
    comments = call.get("comments") if isinstance(call.get("comments"), list) else []
    rows = [c for c in comments if isinstance(c, dict) and isinstance(c.get("content"), str)]
    rows.sort(key=lambda c: (c.get("posted_at") if isinstance(c.get("posted_at"), int) else 0,
                             str(c.get("id", ""))))
    text = "\n".join(c["content"].strip() for c in rows if c["content"].strip())
    return text[: values.SUMMARY_MAX]


def tags(call: dict) -> list[str]:
    raw = call.get("tags") if isinstance(call.get("tags"), list) else []
    return sorted({t["name"].strip()[:64] for t in raw
                   if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"].strip()})


def _nested_id(value) -> int | str | None:
    if isinstance(value, dict):
        inner = value.get("id")
        if isinstance(inner, (int, str)) and not isinstance(inner, bool):
            return inner
    return None


def agent_email(call: dict) -> str:
    user = call.get("user")
    email = user.get("email") if isinstance(user, dict) else None
    return email.strip() if isinstance(email, str) else ""


def call_fields(call: dict) -> dict:
    """The ``record`` arguments for a finished call (``AdapterError`` when malformed)."""
    external_id = call_id(call)
    direction = call.get("direction")
    if direction not in DIRECTIONS:
        raise _malformed("direction is neither inbound nor outbound")
    started = parse_unix(call.get("started_at"), "started_at")
    ended = parse_unix(call.get("ended_at"), "ended_at", required=False)
    if ended is not None and ended < started:
        raise _malformed("ends before it starts")
    duration = call.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        duration = int((ended - started).total_seconds()) if ended is not None else None
    status, result = outcome(call, direction)
    external = phone(call.get("raw_digits"))
    number = call.get("number") if isinstance(call.get("number"), dict) else {}
    line = phone(number.get("digits"))
    external_role, line_role = ("caller", "callee") if direction == "inbound" else ("callee", "caller")
    participants = [(external_role, external)] if external else []
    if line:
        participants.append((line_role, line))
    meta = {
        "outcome": result,
        "tags": tags(call),
        "has_recording": bool(call.get("recording")),
        "has_voicemail": bool(call.get("voicemail")),
    }
    reason = call.get("missed_call_reason")
    if isinstance(reason, str) and reason:
        meta["missed_call_reason"] = reason[:64]
    for key, source in (("user_id", call.get("user")), ("number_id", number)):
        ident = _nested_id(source)
        if ident is not None:
            meta[key] = ident
    return {
        "external_id": external_id,
        "direction": direction,
        "occurred_at": started,
        "ended_at": ended,
        "duration_seconds": duration,
        "status": status,
        "summary": summary(call),
        "external_number": external,
        "participants": participants,
        "agent_email": agent_email(call),
        "metadata": meta,
    }


#: Keys Aircall re-signs on every read (short-lived media links): not part of a call's identity.
VOLATILE_KEYS = frozenset({"recording", "voicemail", "asset", "recording_short_url", "voicemail_short_url"})


def stable(call: dict) -> dict:
    """The call without its short-lived media links, for the "did anything change" comparison."""
    return {k: v for k, v in call.items() if k not in VOLATILE_KEYS}


def comment_count(call: dict) -> int:
    comments = call.get("comments")
    return len([c for c in comments if isinstance(c, dict)]) if isinstance(comments, list) else 0


def delivery_id(body: dict) -> str:
    """The webhook's replay key: event + call id (+ the newest comment id for ``call.commented``)."""
    event = body.get("event") if isinstance(body.get("event"), str) else ""
    call = body.get("data") if isinstance(body.get("data"), dict) else {}
    try:
        ident = call_id(call)
    except AdapterError:
        return ""
    key = f"{event[:40]}:{ident}"
    if event == "call.commented":
        comments = call.get("comments") if isinstance(call.get("comments"), list) else []
        ids = sorted((str(c.get("id")) for c in comments if isinstance(c, dict) and c.get("id") is not None),
                     key=lambda s: (len(s), s))
        if not ids:
            return ""  # no comment id to tell two comment events apart: the body hash is the key
        key += f":{ids[-1]}"
    return key[:200]
