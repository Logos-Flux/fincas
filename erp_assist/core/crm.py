"""CRM engagements, tasks and follow-ups for the assistant (docs/crm/phase3-engagement.md 5.3).

Every function talks to the staff API (``/api/v1/crm/``). Three rules:

- **The shared key is not a person.** The assistant authenticates with one
  service key, so nothing here uses ``me``. A tool that acts *for* someone
  takes ``for_user`` (a staff email, profile id or name) and resolves it
  through the staff directory (``crm/staff/``) to exactly one profile; an
  ``ambiguous`` or ``fuzzy`` answer raises :class:`ERPAmbiguousError` with
  the candidates (the Phase 2 2.7 envelope), never a guess. The backend then
  applies its own record rules to that profile (``crm/queues/<id>/``).
- **One tenant per call.** The key reads every tenant, so each call is pinned
  to one: the person's company, the engagement's, or ``company`` when given.
  Every list and detail request carries ``company=<uuid>``, so the server
  never returns another tenant's rows.
- **Proposals write nothing.** ``draft_follow_up`` only reads (``GET``) and
  returns text plus the calls a human would confirm. It refuses when the
  recipient's address is suppressed (``parties.consent.can_contact``) and
  never lists an erased party. The two write tools go through the services
  behind the API: ``create_task`` (``POST tasks/``) and ``move_stage``
  (``POST engagements/<id>/move/``, only with ``confirm=True``).

Outputs use canonical field names and keys, never tenant labels (spec 3.4).

**Reports** (docs/crm/phase6-adapters.md 3.6, Phase 6 step 7) are
company-wide aggregates, so they take no ``for_user`` but always a
``company``: the shared key reads every tenant. The ERP computes everything
(``/api/v1/crm/reports/``, the same numbers as the report pages) and writes
the ``summary``; these functions only pass parameters and reshape the answer
into ``{report, company, params, data, summary}``.
"""

from __future__ import annotations

import uuid
from typing import Any

from .client import ERPAmbiguousError, ERPClient, ERPNotFoundError, ERPValidationError

CRM = "/api/v1/crm/"
ENGAGEMENTS_URL = f"{CRM}engagements/"
TASKS_URL = f"{CRM}tasks/"
STAGES_URL = f"{CRM}stages/"
CONTACTS_URL = f"{CRM}engagement-contacts/"
STAFF_URL = f"{CRM}staff/"
QUEUES_URL = f"{CRM}queues/"
REPORTS_URL = f"{CRM}reports/"
#: Report name -> path under REPORTS_URL (the catalog lists them too).
REPORTS = {"funnel": "funnel/", "stage-duration": "stage-duration/", "follow-ups": "follow-ups/",
           "attribution": "attribution/"}
COMMUNICATIONS_URL = "/api/v1/communications/communications/"

EXACT, FUZZY, AMBIGUOUS, NONE = "exact", "fuzzy", "ambiguous", "none"
FOLLOW_UP_CHANNELS = ("email", "sms")
TIMELINE_ITEMS = 20


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _scoped(params: dict[str, Any] | None, company: str | None) -> dict[str, Any]:
    out = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    if company:
        out["company"] = company
    return out


def _check_company(company: str | None) -> str | None:
    """The canonical uuid string (the server's form, so comparisons hold), or None."""
    if not company:
        return None
    if not _is_uuid(company):
        raise ERPValidationError(f"company must be a tenant id (uuid), not {company!r}.")
    return str(uuid.UUID(str(company)))


# --- the staff directory -------------------------------------------------------------


async def find_staff(client: ERPClient, query: str, *, company: str | None = None) -> dict[str, Any]:
    """The staff directory's 2.7 envelope for an email, profile id or name.

    ``exact`` / ``fuzzy`` / ``none`` come back as the envelope; ``ambiguous``
    raises :class:`ERPAmbiguousError`.
    """
    company = _check_company(company)
    envelope = await client.get(STAFF_URL, params=_scoped({"q": query}, company))
    if envelope.get("match_quality") == AMBIGUOUS:
        raise ERPAmbiguousError(envelope, (
            f"'{query}' matches several staff accounts. Do not pick one: ask the user who is "
            "meant, then pass that person's email or profile id (and company if two tenants)."))
    return envelope


async def resolve_staff(client: ERPClient, ref: str, *, company: str | None = None,
                        what: str = "staff member") -> dict[str, Any]:
    """Exactly one staff person for ``ref``, or raise. Never guesses.

    ``exact`` (an email one profile holds, or a profile id) returns the
    candidate dict (id, display_name, email, company, timezone). A name match
    (``fuzzy``) and ``ambiguous`` raise :class:`ERPAmbiguousError`; ``none``
    raises :class:`ERPNotFoundError`.
    """
    if not ref or not str(ref).strip():
        raise ERPValidationError(f"Name the {what}: a staff email or profile id.")
    envelope = await find_staff(client, str(ref).strip(), company=company)
    quality = envelope.get("match_quality")
    if quality == EXACT and envelope.get("item"):
        return envelope["item"]
    if quality == FUZZY:
        raise ERPAmbiguousError(envelope, (
            f"'{ref}' is only a name match for a {what}. Confirm the person with the user, "
            "then pass their email or profile id."))
    raise ERPNotFoundError(f"No {what} matches '{ref}'.")


# --- engagement lookup -------------------------------------------------------------------


def _engagement_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"), "number": row.get("number"), "title": row.get("title"),
        "display_name": row.get("title"), "status": row.get("status"),
        "stage_key": row.get("stage_key"), "party_name": row.get("party_name"),
        "owner": row.get("owner"), "company": row.get("company"),
    }


async def find_engagement(client: ERPClient, query: str, *, company: str | None = None,
                          status: str | None = None) -> dict[str, Any]:
    """The 2.7 envelope for an engagement id, number (``EN-00001``) or text.

    ``exact``: the id, or the number. A title / party-name search is
    ``fuzzy`` when it has one hit. Several hits raise
    :class:`ERPAmbiguousError`; nothing is ever picked.
    """
    company = _check_company(company)
    query = (query or "").strip()
    if not query:
        raise ERPValidationError("Name the engagement: its id, number (EN-00001) or title.")
    if _is_uuid(query):
        try:
            row = await client.get(f"{ENGAGEMENTS_URL}{query}/", params=_scoped(None, company))
        except ERPNotFoundError:
            return {"match_quality": NONE, "item": None, "candidates": [],
                    "message": "No engagement has that id in this tenant."}
        return {"match_quality": EXACT, "item": row, "candidates": [_engagement_candidate(row)],
                "message": ""}
    rows = await client.list(ENGAGEMENTS_URL, params=_scoped({"search": query, "status": status,
                                                              "page_size": 25}, company), max_items=25)
    numbered = [r for r in rows if (r.get("number") or "").lower() == query.lower()]
    if len(numbered) == 1:
        return {"match_quality": EXACT, "item": numbered[0],
                "candidates": [_engagement_candidate(numbered[0])], "message": ""}
    hits = numbered or rows
    if len(hits) == 1:
        return {"match_quality": FUZZY, "item": hits[0], "candidates": [_engagement_candidate(hits[0])],
                "message": "A text match only: confirm this engagement before acting on it."}
    if not hits:
        return {"match_quality": NONE, "item": None, "candidates": [],
                "message": f"No engagement matches '{query}'."}
    raise ERPAmbiguousError(
        {"match_quality": AMBIGUOUS, "item": None,
         "candidates": [_engagement_candidate(r) for r in hits[:10]],
         "message": ""},
        f"'{query}' matches {len(hits)} engagements. Do not pick one: ask the user which they "
        "mean (number, party, stage), then pass its id or number.")


async def resolve_engagement(client: ERPClient, ref: str, *, company: str | None = None) -> dict[str, Any]:
    """Exactly one engagement (the API row), or raise: ``fuzzy`` needs the
    user's confirmation first, ``ambiguous`` the user's choice."""
    envelope = await find_engagement(client, ref, company=company)
    quality = envelope.get("match_quality")
    if quality == EXACT:
        return envelope["item"]
    if quality == FUZZY:
        raise ERPAmbiguousError(envelope, (
            f"'{ref}' is only a possible match for an engagement. Confirm it with the user, then "
            "pass its id or number."))
    raise ERPNotFoundError(envelope.get("message") or f"No engagement matches '{ref}'.")


# --- the person's queue -----------------------------------------------------------------


def _task_brief(t: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "kind", "status", "priority", "due_at", "all_day", "due_tz", "is_overdue",
            "engagement", "party", "assignee")
    return {k: t.get(k) for k in keys}


def _engagement_brief(e: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "number", "title", "status", "pipeline_key", "stage_key", "party", "party_name",
            "owner", "expected_value", "currency", "expected_close_date", "next_action_due_at",
            "last_activity_at")
    return {k: e.get(k) for k in keys}


async def _queue(client: ERPClient, for_user: str, company: str | None) -> tuple[dict, dict]:
    person = await resolve_staff(client, for_user, company=company, what="person to act for")
    queue = await client.get(f"{QUEUES_URL}{person['id']}/", params=_scoped(None, person["company"]))
    return person, queue


def _who(person: dict[str, Any]) -> dict[str, Any]:
    return {k: person.get(k) for k in ("id", "display_name", "email", "company")}


async def my_overdue(client: ERPClient, for_user: str, *, company: str | None = None) -> dict[str, Any]:
    """What is overdue for one person: their open tasks due before now, and
    their open engagements with no next action."""
    person, queue = await _queue(client, for_user, company)
    return {
        "for_user": _who(person), "company": person["company"], "timezone": queue["timezone"],
        "overdue": [_task_brief(t) for t in queue["overdue"]],
        "needs_next_action": [_engagement_brief(e) for e in queue["needs_next_action"]],
    }


async def my_day(client: ERPClient, for_user: str, *, company: str | None = None) -> dict[str, Any]:
    """One person's day (spec 6.4): overdue, due by the end of their local
    day, and engagements needing a next action."""
    person, queue = await _queue(client, for_user, company)
    return {
        "for_user": _who(person), "company": person["company"], "timezone": queue["timezone"],
        "overdue": [_task_brief(t) for t in queue["overdue"]],
        "today": [_task_brief(t) for t in queue["today"]],
        "needs_next_action": [_engagement_brief(e) for e in queue["needs_next_action"]],
    }


# --- engagements -------------------------------------------------------------------------


async def list_engagements(
    client: ERPClient,
    *,
    company: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    pipeline: str | None = None,
    stage: str | None = None,
    party: str | None = None,
    source: str | None = None,
    needs_next_action: bool | None = None,
    overdue: bool | None = None,
    stale: bool | None = None,
    no_party: bool | None = None,
    search: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """The engagement list with the API's filters, in the attention order.

    ``owner`` is a staff email / profile id (resolved through the directory,
    and pinning the call to that person's tenant) or ``"none"``. ``pipeline``,
    ``stage`` and ``party`` are ids.
    """
    company = _check_company(company)
    if owner == "me":
        raise ERPValidationError("'me' is not available to the assistant: pass the person's email.")
    owner_id = owner
    if owner and owner != "none":
        person = await resolve_staff(client, owner, company=company, what="owner")
        owner_id = person["id"]
        if company and person["company"] != company:
            raise ERPValidationError("That owner belongs to another company.")
        company = person["company"]
    flags = {"needs_next_action": needs_next_action, "overdue": overdue, "stale": stale,
             "no_party": no_party}
    params: dict[str, Any] = {
        "status": status, "owner": owner_id, "pipeline": pipeline, "stage": stage, "party": party,
        "source": source, "search": search, "ordering": ordering,
        "page_size": 100 if limit is None else min(limit, 100),
        **{k: "true" for k, v in flags.items() if v},
    }
    rows = await client.list(ENGAGEMENTS_URL, params=_scoped(params, company), max_items=limit)
    return [_engagement_brief(r) for r in rows]


async def get_engagement(client: ERPClient, engagement: str, *, company: str | None = None) -> dict[str, Any]:
    """The thread in one read: header facts, the next action, the last
    communication, open tasks, contacts with their ``can_contact`` decision
    (erased parties left out), and the latest timeline items (subjects and
    summaries only, never bodies or raw payloads)."""
    row = await resolve_engagement(client, engagement, company=company)
    tenant = row["company"]
    eid = row["id"]
    facts = await client.get(f"{ENGAGEMENTS_URL}{eid}/follow-up-facts/", params=_scoped(None, tenant))
    timeline = await client.get(f"{ENGAGEMENTS_URL}{eid}/timeline/",
                                params=_scoped({"limit": TIMELINE_ITEMS * 2}, tenant))
    hidden = set(facts.get("hidden_parties") or [])
    recent = [i for i in timeline.get("items", []) if i.get("party") not in hidden][:TIMELINE_ITEMS]
    upcoming = [i for i in timeline.get("upcoming") or [] if i.get("party") not in hidden]
    open_tasks = facts["open_tasks"]
    next_action = next((t for t in open_tasks if t["id"] == row.get("next_task")), None)
    return {
        "engagement": {**_engagement_brief(row), "company": tenant, "stage_name": facts["engagement"]["stage_name"],
                       "owner_name": facts["engagement"]["owner_name"],
                       "effective_probability": row.get("effective_probability"),
                       "weighted_value": row.get("weighted_value"), "source": row.get("source"),
                       "loss_reason": row.get("loss_reason")},
        "next_action": next_action,
        "needs_next_action": row.get("status") == "open" and row.get("next_task") is None,
        "last_communication": facts["last_communication"],
        "open_tasks": open_tasks,
        "contacts": facts["contacts"],
        "recent": recent,
        "upcoming": upcoming,
    }


# --- the follow-up proposal -----------------------------------------------------------------


def _first_name(display_name: str | None) -> str:
    name = (display_name or "").strip()
    return name.split()[0] if name else "there"


async def draft_follow_up(client: ERPClient, engagement: str, *, channel: str = "email",
                          intent: str | None = None, company: str | None = None) -> dict[str, Any]:
    """A follow-up **proposal** for one engagement. Writes nothing, sends nothing.

    Returns ``{proposal, sent: false, written: false, to, subject, body,
    suggested_task, consent, facts, apply}``; ``apply`` lists the API calls a
    human would confirm after sending it themselves. Refuses with
    ``{refused: "suppressed", ...}`` (and no address) when the recipient's
    address is suppressed, or ``no_recipient`` / ``no_address``.
    """
    if channel not in FOLLOW_UP_CHANNELS:
        raise ERPValidationError(f"channel must be one of {', '.join(FOLLOW_UP_CHANNELS)}.")
    row = await resolve_engagement(client, engagement, company=company)
    tenant = row["company"]
    facts = await client.get(f"{ENGAGEMENTS_URL}{row['id']}/follow-up-facts/",
                             params=_scoped({"channel": channel}, tenant))
    eng = facts["engagement"]
    base = {"proposal": True, "sent": False, "written": False, "engagement": eng, "channel": channel}
    if facts["refusal"]:
        reasons = {
            "suppressed": "The recipient's address is suppressed (do-not-contact). Do not draft or "
                          "send to it; tell the user, who can review the suppression with staff.",
            "no_recipient": "No live party on this engagement to write to (none, or erased).",
            "no_address": f"No usable {channel} contact point on this engagement's people.",
        }
        return {**base, "refused": facts["refusal"], "message": reasons.get(facts["refusal"], ""),
                "to": facts["to"], "consent": facts["consent"]}
    to = facts["to"]
    last = facts["last_communication"]
    owner = eng.get("owner_name") or ""
    topic = (intent or "").strip()
    if channel == "email":
        # "Re:" only on the recipient's own email thread, never someone else's subject.
        replying = last and last.get("subject") and last.get("kind") == "email" \
            and last.get("party") == to["party"]
        subject = f"Re: {last['subject']}" if replying else "Following up"
        lines = [f"Hi {_first_name(to['display_name'])},", "",
                 topic or "I wanted to follow up on our recent conversation and see whether you "
                          "have any questions, or what the best next step is on your side.",
                 "", "Best regards,", owner]
        body = "\n".join(lines).rstrip()
    else:
        subject = ""
        body = (f"Hi {_first_name(to['display_name'])}, "
                + (topic or "just following up on our conversation. Any questions?")
                + (f" - {owner}" if owner else ""))
    suggested_task = {"title": f"Follow up with {to['display_name']}", "kind": "follow_up",
                      "due": facts["suggested_due_date"], "due_tz": facts["owner_timezone"],
                      "assignee": eng.get("owner"), "engagement": eng["id"]}
    kind = "email" if channel == "email" else "sms"
    apply = [
        {"step": "log_communication", "when": "after a human has sent the message",
         "method": "POST", "path": COMMUNICATIONS_URL,
         "body": {"company": tenant, "kind": kind, "direction": "outbound", "engagement": eng["id"],
                  "party": to["party"], "subject": subject, "body_text": body,
                  "occurred_at": "<when it was sent>",
                  "participant_input": [{"role": "to", "address": to["address"],
                                         "display_name": to["display_name"], "party": to["party"]}]}},
        {"step": "set_next_action", "when": "if the user wants a reminder",
         "method": "POST", "path": TASKS_URL,
         "body": {"company": tenant, **{k: v for k, v in suggested_task.items() if v is not None}}},
    ]
    return {
        **base, "refused": None, "to": to, "subject": subject, "body": body,
        "suggested_task": suggested_task, "consent": facts["consent"],
        "facts": {"last_communication": last, "open_tasks": facts["open_tasks"],
                  "owner_timezone": facts["owner_timezone"]},
        "apply": apply,
        "message": "A proposal only: nothing was sent or saved. Show it to the user; the user "
                   "sends it, then the apply steps log it.",
    }


# --- write tools -------------------------------------------------------------------------


async def create_task(
    client: ERPClient,
    *,
    title: str,
    engagement: str | None = None,
    assignee: str | None = None,
    due: str | None = None,
    due_tz: str | None = None,
    kind: str | None = None,
    priority: str | None = None,
    description: str | None = None,
    company: str | None = None,
) -> dict[str, Any]:
    """Create a task (the next action when it has an engagement) through
    ``POST tasks/`` and so ``crm.tasks.create``.

    ``engagement`` is an id or number (exact only); ``assignee`` a staff email
    or profile id, resolved through the directory (``ambiguous`` / name-only
    raise the envelope, nothing is written). The task's tenant is the
    engagement's, else the assignee's, else ``company``; a mismatch is refused
    before any write.
    """
    company = _check_company(company)
    if not (title or "").strip():
        raise ERPValidationError("A task needs a title.")
    eng = await resolve_engagement(client, engagement, company=company) if engagement else None
    tenant = eng["company"] if eng else company
    person = await resolve_staff(client, assignee, company=tenant, what="assignee") if assignee else None
    if person is not None:
        if tenant and person["company"] != tenant:
            raise ERPValidationError("The assignee belongs to another company than the engagement.")
        tenant = tenant or person["company"]
    payload: dict[str, Any] = {"title": title.strip()}
    optional = {"engagement": eng["id"] if eng else None, "assignee": person["id"] if person else None,
                "due": due, "due_tz": due_tz, "kind": kind, "priority": priority,
                "description": description, "company": tenant}
    payload.update({k: v for k, v in optional.items() if v is not None})
    return await client.post(TASKS_URL, json=payload)


async def _stage_for(client: ERPClient, eng: dict[str, Any], stage: str) -> dict[str, Any]:
    tenant = eng["company"]
    if _is_uuid(stage):
        try:
            row = await client.get(f"{STAGES_URL}{stage}/", params=_scoped(None, tenant))
        except ERPNotFoundError as exc:
            raise ERPNotFoundError("No such stage in this engagement's company.") from exc
        return row
    stages = await client.list(STAGES_URL, params=_scoped({"pipeline": eng["pipeline"], "is_active": "true"},
                                                          tenant))
    want = stage.strip().lower()
    hits = [s for s in stages if s.get("key") == want] or \
        [s for s in stages if (s.get("name") or "").strip().lower() == want]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ERPAmbiguousError({"match_quality": AMBIGUOUS, "item": None, "candidates": [
            {"id": s["id"], "display_name": s.get("name"), "key": s.get("key")} for s in hits]},
            f"'{stage}' names several stages: pass the stage key or id.")
    raise ERPNotFoundError(
        f"No active stage '{stage}' in this pipeline. Stages: "
        + ", ".join(s.get("key", "") for s in sorted(stages, key=lambda s: s.get("sort_order") or 0)))


async def move_stage(client: ERPClient, *, engagement: str, stage: str, note: str | None = None,
                     company: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """Move an open engagement to a stage through ``POST engagements/<id>/move/``
    (``crm.engagements.move``; a stage id of another pipeline changes the
    pipeline). Never wins or loses: ``needs_close`` is the stage's outcome
    hint for the user to confirm in the ERP.

    Without ``confirm=True`` nothing is written and the preview comes back.
    """
    company = _check_company(company)
    eng = await resolve_engagement(client, engagement, company=company)
    target = await _stage_for(client, eng, stage)
    if target.get("company") != eng["company"]:
        raise ERPValidationError("That stage belongs to another company.")
    preview = {"engagement": eng["id"], "number": eng.get("number"), "status": eng.get("status"),
               "from_stage": eng.get("stage_key"), "to_stage": target.get("key"),
               "to_stage_id": target["id"], "needs_close": target.get("outcome_hint") or ""}
    if confirm is not True:
        return {"error": "ConfirmationRequired", "written": False, "preview": preview,
                "message": "Refusing to move the engagement without explicit confirm=True. Show the "
                           "user the preview; call again with confirm=True once they agree."}
    result = await client.post(f"{ENGAGEMENTS_URL}{eng['id']}/move/",
                               json={"stage": target["id"], "note": note or ""})
    moved = result.get("engagement") or {}
    return {"written": result.get("change") is not None, "change": result.get("change"),
            "needs_close": result.get("needs_close") or "", "engagement": _engagement_brief(moved),
            "message": ("The stage suggests closing as " + result["needs_close"] + ": a person "
                        "confirms the outcome in the ERP; this tool never closes.")
            if result.get("needs_close") else ""}


# --- reports (Phase 6 step 7) ----------------------------------------------------------


async def list_crm_reports(client: ERPClient) -> dict[str, Any]:
    """The report catalog: each report's parameters, allowed values, columns
    and the definitions its page shows."""
    return await client.get(REPORTS_URL)


def _report_name(report: str) -> str:
    name = (report or "").strip().lower().replace("_", "-")
    if name not in REPORTS:
        raise ERPValidationError(f"report must be one of {', '.join(REPORTS)}, not {report!r}.")
    return name


async def crm_report(
    client: ERPClient,
    report: str,
    *,
    company: str | None,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    **params: Any,
) -> dict[str, Any]:
    """One CRM report for one tenant: ``{report, company, params, data, summary}``.

    ``company`` is required (the shared key is cross-tenant). ``period`` or
    ``date_from`` + ``date_to`` (local days, site time zone); report params
    (``attribution``, ``pipeline``) pass through. The ERP refuses unknown
    params and values with the allowed list (``ERPValidationError``).
    """
    name = _report_name(report)
    company = _check_company(company)
    if company is None:
        raise ERPValidationError(
            "company is required: reports are per tenant and the assistant's key reads every tenant.")
    query = _scoped({"period": period, "date_from": date_from, "date_to": date_to, **params}, company)
    answer = await client.get(f"{REPORTS_URL}{REPORTS[name]}", params=query)
    data = {k: answer.get(k) for k in ("timezone", "columns", "rows", "totals", "notes", "coverage")}
    return {"report": answer["report"], "company": answer["company"], "params": answer["params"],
            "data": data, "summary": answer["summary"]}


async def crm_funnel_report(client: ERPClient, *, company: str | None, period: str | None = None,
                            date_from: str | None = None, date_to: str | None = None,
                            attribution: str | None = None) -> dict[str, Any]:
    """Engagements created in the period by source (``attribution``:
    ``engagement`` or ``first_touch``)."""
    return await crm_report(client, "funnel", company=company, period=period, date_from=date_from,
                            date_to=date_to, attribution=attribution)


async def crm_stage_report(client: ERPClient, *, company: str | None, period: str | None = None,
                           date_from: str | None = None, date_to: str | None = None,
                           pipeline: str | None = None) -> dict[str, Any]:
    """Stage duration and conversion for one pipeline (key or id; default the
    tenant's default pipeline)."""
    return await crm_report(client, "stage-duration", company=company, period=period,
                            date_from=date_from, date_to=date_to, pipeline=pipeline)


async def crm_follow_up_report(client: ERPClient, *, company: str | None, period: str | None = None,
                               date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    """Follow-up completion: tasks due in the period, plus next actions now."""
    return await crm_report(client, "follow-ups", company=company, period=period,
                            date_from=date_from, date_to=date_to)
