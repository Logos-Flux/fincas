"""HubSpot records -> canonical records, and the coverage of each type.

Pure functions of a :class:`~.model.Bundle`: no files are read here, so the
crawl and the CSV export convert the same way (docs/crm/phase5-importer.md
sections 2.2-2.13; the canonical fields are docs/crm/importing.md section 3).
"""

from __future__ import annotations

import json
import zoneinfo
from datetime import datetime, timezone
from typing import Any

from core.plugins.crm_export import AdapterError
from crm_import.canonical import OBJECT_TYPES, make_record

from .mapping import CONSUMED
from .model import (
    CANONICAL,
    COMMUNICATIONS,
    CORE_PAIRS,
    PAIRS,
    SINGULAR,
    Bundle,
    Feed,
    PairFeed,
    Status,
)
from .values import (
    DOCS,
    address_hash,
    html_to_text,
    id_key,
    is_key,
    looks_like_html,
    parse_bool,
    parse_decimal,
    parse_instant,
    slug_key,
    split_multi,
    text,
)

#: The raw envelope version of a provider object carried in a communication's
#: ``raw`` (``{schema_version, provider, payload}``): version 1 is a CRM v3
#: object exactly as recorded. The applier passes it as ``known_max_version``.
RAW_SCHEMA_VERSION = 1

TASK_KINDS = {"TODO": "todo", "CALL": "call", "EMAIL": "email"}
TASK_STATUSES = {"NOT_STARTED": "open", "IN_PROGRESS": "in_progress", "WAITING": "waiting",
                 "DEFERRED": "waiting", "COMPLETED": "done"}
TASK_PRIORITIES = {"LOW": "low", "MEDIUM": "normal", "HIGH": "high"}
MEETING_STATUSES = {"COMPLETED": "completed", "SCHEDULED": "scheduled", "RESCHEDULED": "scheduled",
                    "NO_SHOW": "no_show", "CANCELED": "cancelled", "CANCELLED": "cancelled"}
CALL_STATUSES = {"COMPLETED": "completed", "CANCELED": "cancelled", "BUSY": "failed", "NO_ANSWER": "failed",
                 "FAILED": "failed"}
CALL_DIRECTIONS = {"INBOUND": "inbound", "OUTBOUND": "outbound"}
EMAIL_DIRECTIONS = {"EMAIL": "outbound", "INCOMING_EMAIL": "inbound", "FORWARDED_EMAIL": "outbound"}
BODY = {"notes": "hs_note_body", "meetings": "hs_meeting_body", "calls": "hs_call_body", "emails": "hs_email_html"}
FOLLOW_UP_FIELDS = ("hs_next_step", "notes_next_activity_date")
TARGET_ORDER = {"contacts": 0, "companies": 1, "deals": 2, "tickets": 3}


def _int(value, where: str) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"{where}: {value!r} is not a whole number. " + DOCS) from exc


def _moment(instant: str) -> datetime:
    return datetime.fromisoformat(instant.replace("Z", "+00:00"))


def ref(object_type: str, source_id: str) -> dict:
    return {"object_type": object_type, "source_id": str(source_id)}


def _missing_feed(obj: str) -> Feed:
    feed = Feed(obj=obj, input="")
    feed.status.enumerated = False
    feed.status.add("file_absent", f"no {obj} in the input (neither crawl/{obj}/ nor export/{obj}.csv)")
    return feed


class Converter:
    def __init__(self, bundle: Bundle, *, mapping: dict, source: str, time_zone: str | None = None):
        self.b = bundle
        # The zone a close date is a date in: --time-zone, else the portal's zone, else UTC.
        self.date_zone = timezone.utc
        for name in (time_zone, bundle.account.get("timeZone")):
            try:
                if isinstance(name, str) and name:
                    self.date_zone = zoneinfo.ZoneInfo(name)
                    break
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                continue
        self.mapping = mapping
        self.source = source
        self.feeds = {obj: bundle.feeds.get(obj) or _missing_feed(obj) for obj in CANONICAL}
        self.records: dict[str, list[dict]] = {t: [] for t in OBJECT_TYPES}
        self.stage_outcome: dict[str, dict[str, str]] = {}  # pipeline id -> stage id -> outcome
        self.links: dict[tuple[str, str], dict[str, list]] = {}  # pair -> from id -> [(to id, Link)]
        self.truncated_bodies = 0
        self._apply_primary_company()
        for key, pair in bundle.pairs.items():
            index: dict[str, list] = {}
            for (a_id, b_id), link in pair.links.items():
                index.setdefault(a_id, []).append((b_id, link))
            for items in index.values():
                items.sort(key=lambda x: (not x[1].primary, id_key(x[0])))
            self.links[key] = index

    # -- helpers ---------------------------------------------------------------
    def _apply_primary_company(self) -> None:
        """``associatedcompanyid`` names a contact's primary company in both inputs."""
        feed = self.b.feeds.get("contacts")
        if feed is None:
            return
        for record in feed.records.values():
            company = text(record.properties.get("associatedcompanyid"))
            if company:
                link = self.b.pair("contacts", "companies").link(record.id, company)
                link.primary = True

    def targets(self, obj: str, record_id: str, others: tuple[str, ...]) -> list[dict]:
        out = []
        for other in sorted(others, key=lambda o: TARGET_ORDER.get(o, 9)):
            for other_id, _link in sorted(self.links.get((obj, other), {}).get(record_id, []),
                                          key=lambda x: id_key(x[0])):
                out.append(ref(CANONICAL[other], other_id))
        return out

    def refs(self, obj: str, record_id: str, other: str) -> list[dict]:
        return [ref(CANONICAL[other], other_id) for other_id, _ in self.links.get((obj, other), {}).get(record_id, [])]

    def owner_ref(self, props: dict) -> dict | None:
        owner = text(props.get("hubspot_owner_id"))
        return ref("owner", owner) if owner else None

    def properties(self, obj: str, props: dict, extra_consumed: tuple[str, ...] = ()) -> dict:
        consumed = CONSUMED.get(obj, frozenset()) | set(extra_consumed)
        return {k: v for k, v in sorted(props.items()) if k not in consumed}

    def history(self, feed: Feed, record) -> list[dict] | None:
        if not feed.history_exported or record.archived:
            return None
        out = []
        for prop in sorted(feed.history_props):
            for entry in (record.history or {}).get(prop, []):
                at = parse_instant(entry.get("timestamp"), where=f"{record.raw_ref} history {prop}")
                if at is None:
                    continue
                value = entry.get("value")
                out.append({"property": prop, "value": value, "at": at,
                            "source": text(entry.get("sourceType"))})
        out.sort(key=lambda h: (h["property"], h["at"], json.dumps(h["value"], sort_keys=True)))
        return out

    def envelope(self, object_type: str, feed: Feed, record, data: dict, properties: dict, *,
                 source_object_type: str, aliases=()) -> dict:
        where = record.raw_ref
        return make_record(
            object_type,
            source=self.source,
            source_id=record.id,
            source_object_type=source_object_type,
            aliases=[a for a in aliases if a != record.id],
            archived=record.archived,
            created_at=parse_instant(record.created_at, where=f"{where} created"),
            updated_at=parse_instant(record.updated_at, where=f"{where} updated"),
            data=data,
            properties=properties,
            history=self.history(feed, record),
            raw_ref=record.raw_ref,
        )

    def records_of(self, obj: str):
        feed = self.feeds[obj]
        return feed, sorted(feed.records.values(), key=lambda r: id_key(r.id))

    # -- owners and pipelines --------------------------------------------------
    def owners(self) -> None:
        seen: set[str] = set()
        for item, archived, raw_ref in sorted(self.b.owners, key=lambda x: (x[1], id_key(str((x[0] or {}).get("id"))))):
            if not isinstance(item, dict) or item.get("id") in (None, ""):
                raise AdapterError(f"{raw_ref} is not a HubSpot owner (no id). " + DOCS)
            oid = str(item["id"])
            if oid in seen:
                continue  # listed as active and archived: the active entry wins
            seen.add(oid)
            first, last, email = text(item.get("firstName")), text(item.get("lastName")), text(item.get("email"))
            display = " ".join(x for x in (first, last) if x) or email or f"Owner {oid}"
            teams = []
            for team in item.get("teams") or []:
                if isinstance(team, dict) and team.get("id") not in (None, ""):
                    teams.append({"source_id": str(team["id"]), "name": str(team.get("name") or ""),
                                  "primary": team.get("primary") is True})
            teams.sort(key=lambda t: (not t["primary"], id_key(t["source_id"])))
            props = {k: v for k, v in sorted(item.items())
                     if k not in ("id", "email", "firstName", "lastName", "teams", "archived", "createdAt",
                                  "updatedAt") and v is not None}
            self.records["owner"].append(make_record(
                "owner", source=self.source, source_id=oid, source_object_type="owner",
                archived=archived or item.get("archived") is True,
                created_at=parse_instant(item.get("createdAt"), where=raw_ref),
                updated_at=parse_instant(item.get("updatedAt"), where=raw_ref),
                data={"email": email, "given_name": first, "family_name": last, "display_name": display,
                      "active": not (archived or item.get("archived") is True), "teams": teams},
                properties=props, raw_ref=raw_ref,
            ))

    def pipelines(self) -> None:
        keys: dict[str, str] = {}
        seen_ids: dict[str, str] = {}
        mapped = self.mapping.get("pipelines", {})
        for obj, applies_to in (("deals", "deal"), ("tickets", "ticket")):
            for item, raw_ref in self.b.pipelines.get(obj, []):
                if not isinstance(item, dict) or item.get("id") in (None, "") or not isinstance(item.get("stages"), list):
                    raise AdapterError(f"{raw_ref} is not a HubSpot pipeline (expected id and stages). " + DOCS)
                pid = str(item["id"])
                if pid in seen_ids:
                    raise AdapterError(
                        f"pipeline id {pid!r} is both a {seen_ids[pid]} and a {obj} pipeline — cause: the "
                        "canonical format holds one pipeline per id. Fix: report this export; the adapter "
                        "cannot tell the two apart. " + DOCS)
                seen_ids[pid] = obj
                spec = mapped.get(pid, {})
                key = spec.get("key") or slug_key(pid)
                if key in keys:
                    raise AdapterError(
                        f"pipelines {keys[key]!r} and {pid!r} both get the key {key!r} — cause: their ids slug "
                        f"to the same key. Fix: name one in the mapping file ([pipelines.\"{pid}\"] key = "
                        "\"…\"). " + DOCS)
                keys[key] = pid
                stage_keys: dict[str, str] = {}
                stages = []
                outcomes: dict[str, str] = {}
                for stage in sorted(item["stages"], key=lambda s: (_int(s.get("displayOrder"), raw_ref),
                                                                    id_key(str(s.get("id"))))):
                    sid = str(stage.get("id") or "")
                    label = text(stage.get("label")) or sid
                    if not sid:
                        raise AdapterError(f"{raw_ref}: a stage has no id. " + DOCS)
                    if len(label) > 100:
                        raise AdapterError(f"{raw_ref}: stage {sid!r} has a label over 100 characters; "
                                           "shorten it in HubSpot before exporting. " + DOCS)
                    skey = (spec.get("stages") or {}).get(sid) or slug_key(sid)
                    if skey in stage_keys:
                        raise AdapterError(
                            f"pipeline {pid!r}: stages {stage_keys[skey]!r} and {sid!r} both get the key {skey!r}. "
                            f"Fix: name one in the mapping file ([pipelines.\"{pid}\".stages]). " + DOCS)
                    stage_keys[skey] = sid
                    meta = stage.get("metadata") if isinstance(stage.get("metadata"), dict) else {}
                    probability = None
                    outcome = ""
                    if applies_to == "deal":
                        raw_prob = meta.get("probability")
                        try:
                            prob = float(raw_prob) if raw_prob not in (None, "") else None
                        except (TypeError, ValueError):
                            prob = None
                        if prob is not None and 0.0 <= prob <= 1.0:
                            probability = int(round(prob * 100))
                        if str(meta.get("isClosed", "")).lower() == "true":
                            outcome = "won" if prob == 1.0 else "lost" if prob == 0.0 else "closed"
                    elif str(meta.get("ticketState", "")).upper() == "CLOSED":
                        outcome = "closed"
                    order = _int(stage.get("displayOrder"), raw_ref)
                    if order < 0:
                        raise AdapterError(f"{raw_ref}: stage {sid!r} has a negative displayOrder. " + DOCS)
                    stages.append({"source_id": sid, "key": skey, "name": label, "sort_order": order,
                                   "probability": probability, "outcome": outcome})
                    outcomes[sid] = outcome
                self.stage_outcome[pid] = outcomes
                order = _int(item.get("displayOrder"), raw_ref)
                stage_meta = {str(s.get("id")): s.get("metadata") for s in item["stages"] if s.get("metadata")}
                archived_stages = sorted(str(s.get("id")) for s in item["stages"] if s.get("archived") is True)
                props = {}
                if stage_meta:
                    props["stage_metadata"] = dict(sorted(stage_meta.items()))
                if archived_stages:
                    props["archived_stages"] = archived_stages
                self.records["pipeline"].append(make_record(
                    "pipeline", source=self.source, source_id=pid, source_object_type="pipeline",
                    archived=item.get("archived") is True,
                    created_at=parse_instant(item.get("createdAt"), where=raw_ref),
                    updated_at=parse_instant(item.get("updatedAt"), where=raw_ref),
                    data={"applies_to": applies_to, "key": key, "name": text(item.get("label")) or pid,
                          "sort_order": max(order, 0), "active": item.get("archived") is not True,
                          "stages": stages},
                    properties=props, raw_ref=raw_ref,
                ))
        if any(self.b.pipelines.get(obj) for obj in ("deals", "tickets")):
            for pid, spec in sorted(mapped.items()):
                unknown = [] if pid in self.stage_outcome else [f"pipeline {pid!r}"]
                unknown += [f"stage {sid!r} of pipeline {pid!r}" for sid in sorted((spec.get("stages") or {}))
                            if pid in self.stage_outcome and sid not in self.stage_outcome[pid]]
                if unknown:
                    raise AdapterError(
                        f"mapping file: [pipelines] names {', '.join(unknown)}, which the export does not hold — "
                        "cause: a typo, or a pipeline deleted in HubSpot. Fix: correct the id (pipeline and stage "
                        "ids, not labels). " + DOCS)

    # -- parties -----------------------------------------------------------------
    @staticmethod
    def _address(props: dict, line_2: str | None) -> list[dict]:
        parts = {"line_1": text(props.get("address")), "line_2": text(props.get(line_2)) if line_2 else None,
                 "city": text(props.get("city")), "region": text(props.get("state")),
                 "postal_code": text(props.get("zip")), "country": text(props.get("country"))}
        if not any(parts.values()):
            return []
        return [{"purpose": "other", **parts, "primary": True}]

    @staticmethod
    def _phones(props: dict, fields: tuple[tuple[str, str], ...]) -> list[dict]:
        phones = []
        for name, kind in fields:
            raw = text(props.get(name))
            if raw and not any(p["raw"] == raw for p in phones):
                phones.append({"raw": raw[:64], "kind": kind, "primary": not phones})
        return phones

    def companies(self) -> None:
        feed, records = self.records_of("companies")
        for r in records:
            p = r.properties
            domains = []
            for domain in [text(p.get("domain"))] + split_multi(p.get("hs_additional_domains")):
                if domain and domain not in domains:
                    domains.append(domain)
            data = {"name": text(p.get("name")), "legal_name": None, "domains": domains,
                    "phones": self._phones(p, (("phone", "phone"),)), "addresses": self._address(p, "address2"),
                    "owner_ref": self.owner_ref(p), "registration_number": None, "tax_id": None}
            self.records["company"].append(self.envelope(
                "company", feed, r, data, self.properties("companies", p), source_object_type="company",
                aliases=split_multi(p.get("hs_merged_object_ids"))))

    def contacts(self) -> None:
        feed, records = self.records_of("contacts")
        for r in records:
            p = r.properties
            emails = []
            primary = text(p.get("email"))
            if primary:
                emails.append({"address": primary, "primary": True})
            for extra in split_multi(p.get("hs_additional_emails")):
                if not any(e["address"] == extra for e in emails):
                    emails.append({"address": extra, "primary": False})
            data = {"given_name": text(p.get("firstname")), "family_name": text(p.get("lastname")),
                    "emails": emails,
                    "phones": self._phones(p, (("phone", "phone"), ("mobilephone", "mobile"), ("fax", "fax"))),
                    "addresses": self._address(p, None), "owner_ref": self.owner_ref(p),
                    "company_refs": self.refs("contacts", r.id, "companies"),
                    "marketable": parse_bool(p.get("hs_marketable_status")), "job_title": text(p.get("jobtitle"))}
            self.records["contact"].append(self.envelope(
                "contact", feed, r, data, self.properties("contacts", p), source_object_type="contact",
                aliases=split_multi(p.get("hs_merged_object_ids"))))
            self._consent(r, emails)

    # -- deals and tickets --------------------------------------------------------
    def _currency(self, p: dict) -> str | None:
        for value in (p.get("deal_currency_code"), self.b.account.get("companyCurrency"),
                      self.mapping.get("default_currency")):
            value = text(value)
            if value and len(value) == 3 and value.isalpha():
                return value.upper()
        return None

    def deals(self) -> None:
        feed, records = self.records_of("deals")
        follow_props = sorted(k for k, v in (self.mapping.get("deal", {}).get("properties", {})).items()
                              if v == "follow_up" and k not in FOLLOW_UP_FIELDS)
        for r in records:
            p = r.properties
            pid, sid = text(p.get("pipeline")), text(p.get("dealstage"))
            if not pid or not sid:
                raise AdapterError(f"{r.raw_ref}: deal {r.id} has no pipeline or dealstage — every HubSpot "
                                   "deal has both; the export is damaged. " + DOCS)
            won, closed = parse_bool(p.get("hs_is_closed_won")), parse_bool(p.get("hs_is_closed"))
            if closed is True and won is not None:
                status = "won" if won else "lost"
            elif closed is False:
                status = "open"
            elif won is True:
                status = "won"
            else:  # no flags, or closed with no won flag: the stage decides
                outcome = self.stage_outcome.get(pid, {}).get(sid)
                status = {"won": "won", "lost": "lost", "": "open"}.get(outcome) if outcome is not None else None
            closedate = parse_instant(p.get("closedate"), where=f"{r.raw_ref} closedate")
            follow_up = [{"field": f, "value": text(p.get(f))} for f in (*FOLLOW_UP_FIELDS, *follow_props)
                         if text(p.get(f)) is not None]
            data = {"title": text(p.get("dealname")), "pipeline_ref": ref("pipeline", pid),
                    "stage_ref": ref("stage", sid), "status": status,
                    "closed_at": closedate if status in ("won", "lost") else None,
                    "amount": parse_decimal(p.get("amount")), "currency": self._currency(p),
                    "expected_close_date": (_moment(closedate).astimezone(self.date_zone).date().isoformat()
                                            if closedate and status == "open" else None),
                    "owner_ref": self.owner_ref(p), "company_refs": self.refs("deals", r.id, "companies"),
                    "contact_refs": self.refs("deals", r.id, "contacts"),
                    "loss_reason_raw": text(p.get("closed_lost_reason")), "follow_up": follow_up}
            self.records["deal"].append(self.envelope(
                "deal", feed, r, data, self.properties("deals", p), source_object_type="deal",
                aliases=split_multi(p.get("hs_merged_object_ids"))))

    def tickets(self) -> None:
        feed, records = self.records_of("tickets")
        for r in records:
            p = r.properties
            pid, sid = text(p.get("hs_pipeline")), text(p.get("hs_pipeline_stage"))
            if not pid or not sid:
                raise AdapterError(f"{r.raw_ref}: ticket {r.id} has no hs_pipeline or hs_pipeline_stage. " + DOCS)
            closed = self.stage_outcome.get(pid, {}).get(sid) == "closed"
            data = {"pipeline_ref": ref("pipeline", pid), "stage_ref": ref("stage", sid),
                    "state": "closed" if closed else "open", "subject": text(p.get("subject")),
                    "description": html_to_text(p.get("content")),
                    "opened_at": parse_instant(p.get("createdate"), where=f"{r.raw_ref} createdate"),
                    "closed_at": parse_instant(p.get("closed_date"), where=f"{r.raw_ref} closed_date") if closed else None,
                    "owner_ref": self.owner_ref(p), "contact_refs": self.refs("tickets", r.id, "contacts"),
                    "company_refs": self.refs("tickets", r.id, "companies")}
            self.records["relationship"].append(self.envelope(
                "relationship", feed, r, data, self.properties("tickets", p), source_object_type="ticket"))

    # -- activity ------------------------------------------------------------------
    def tasks(self) -> None:
        feed, records = self.records_of("tasks")
        kinds = {**TASK_KINDS, **self.mapping.get("task", {}).get("kinds", {})}
        for r in records:
            p = r.properties
            status = TASK_STATUSES.get(str(p.get("hs_task_status") or "NOT_STARTED").upper(), "open")
            completed = parse_instant(p.get("hs_task_completion_date"), where=f"{r.raw_ref} completion")
            if status == "done" and completed is None:
                # HubSpot leaves the completion date unset on some old tasks; the last
                # change is the closest recorded instant (hs_task_status stays in properties).
                completed = parse_instant(r.updated_at or r.created_at, where=f"{r.raw_ref} updated")
            kind = kinds.get(str(p.get("hs_task_type") or "TODO").upper(), "todo")
            data = {"title": text(p.get("hs_task_subject")), "body_text": html_to_text(p.get("hs_task_body")),
                    "kind": kind if is_key(kind) else "todo", "status": status,
                    "priority": TASK_PRIORITIES.get(str(p.get("hs_task_priority") or "").upper()),
                    "due_at": parse_instant(p.get("hs_timestamp"), where=f"{r.raw_ref} hs_timestamp"),
                    "due_is_date": False, "completed_at": completed if status == "done" else None,
                    "owner_ref": self.owner_ref(p),
                    "target_refs": self.targets("tasks", r.id, ("contacts", "companies", "deals", "tickets"))}
            self.records["task"].append(self.envelope(
                "task", feed, r, data, self.properties("tasks", p), source_object_type="task"))

    def communications(self) -> None:
        seen: dict[str, str] = {}
        for obj in COMMUNICATIONS:
            feed, records = self.records_of(obj)
            for r in records:
                if r.id in seen:
                    raise AdapterError(
                        f"{obj} {r.id} has the id of {seen[r.id]} {r.id} — cause: HubSpot activities share one "
                        "id space, so this export is damaged. " + DOCS)
                seen[r.id] = obj
                self.records["communication"].append(self._communication(obj, feed, r))

    def _communication(self, obj: str, feed: Feed, r) -> dict:
        p = r.properties
        where = r.raw_ref
        body_html = text(p.get(BODY[obj]))
        if obj == "emails":
            body_text = text(p.get("hs_email_text")) or html_to_text(body_html)
        else:
            body_text = html_to_text(body_html)
        if body_text is None and text(p.get("hs_body_preview")):
            body_text = text(p.get("hs_body_preview"))
            if parse_bool(p.get("hs_body_preview_is_truncated")):
                self.truncated_bodies += 1
        occurred = parse_instant(p.get("hs_timestamp"), where=f"{where} hs_timestamp")
        participants: list[dict] = []
        direction, status, subject, ended, duration = None, None, None, None, None
        targets = self.targets(obj, r.id, ("contacts", "companies", "deals", "tickets"))
        if obj == "notes":
            kind, direction, status = "note", "internal", "completed"
        elif obj == "meetings":
            kind = "meeting"
            subject = text(p.get("hs_meeting_title"))
            occurred = parse_instant(p.get("hs_meeting_start_time"), where=f"{where} start") or occurred
            ended = parse_instant(p.get("hs_meeting_end_time"), where=f"{where} end")
            status = MEETING_STATUSES.get(str(p.get("hs_meeting_outcome") or "").upper())
            staff_only = text(p.get("hs_attendee_owner_ids")) and not any(
                t["object_type"] in ("contact", "company") for t in targets)
            direction = "internal" if staff_only else "outbound"
        elif obj == "calls":
            kind = "call"
            subject = text(p.get("hs_call_title"))
            direction = CALL_DIRECTIONS.get(str(p.get("hs_call_direction") or "").upper())
            status = CALL_STATUSES.get(str(p.get("hs_call_status") or "").upper())
            ms = text(p.get("hs_call_duration"))
            duration = int(float(ms)) // 1000 if ms and ms.replace(".", "", 1).isdigit() else None
            for role, prop in (("caller", "hs_call_from_number"), ("callee", "hs_call_to_number")):
                if text(p.get(prop)):
                    participants.append({"role": role, "address": text(p.get(prop)), "name": None})
        else:
            kind = "email"
            subject = text(p.get("hs_email_subject"))
            direction = EMAIL_DIRECTIONS.get(str(p.get("hs_email_direction") or "").upper())
            status = "completed"
            participants = self._email_participants(p)
        if occurred is None:
            occurred = parse_instant(r.created_at, where=f"{where} created")
        if occurred is None:
            raise AdapterError(f"{where}: {obj} {r.id} has no hs_timestamp and no create date. " + DOCS)
        if ended is not None and _moment(ended) < _moment(occurred):
            ended = None
        raw = {"schema_version": RAW_SCHEMA_VERSION, "provider": "hubspot", "payload": r.payload or {}}
        data = {"kind": kind, "direction": direction, "status": status, "subject": subject,
                "body_text": body_text, "body_html": body_html if looks_like_html(body_html) else None,
                "summary": None, "occurred_at": occurred, "ended_at": ended, "duration_seconds": duration,
                "owner_ref": self.owner_ref(p), "participants": participants, "target_refs": targets, "raw": raw}
        return self.envelope("communication", feed, r, data, self.properties(obj, p),
                             source_object_type=SINGULAR[obj])

    @staticmethod
    def _email_participants(p: dict) -> list[dict]:
        out = []
        try:
            headers = json.loads(p["hs_email_headers"]) if text(p.get("hs_email_headers")) else None
        except ValueError:
            headers = None
        if isinstance(headers, dict):
            for role in ("from", "to", "cc", "bcc"):
                people = headers.get(role)
                for person in [people] if isinstance(people, dict) else (people or []):
                    if isinstance(person, dict) and text(person.get("email")):
                        name = " ".join(x for x in (text(person.get("firstName")), text(person.get("lastName"))) if x)
                        out.append({"role": role, "address": person["email"], "name": name or None})
            return out
        for role, prop in (("from", "hs_email_from_email"), ("to", "hs_email_to_email"), ("cc", "hs_email_cc_email"),
                           ("bcc", "hs_email_bcc_email")):
            for address in split_multi(p.get(prop)):
                out.append({"role": role, "address": address, "name": None})
        return out

    # -- consent -----------------------------------------------------------------------
    def _consent(self, contact, emails: list[dict]) -> None:
        p = contact.properties
        when = None
        for entry in sorted((contact.history or {}).get("hs_email_optout", []), key=lambda e: str(e.get("timestamp"))):
            if parse_bool(entry.get("value")) is True:
                when = parse_instant(entry.get("timestamp"), where=f"{contact.raw_ref} history")
        if parse_bool(p.get("hs_email_optout")) is True:
            for email in emails:
                self._consent_record(contact, f"optout:{contact.id}:{address_hash(email['address'])}", "contact", {
                    "address": email["address"], "channel": "email", "purpose": "marketing", "state": "withdrawn",
                    "suppress": "opt_out", "occurred_at": when, "subscription": None,
                    "evidence": "hs_email_optout=true (opted out of all email)"}, {})
        bounce = text(p.get("hs_email_hard_bounce_reason_enum"))
        primary = next((e["address"] for e in emails if e["primary"]), None)
        if bounce and primary:
            self._consent_record(contact, f"bounce:{contact.id}:{address_hash(primary)}", "contact", {
                "address": primary, "channel": "email", "purpose": None, "state": None, "suppress": "hard_bounce",
                "occurred_at": None, "subscription": None, "evidence": f"hs_email_hard_bounce_reason_enum={bounce}"}, {})

    def _consent_record(self, contact, source_id: str, source_object_type: str, data: dict, props: dict) -> None:
        self.records["consent"].append(make_record(
            "consent", source=self.source, source_id=source_id, source_object_type=source_object_type,
            archived=bool(contact is not None and contact.archived), data=data, properties=props,
            raw_ref=contact.raw_ref if contact is not None else None))

    def subscriptions(self) -> None:
        seen: dict[str, str] = {}
        for item, raw_ref in self.b.subscriptions:
            if not isinstance(item, dict) or not text(item.get("recipient")):
                raise AdapterError(f"{raw_ref} is not a subscription status response (no recipient). " + DOCS)
            recipient = item["recipient"]
            for i, status in enumerate(item.get("subscriptionStatuses") or []):
                if not isinstance(status, dict) or status.get("id") in (None, ""):
                    continue
                state = {"SUBSCRIBED": "granted", "NOT_SUBSCRIBED": "withdrawn"}.get(str(status.get("status")).upper())
                if state is None:
                    continue
                evidence = {k: status[k] for k in ("legalBasis", "legalBasisExplanation", "sourceOfStatus")
                            if status.get(k) not in (None, "")}
                source_id = f"subscription:{status['id']}:{address_hash(recipient)}"
                if source_id in seen:
                    # The same address read twice (two contacts share it, or batches overlapped).
                    if seen[source_id] != state:
                        raise AdapterError(
                            f"{raw_ref}: subscription {status['id']} has two different statuses for one address "
                            "— cause: the recording changed during the crawl. Fix: re-record the statuses. " + DOCS)
                    continue
                seen[source_id] = state
                self.records["consent"].append(make_record(
                    "consent", source=self.source,
                    source_id=source_id,
                    source_object_type="subscription_status",
                    data={"address": recipient, "channel": "email", "purpose": "marketing", "state": state,
                          "suppress": None, "occurred_at": parse_instant(status.get("timestamp"), where=raw_ref),
                          "subscription": {"id": str(status["id"]), "name": text(status.get("name"))},
                          "evidence": json.dumps(evidence, sort_keys=True) if evidence else None},
                    properties=dict(sorted(status.items())),
                    raw_ref=f"{raw_ref}/subscriptionStatuses/{i}"))

    # -- associations and property definitions -------------------------------------------
    def associations(self) -> None:
        for a, b in CORE_PAIRS:
            pair = self.b.pairs.get((a, b))
            if pair is None:
                continue
            for (a_id, b_id), link in sorted(pair.links.items(), key=lambda x: (id_key(x[0][0]), id_key(x[0][1]))):
                labels = sorted(link.labels)
                props = {}
                if labels:
                    props["labels"] = labels
                if link.type_ids:
                    props["type_ids"] = sorted(link.type_ids)
                self.records["association"].append(make_record(
                    "association", source=self.source,
                    source_id=f"{SINGULAR[a]}:{a_id}/{SINGULAR[b]}:{b_id}",
                    source_object_type=f"{SINGULAR[a]}_to_{SINGULAR[b]}",
                    data={"from_ref": ref(CANONICAL[a], a_id), "to_ref": ref(CANONICAL[b], b_id),
                          "label": labels[0] if labels else (sorted(link.type_ids)[0] if link.type_ids else None),
                          "primary": link.primary},
                    properties=props, raw_ref=None))

    def property_defs(self) -> None:
        done: set[tuple[str, str]] = set()
        for obj in CANONICAL:
            otype = CANONICAL[obj]
            for item, raw_ref in self.b.properties.get(obj, []):
                if not isinstance(item, dict) or not text(item.get("name")) or not text(item.get("type")):
                    raise AdapterError(f"{raw_ref} is not a HubSpot property definition (name, type). " + DOCS)
                key = (otype, item["name"])
                if key in done:
                    continue  # notes, meetings, calls and emails share names; the first definition is kept
                done.add(key)
                options = [{"value": str(o.get("value", "")), "label": str(o.get("label", ""))}
                           for o in sorted((o for o in item.get("options") or [] if isinstance(o, dict)),
                                           key=lambda o: (int(o.get("displayOrder") or 0), str(o.get("value"))))]
                props = {k: v for k, v in sorted(item.items())
                         if k not in ("name", "label", "type", "fieldType", "options", "groupName", "hubspotDefined")
                         and v is not None}
                self.records["property_def"].append(make_record(
                    "property_def", source=self.source, source_id=f"{SINGULAR[obj]}.{item['name']}",
                    source_object_type="property",
                    data={"object_type": otype, "name": item["name"], "label": str(item.get("label") or ""),
                          "type": item["type"], "field_type": text(item.get("fieldType")), "options": options,
                          "group": text(item.get("groupName")), "provider_defined": item.get("hubspotDefined") is True},
                    properties=props, raw_ref=raw_ref))

    def run(self) -> dict[str, list[dict]]:
        self.owners()
        self.pipelines()
        self.companies()
        self.contacts()
        self.deals()
        self.tickets()
        self.tasks()
        self.communications()
        self.subscriptions()
        self.associations()
        self.property_defs()
        return self.records

    # -- coverage ------------------------------------------------------------------------
    def coverage(self) -> dict[str, dict[str, Any]]:
        feeds = self.feeds
        objects: dict[str, dict[str, Any]] = {}

        def entry(statuses: list[Status], **extra) -> dict:
            reasons = sorted({r for s in statuses for r in s.reasons})
            if not statuses or all(not s.enumerated for s in statuses):
                coverage = "unknown"
                if not reasons:
                    reasons = [("file_absent", "nothing was exported")]
            else:
                coverage = "partial" if reasons else "complete"
            out = {"coverage": coverage, **{k: v for k, v in extra.items() if v is not None}}
            if reasons:
                out["reasons"] = [{"code": c, "detail": d} for c, d in reasons]
            if coverage == "unknown":
                out.pop("declared_total", None)
                out.pop("history", None)
                out.pop("archived", None)
            return out

        def from_feeds(objs: tuple[str, ...]) -> dict:
            group = [feeds[o] for o in objs]
            enumerated = [f for f in group if f.status.enumerated]
            archived = "enumerated" if enumerated and all(f.archived == "enumerated" for f in enumerated) \
                else "not_enumerated"
            totals = [f.declared_total for f in group]
            declared = sum(totals) if all(t is not None for t in totals) else None
            hist = [f.history_props for f in enumerated if f.history_exported]
            history = sorted(set.intersection(*hist)) if hist and len(hist) == len(enumerated) else None
            statuses = [f.status for f in group]
            if objs == COMMUNICATIONS and self.truncated_bodies:
                extra = Status()
                extra.add("truncated_export", f"{self.truncated_bodies} communication(s) hold only a truncated "
                                              "body preview (hs_body_preview_is_truncated); the full body was not "
                                              "exported")
                statuses.append(extra)
            return entry(statuses, archived=archived, declared_total=declared, history=history)

        owner_archived = self.b.owners_archived if self.b.owner_status.enumerated else None
        objects["owner"] = entry([self.b.owner_status], archived=owner_archived)
        pipeline_statuses = [s for obj, s in self.b.pipeline_status.items()
                             if s.enumerated or feeds[obj].status.enumerated]
        objects["pipeline"] = entry(pipeline_statuses, archived="not_applicable")
        objects["company"] = from_feeds(("companies",))
        objects["contact"] = from_feeds(("contacts",))
        objects["deal"] = from_feeds(("deals",))
        objects["relationship"] = from_feeds(("tickets",))
        objects["task"] = from_feeds(("tasks",))
        objects["communication"] = from_feeds(COMMUNICATIONS)

        pair_statuses = []
        for a, b in PAIRS:
            if not (feeds[a].status.enumerated and feeds[b].status.enumerated):
                continue
            pair = self.b.pairs.get((a, b)) or PairFeed(a, b)
            status = Status(reasons=list(pair.status.reasons))
            if not pair.exported and not any(c == "forbidden" for c, _ in status.reasons):
                status.add("pair_not_exported", f"{SINGULAR[a]}→{SINGULAR[b]}: no association pages "
                                                f"(crawl/associations/{a}-{b}/ or ?associations={b} on the "
                                                f"{a} pages)")
            pair_statuses.append(status)
        objects["association"] = entry(pair_statuses or [Status()], archived="not_applicable")

        contact = feeds["contacts"].status
        consent = Status(enumerated=contact.enumerated)
        for code, detail in contact.reasons:
            consent.add(code, f"contacts: {detail}")
        subs = self.b.subscription_status
        for code, detail in subs.reasons:
            consent.add(code, detail)
        if not subs.enumerated and not subs.reasons:
            consent.add("file_absent", "no subscription statuses: a file export has none, so per-subscription "
                                       "grants and withdrawals are unknown")
        elif subs.enumerated:
            read = {str(item.get("recipient", "")).strip().lower() for item, _ in self.b.subscriptions
                    if isinstance(item, dict)}
            addresses = {e["address"].strip().lower() for r in self.records["contact"] if not r["archived"]
                         for e in r["data"]["emails"]}
            unread = addresses - read
            if unread:
                consent.add("count_mismatch", f"subscription statuses were recorded for {len(addresses) - len(unread)} "
                                              f"of {len(addresses)} live contact addresses; the rest are unknown")
        objects["consent"] = entry([consent], archived="not_applicable")

        prop_statuses = [s for obj, s in self.b.property_status.items()
                         if s.enumerated or feeds[obj].status.enumerated]
        objects["property_def"] = entry(prop_statuses, archived="not_applicable")
        return objects
