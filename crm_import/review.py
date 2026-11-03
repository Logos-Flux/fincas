"""The review queue's services (docs/crm/phase5-importer.md 4.6-4.9; step 9).

The screens (``crm_import.erp_views``) call these; nothing here knows HTTP.

**The queue** of one batch (``queue_rows``): confidence ascending, then source
order (``?order=desc`` flips both), so conflicts (0.0) and the weak matches
come first. Filters: state (default: undecided), decision, confidence band,
one flag, "AI disagrees" and "assigned to me". Pagination is a keyset cursor
on ``(confidence, row_number)`` (``after=<confidence>:<row number>``), so page
200 costs what page 1 does.

**Queries.** A queue page costs the same number of queries whatever its size
(``tests.test_review_views``): the header counts are one aggregate plus one
per-actor count; the page is one query; the golden side of every row on the
page (targets and candidates) is one query per target model, plus the
contact points of parties (``present``).

**Claims** (4.9): ``proposed_mapping["claim"] = {"by": <username>, "until":
<epoch seconds>}``, set by "Claim next 50" over the undecided, unclaimed rows
of the current view, lapsing after ``CLAIM_TTL``. A JSON key, not a column:
no migration, and re-running matching (which rewrites the proposal) keeps
the claim (``rematch``). A row another reviewer holds a live claim on is
never decided by someone else: neither one at a time nor in bulk
(``ClaimHeld``).

**Bulk actions** (4.8) each record their actor on every row
(``mapping_actor``) and write one ``AuditLog`` row with the counts. They read
only rows of the batch they were called on, so an id from another batch or
tenant is ignored, never decided.

**Personal data.** Source values are shown in full only with
``crm_import.view_import_pii``; without it addresses and numbers are masked
(``j***@example.com``, ``***0143``) and everything else that is a value
(addresses, job titles, properties, the field diff, the raw record) is
withheld. Display names are always shown: the reviewer needs them to
recognise a person, and staff see party names everywhere in the back office.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.db import transaction
from django.db.models import Count, DecimalField, Q, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from parties.erp_views import mask_address
from unify.models import StagedRow

from . import compare, match
from . import stage as staging

MS = StagedRow.MappingState

PAGE_SIZE = 50
CLAIM_BATCH = 50
CLAIM_TTL = dt.timedelta(hours=1)
THRESHOLD = match.AUTO_APPROVE_THRESHOLD  # 0.95, shared with products (I7)
MID_FLOOR = Decimal("0.600")

#: ``?state=``: what the queue shows; "undecided" is the default.
STATES: dict[str, tuple[str, ...] | None] = {
    "undecided": (MS.PROPOSED,),
    "approved": (MS.APPROVED,),
    "applied": (MS.APPLIED,),
    "failed": (MS.FAILED,),
    "skipped": (MS.NA,),
    "all": None,
}
STATE_LABELS = {"undecided": "Undecided", "approved": "Approved", "applied": "Applied", "failed": "Failed",
                "skipped": "Skipped (N/A)", "all": "All rows"}
DECISIONS = (match.CREATE, match.LINK, match.UPDATE, match.CONFLICT, "none")
DECISION_LABELS = {"create": "Create", "link": "Link", "update": "Update", "conflict": "Conflict",
                   "none": "No proposal"}
BANDS = ("high", "mid", "low")
BAND_LABELS = {"high": "≥ 0.95", "mid": "0.60–0.95", "low": "< 0.60"}
FLAGS = tuple(match.FLAGS)
FLAG_AI_DISAGREES = "ai_disagrees"
#: Reviewer N/A reasons (4.8: "a required reason from a short list").
SKIP_REASONS = ((match.REJECTED, "Not wanted"), (match.OUT_OF_SCOPE, "Out of scope"))
DECIDED = (MS.APPROVED, MS.APPLIED, MS.NA, MS.FAILED)
SYSTEM_ACTOR_PREFIX = "system:"

FLAG_TEXT = {
    "needs_individual_review": "Staff, login or account holder",
    "outcome_conflict": "Outcome conflict",
    "party_ambiguous": "Party ambiguous",
    "phone_only": "Phone only",
    "archived_owner": "Archived owner",
    "closed_at_inferred": "Close date inferred",
    "owner_unresolved": "Owner unresolved",
    "ai_disagrees": "AI disagrees",
    "ambiguous_match": "Several candidates",
    "name_mismatch": "Names disagree",
    "party_already_linked": "Already linked",
    "shared_target": "Shared target",
    "ref_target_missing": "Target missing",
    "stage_keys_differ": "Stages differ",
    "stage_outcome_closed": "Closed stage",
    "competing_candidate": "Competing candidate",
    "role_address": "Role address",
    "erased_contact_point_removed": "Erased number removed",
}
NA_TEXT = {
    "unchanged": "unchanged since the last import",
    "erased": "an erased person's record",
    "archived_in_source": "archived in the source",
    "out_of_scope": "out of scope",
    "rejected": "rejected by a reviewer",
    "duplicate_in_export": "the same record twice in the export",
}
RULE_TEXT = {
    "R1": "same email {value}",
    "R2": "same phone {value} and family name",
    "R3": "same phone {value}",
    "R4": "same name and postal code",
    "R5": "same organization name {value}",
    "R6": "same domain {value}",
    "owner_email": "same email {value}",
    "pipeline_key": "same pipeline key",
}


class ReviewError(ValueError):
    """A decision the queue refuses. The message is shown to the reviewer."""


class ClaimHeld(ReviewError):
    pass


class CursorError(ValueError):
    pass


# --- filters --------------------------------------------------------------------------


@dataclass
class Filters:
    state: str = "undecided"
    decision: str = ""
    band: str = ""
    flag: str = ""
    mine: bool = False
    order: str = "asc"
    after: str = ""

    @classmethod
    def from_query(cls, query) -> Filters:
        def pick(name, allowed, default=""):
            value = (query.get(name) or "").strip()
            return value if value in allowed else default

        return cls(
            state=pick("state", STATES, "undecided"),
            decision=pick("decision", DECISIONS),
            band=pick("band", BANDS),
            flag=pick("flag", FLAGS),
            mine=query.get("mine") == "1",
            order=pick("order", ("asc", "desc"), "asc"),
            after=(query.get("after") or "").strip(),
        )

    @property
    def narrowed(self) -> bool:
        """Anything beyond the default view (the "no rows match" empty state)."""
        return bool(self.decision or self.band or self.flag or self.mine or self.state != "undecided")

    def query(self, **changes) -> str:
        """The query string of this view (``after`` dropped unless given)."""
        from django.http import QueryDict

        values = {"state": self.state, "decision": self.decision, "band": self.band, "flag": self.flag,
                  "mine": "1" if self.mine else "", "order": self.order, "after": ""}
        values.update(changes)
        q = QueryDict(mutable=True)
        for key, value in values.items():
            if value and not (key == "state" and value == "undecided") and not (key == "order" and value == "asc"):
                q[key] = value
        return q.urlencode()


def flag_q(flag: str) -> Q:
    # A JSON array's text holds each flag in quotes; flag names are never
    # substrings of each other once quoted. Works on SQLite and Postgres
    # (JSON ``contains`` does not exist on SQLite).
    return Q(proposed_mapping__flags__icontains=f'"{flag}"')


# SQL negation of a JSON key lookup is NULL when the key is missing (NOT NULL
# is NULL), so a "not X" filter is spelled positively: missing, null, or the
# negated lookup. Never ``~Q(key=…)`` or ``.exclude(key=…)`` alone.


def unflagged_q() -> Q:
    """No flag at all: an empty array has no quote character."""
    return (Q(proposed_mapping__flags__isnull=True) | Q(proposed_mapping__flags=None)
            | ~Q(proposed_mapping__flags__icontains='"'))


def not_rule_q(rule: str) -> Q:
    return (Q(proposed_mapping__rule__isnull=True) | Q(proposed_mapping__rule=None)
            | ~Q(proposed_mapping__rule=rule))


def no_ai_disagreement_q() -> Q:
    return ((Q(proposed_mapping__ai__isnull=True) | Q(proposed_mapping__ai__disagrees__isnull=True)
             | Q(proposed_mapping__ai__disagrees=False))
            & (Q(proposed_mapping__flags__isnull=True) | Q(proposed_mapping__flags=None) | ~flag_q(FLAG_AI_DISAGREES)))


def decision_q(decision: str) -> Q:
    if decision == "none":
        return Q(proposed_mapping__decision=None) | Q(proposed_mapping__decision__isnull=True)
    return Q(proposed_mapping__decision=decision)


def band_q(band: str) -> Q:
    if band == "high":
        return Q(mapping_confidence__gte=THRESHOLD)
    if band == "mid":
        return Q(mapping_confidence__gte=MID_FLOOR, mapping_confidence__lt=THRESHOLD)
    return Q(mapping_confidence__lt=MID_FLOOR) | Q(mapping_confidence__isnull=True)


def epoch(now=None) -> int:
    return int((now or timezone.now()).timestamp())


def live_claim_q(now=None) -> Q:
    return Q(proposed_mapping__claim__until__gte=epoch(now))


def claimed_by_q(username: str, now=None) -> Q:
    return Q(proposed_mapping__claim__by=username) & live_claim_q(now)


def held_by_others_q(username: str, now=None) -> Q:
    return live_claim_q(now) & ~Q(proposed_mapping__claim__by=username)


def unclaimed_q(now=None) -> Q:
    return (Q(proposed_mapping__claim__isnull=True) | Q(proposed_mapping__claim=None)
            | Q(proposed_mapping__claim__until__lt=epoch(now)))


def free_for_q(username: str, now=None) -> Q:
    """Rows ``username`` may decide: unclaimed, lapsed, or their own claim."""
    return unclaimed_q(now) | Q(proposed_mapping__claim__by=username)


def ai_disagrees_q() -> Q:
    return Q(proposed_mapping__ai__disagrees=True) | flag_q(FLAG_AI_DISAGREES)


def filtered(batch, filters: Filters, *, username: str, now=None):
    """The batch's rows in this view, annotated ``conf`` (confidence, NULL as 0)."""
    qs = StagedRow.objects.filter(batch=batch).annotate(
        conf=Coalesce("mapping_confidence", Value(Decimal("0")), output_field=DecimalField(max_digits=4,
                                                                                           decimal_places=3)))
    states = STATES[filters.state]
    if states is not None:
        qs = qs.filter(mapping_state__in=states)
    if filters.decision:
        qs = qs.filter(decision_q(filters.decision))
    if filters.band:
        qs = qs.filter(band_q(filters.band))
    if filters.flag == FLAG_AI_DISAGREES:
        qs = qs.filter(ai_disagrees_q())
    elif filters.flag:
        qs = qs.filter(flag_q(filters.flag))
    if filters.mine:
        qs = qs.filter(claimed_by_q(username, now))
    return qs


def parse_cursor(value: str) -> tuple[Decimal, int] | None:
    if not value:
        return None
    try:
        conf, number = value.split(":", 1)
        return Decimal(conf).quantize(Decimal("0.001")), int(number)
    except (ValueError, InvalidOperation) as exc:
        raise CursorError(value) from exc


def queue_rows(batch, filters: Filters, *, username: str, now=None, size: int | None = None):
    """``(rows, next_cursor)``: one page of the view in queue order (one query)."""
    size = size or PAGE_SIZE
    qs = filtered(batch, filters, username=username, now=now)
    cursor = parse_cursor(filters.after)
    desc = filters.order == "desc"
    if cursor is not None:
        conf, number = cursor
        if desc:
            qs = qs.filter(Q(conf__lt=conf) | Q(conf=conf, row_number__lt=number))
        else:
            qs = qs.filter(Q(conf__gt=conf) | Q(conf=conf, row_number__gt=number))
    qs = qs.order_by("-conf", "-row_number") if desc else qs.order_by("conf", "row_number")
    rows = list(qs[: size + 1])
    more = len(rows) > size
    rows = rows[:size]
    nxt = f"{rows[-1].conf}:{rows[-1].row_number}" if more and rows else ""
    return rows, nxt


# --- the progress header (4.9) ------------------------------------------------------------


def header(batch, *, username: str, now=None, today_start=None, session_start=None) -> dict:
    """Counts for the queue header, the chips and "Your progress" (three queries)."""
    now = now or timezone.now()
    today_start = today_start or now.replace(hour=0, minute=0, second=0, microsecond=0)
    p = Q(mapping_state=MS.PROPOSED)
    decided = Q(mapping_state__in=DECIDED)
    human = ~Q(mapping_actor__startswith=SYSTEM_ACTOR_PREFIX) & ~Q(mapping_actor="")
    # The JSON-reading counts run over the undecided rows only; the state and
    # progress counts over every row (both one pass, no JSON for the second).
    pagg = {
        "conflicts": Count("id", filter=decision_q(match.CONFLICT)),
        **{f"band_{b}": Count("id", filter=band_q(b)) for b in BANDS},
        **{f"decision_{d}": Count("id", filter=decision_q(d)) for d in DECISIONS},
        **{f"flag_{f}": Count("id", filter=ai_disagrees_q() if f == FLAG_AI_DISAGREES else flag_q(f)) for f in FLAGS},
        "mine": Count("id", filter=claimed_by_q(username, now)),
        "held_by_others": Count("id", filter=held_by_others_q(username, now)),
    }
    agg = {
        "total": Count("id"),
        **{f"state_{s}": Count("id", filter=Q(mapping_state=s)) for s in MS.values},
        "you_today": Count("id", filter=decided & Q(mapping_actor=username, updated_at__gte=today_start)),
        "everyone_today": Count("id", filter=decided & human & Q(updated_at__gte=today_start)),
    }
    if session_start is not None:
        agg["you_session"] = Count("id", filter=decided & Q(mapping_actor=username, updated_at__gte=session_start))
    c = StagedRow.objects.filter(batch=batch).aggregate(**agg)
    c.update(StagedRow.objects.filter(p, batch=batch).aggregate(**pagg))
    by_actor = list(
        StagedRow.objects.filter(Q(batch=batch) & decided & human & Q(updated_at__gte=today_start))
        .values("mapping_actor").annotate(n=Count("id")).order_by("-n", "mapping_actor")[:6]
    )
    total = c["total"]
    remaining = c[f"state_{MS.PROPOSED}"] + c[f"state_{MS.UNMAPPED}"]
    approved = c[f"state_{MS.APPROVED}"] + c[f"state_{MS.APPLIED}"]
    skipped = c[f"state_{MS.NA}"]
    failed = c[f"state_{MS.FAILED}"]
    decided_n = total - remaining

    def pct(n):
        return round(100 * n / total, 1) if total else 0

    return {
        "total": total, "decided": decided_n, "remaining": remaining, "percent": int(pct(decided_n)),
        "approved": approved, "approved_pending": c[f"state_{MS.APPROVED}"], "applied": c[f"state_{MS.APPLIED}"],
        "skipped": skipped, "failed": failed, "conflicts": c["conflicts"],
        "bar": [("approved", "Approved", approved, pct(approved)), ("skipped", "Skipped", skipped, pct(skipped)),
                ("failed", "Failed", failed, pct(failed)), ("remaining", "To go", remaining, pct(remaining))],
        "bands": {b: c[f"band_{b}"] for b in BANDS},
        "decisions": {d: c[f"decision_{d}"] for d in DECISIONS},
        "flags": {f: c[f"flag_{f}"] for f in FLAGS if c[f"flag_{f}"]},
        "mine": c["mine"], "held_by_others": c["held_by_others"],
        "you_today": c["you_today"], "you_session": c.get("you_session"), "everyone_today": c["everyone_today"],
        "by_actor": [(item["mapping_actor"], item["n"]) for item in by_actor],
    }


def header_json(h: dict) -> dict:
    """What the decision POST returns: the new header counts (4.9)."""
    return {k: h[k] for k in ("total", "decided", "remaining", "percent", "approved", "skipped", "failed",
                              "conflicts", "mine", "you_today", "you_session", "everyone_today")}


def eligible_q(username: str, now=None) -> Q:
    """Rows "Approve all unflagged ≥ 0.95" may take (4.8). A display count:
    ``match.bulk_approvable`` decides row by row when the action runs."""
    return (Q(mapping_state=MS.PROPOSED, mapping_confidence__gte=THRESHOLD)
            & Q(proposed_mapping__decision__in=[match.CREATE, match.LINK, match.UPDATE])
            & unflagged_q() & not_rule_q("R6") & no_ai_disagreement_q() & free_for_q(username, now))


# --- presentation -----------------------------------------------------------------------


def source_name(otype: str, raw: dict, *, can_pii: bool = True) -> str:
    """The source record's display name (never an address unmasked; a
    communication's or ticket's subject only with the PII permission)."""
    if match.is_erased(raw):
        return "Erased record"
    data = raw.get("data") or {}
    sid = raw.get("source_id") or ""
    if otype == "contact":
        name = " ".join(x for x in (data.get("given_name"), data.get("family_name")) if x)
        if not name:
            emails = [e.get("address") for e in data.get("emails") or [] if e.get("address")]
            name = mask_address(emails[0]) if emails else ""
        return name or f"Contact {sid}"
    if otype == "company":
        return data.get("name") or (data.get("domains") or [""])[0] or f"Company {sid}"
    if otype == "owner":
        return data.get("display_name") or f"Owner {sid}"
    if otype == "pipeline":
        return data.get("name") or data.get("key") or f"Pipeline {sid}"
    if otype in ("deal", "task"):
        return data.get("title") or f"{otype.title()} {sid}"
    if otype == "relationship":
        return (data.get("subject") if can_pii else None) or f"Ticket {sid}"
    if otype == "communication":
        return (data.get("subject") if can_pii else None) or f"{(data.get('kind') or 'note').title()} {sid}"
    if otype == "consent":
        address = data.get("address") or ""
        what = data.get("purpose") or data.get("suppress") or ""
        return f"{mask_address(address) if address else sid} · {data.get('channel') or ''} {what}".strip()
    if otype == "association":
        a, b = data.get("from_ref") or {}, data.get("to_ref") or {}
        return f"{a.get('object_type')} {a.get('source_id')} → {b.get('object_type')} {b.get('source_id')}"
    return f"{otype} {sid}"


def _model(label: str):
    try:
        return apps.get_model(label)
    except (LookupError, ValueError):
        return None


def load_targets(rows, company) -> dict[tuple[str, str], object]:
    """Every target and candidate the page names, keyed ``(model, id)``:
    one query per model (plus contact points for parties)."""
    wanted: dict[str, set[str]] = {}
    for row in rows:
        proposal = row.proposed_mapping or {}
        target = proposal.get("target") or {}
        model = target.get("model") or _default_model(row)
        if target.get("id"):
            wanted.setdefault(model, set()).add(str(target["id"]))
        for cand in proposal.get("candidates") or []:
            if cand.get("id"):
                wanted.setdefault(model, set()).add(str(cand["id"]))
    out: dict[tuple[str, str], object] = {}
    for label, ids in wanted.items():
        model = _model(label)
        if model is None:
            continue
        qs = model._base_manager.filter(pk__in=list(ids))
        if any(f.name == "company" for f in model._meta.get_fields()):
            qs = qs.filter(company=company)  # a candidate is always the job's tenant's
        if label == match.PARTY_MODEL:
            qs = qs.prefetch_related("emails", "phones", "addresses")
        elif label == match.PROFILE_MODEL:
            qs = qs.select_related("user")
        elif label == match.PIPELINE_MODEL:
            qs = qs.prefetch_related("stages")
        for obj in qs:
            out[(label, str(obj.pk))] = obj
    return out


def _default_model(row) -> str:
    otype = row_object_type(row)
    return {"owner": match.PROFILE_MODEL, "pipeline": match.PIPELINE_MODEL}.get(otype) or \
        compare.TARGET_MODELS.get(otype) or match.PARTY_MODEL


def row_object_type(row) -> str:
    return row.batch.entity_kind.removeprefix("crm_") if hasattr(row, "batch") and row.batch_id else ""


def target_name(obj) -> str:
    if obj is None:
        return "a record that is gone"
    for attr in ("display_name", "name", "title", "subject", "number"):
        value = getattr(obj, attr, "")
        if value:
            return str(value)
    return str(obj)


def _join(values) -> str:
    return ", ".join(str(v) for v in values if v not in (None, ""))


def _mask_all(values) -> list[str]:
    return [mask_address(str(v)) for v in values if v]


HIDDEN = "Hidden (needs PII permission)"


def comparison(otype: str, raw: dict, golden, *, can_pii: bool) -> list[dict]:
    """The side-by-side table: one line per field, source next to golden.
    ``differs`` drives emphasis (agreeing cells muted, as the merge review)."""
    data = raw.get("data") or {}
    lines: list[tuple[str, object, object, str]] = []  # label, source, golden, kind (name/point/value)
    if otype == "contact":
        emails = [e.get("address") for e in data.get("emails") or [] if e.get("address")]
        phones = _phones(raw)
        postcodes = [a.get("postal_code") for a in data.get("addresses") or [] if a.get("postal_code")]
        g_emails = [e.email for e in golden.emails.all()] if golden is not None else None
        g_phones = [p.number_e164 or p.number_raw for p in golden.phones.all()] if golden is not None else None
        g_post = [a.postal_code for a in golden.addresses.all() if a.postal_code] if golden is not None else None
        lines += [
            ("Given name", data.get("given_name"), getattr(golden, "given_name", None), "name"),
            ("Family name", data.get("family_name"), getattr(golden, "family_name", None), "name"),
            ("Emails", emails, g_emails, "point"),
            ("Phones", phones, g_phones, "point"),
            ("Postal codes", postcodes, g_post, "value"),
            ("Job title", data.get("job_title"), None, "value"),
        ]
    elif otype == "company":
        phones = _phones(raw)
        lines += [
            ("Name", data.get("name"), getattr(golden, "display_name", None), "name"),
            ("Legal name", data.get("legal_name"), getattr(golden, "legal_name", None), "name"),
            ("Domains", data.get("domains") or [], [golden.primary_domain] if getattr(golden, "primary_domain", "")
             else ([] if golden is not None else None), "name"),
            ("Phones", phones, [p.number_e164 or p.number_raw for p in golden.phones.all()]
             if golden is not None else None, "point"),
            ("Registration number", data.get("registration_number"), getattr(golden, "registration_number", None),
             "value"),
            ("Tax id", data.get("tax_id"), getattr(golden, "tax_id", None), "value"),
        ]
    elif otype == "owner":
        user = getattr(golden, "user", None) if golden is not None else None
        lines += [
            ("Name", data.get("display_name"), getattr(golden, "display_name", None), "name"),
            ("Email", data.get("email"), getattr(user, "email", None) if golden is not None else None, "point"),
            ("Active in source", "yes" if data.get("active") else "no", None, "name"),
        ]
    elif otype == "pipeline":
        stages = [s.get("key") for s in data.get("stages") or []]
        g_stages = [s.key for s in golden.stages.all()] if golden is not None else None
        lines += [
            ("Name", data.get("name"), getattr(golden, "name", None), "name"),
            ("Key", data.get("key"), getattr(golden, "key", None), "name"),
            ("Stages", stages, g_stages, "name"),
        ]
    else:
        for key in ("title", "subject", "status", "kind", "state", "channel", "purpose", "amount", "currency",
                    "due_at", "occurred_at", "closed_at"):
            if key in data and data.get(key) not in (None, ""):
                kind = "value" if (key == "amount" or (key in ("subject", "title")
                                                      and otype in ("communication", "relationship"))) else "name"
                lines.append((key.replace("_", " ").capitalize(), data.get(key), None, kind))
    out = []
    for label, src, gold, kind in lines:
        src_list = src if isinstance(src, list) else None
        gold_list = gold if isinstance(gold, list) else None
        differs = golden is not None and _normal(src) != _normal(gold)
        out.append({
            "label": label,
            "source": _show(src_list if src_list is not None else src, kind, can_pii),
            "golden": "" if golden is None else _show(gold_list if gold_list is not None else gold, kind, can_pii),
            "differs": differs,
            "pii": kind != "name",
        })
    return out


def _phones(raw: dict) -> list[str]:
    """E.164 where the number parses (what golden stores), else the source text."""
    return [p.removeprefix("raw:") for p in compare.contact_phones(raw)]


def _normal(value):
    if isinstance(value, list):
        return sorted(str(v).strip().lower() for v in value if v)
    return str(value or "").strip().lower()


def _show(value, kind: str, can_pii: bool) -> str:
    if value in (None, "", []):
        return ""
    if can_pii or kind == "name":
        return _join(value) if isinstance(value, list) else str(value)
    if kind == "point":
        return _join(_mask_all(value if isinstance(value, list) else [value]))
    return HIDDEN


def field_diff(otype: str, raw: dict, proposal: dict, golden, *, can_pii: bool) -> list[dict]:
    """For ``update`` / ``conflict``: each field the staged diff names, old →
    new: golden's current value (read live, 3.3) and the source's, masked
    without the PII permission (4.4)."""
    fields = ((proposal.get("diff") or {}).get("fields") or {})
    if not fields:
        return []
    projected = compare.project(otype, raw) if not match.is_erased(raw) else {}
    columns = compare.GOLDEN_COLUMNS.get(otype, {})
    out = []
    for name, state in sorted(fields.items()):
        column = columns.get(name)
        gold = getattr(golden, column, None) if (golden is not None and column) else None
        src = projected.get(name)
        out.append({
            "field": name, "state": state, "conflict": state == compare.CONFLICT,
            "source": _value_text(src) if can_pii else mask_value(src),
            "golden": (_value_text(gold) if can_pii else mask_value(gold)) if column else "no golden column",
        })
    return out


def mask_value(value) -> str:
    """A diff value without the PII permission (4.4: "values masked"): an
    address or number as ``j***@example.com`` / ``***0143``, other text as its
    first character, anything else as dots. Enough to see that it changed."""
    if value in (None, "", [], {}):
        return "—"
    if isinstance(value, list):
        return ", ".join(mask_value(v) for v in value[:5]) + (" …" if len(value) > 5 else "")
    if isinstance(value, dict):
        return "•••"
    text = str(value)
    if "@" in text or (text.startswith("+") and text[1:].replace(" ", "").isdigit()) or text.startswith("raw:"):
        return mask_address(text.removeprefix("raw:"))
    if isinstance(value, str) and text[:1].isalpha():
        return f"{text[:1]}…"
    return "•••"


def _value_text(value) -> str:
    if value in (None, "", []):
        return "—"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[:300]
    return str(value)[:300]


def sentence(otype: str, proposal: dict, row, golden, type_label: str) -> str:
    """The decision as a sentence (4.6: "Link to Jane Doe — same email j…@example.com")."""
    decision = proposal.get("decision")
    evidence = proposal.get("evidence") or []
    rule = proposal.get("rule")
    if row.mapping_state == MS.NA or decision == match.NA:
        reason = proposal.get("reason") or ""
        return f"Not imported: {NA_TEXT.get(reason, reason or 'no reason recorded')}."
    if decision == match.LINK:
        ev = next((e for e in evidence if e.get("rule") == rule), evidence[0] if evidence else None)
        why = RULE_TEXT.get(rule or "", rule or "").format(value=(ev or {}).get("value", "")) if rule else ""
        return f"Link to {target_name(golden)}" + (f" — {why}" if why else "") + "."
    if decision == match.CREATE:
        return f"Create a new {type_label.lower()}."
    fields = sorted(((proposal.get("diff") or {}).get("fields") or {}).items())
    if decision == match.UPDATE:
        names = ", ".join(f for f, _s in fields) or "no field named"
        return f"Update {target_name(golden)}: the source changed {names}."
    if decision == match.CONFLICT:
        names = ", ".join(f for f, s in fields if s == compare.CONFLICT) or "a field"
        return f"Conflict on {target_name(golden)}: staff changed {names} since the last import."
    n = len(proposal.get("candidates") or [])
    if n:
        return f"No proposal: {n} candidate{'s' if n != 1 else ''} at the same rule. Pick one or create new."
    return "No proposal. Create new, or skip."


def present(rows, *, job, otype: str, type_label: str, can_pii: bool, username: str, now=None) -> list[dict]:
    """Everything a queue row renders, for one page (constant queries)."""
    now = now or timezone.now()
    targets = load_targets(rows, job.company)
    stamp = epoch(now)
    out = []
    for row in rows:
        proposal = row.proposed_mapping or {}
        raw = row.raw or {}
        model = (proposal.get("target") or {}).get("model") or _default_model(row)
        tid = (proposal.get("target") or {}).get("id")
        golden = targets.get((model, str(tid))) if tid else None
        suggestion = proposal.get("ai") or None
        ai_pick = str((suggestion or {}).get("suggested") or "")
        # The rule's order, with the AI's pick marked: reordering by the
        # suggestion (3.5) would change what "1" means and which candidate the
        # comparison shows, anchoring the reviewer on the model.
        stored = list((proposal.get("candidates") or [])[:5])
        candidates = []
        for i, cand in enumerate(stored, start=1):
            obj = targets.get((model, str(cand.get("id"))))
            candidates.append({"key": i, "id": cand.get("id"), "name": target_name(obj), "rule": cand.get("rule"),
                               "confidence": cand.get("confidence"),
                               "chosen": str(cand.get("id")) == str(tid), "ai_pick": str(cand.get("id")) == ai_pick})
        if suggestion is not None:
            picked = next((c for c in candidates if c["ai_pick"]), None)
            suggestion = {**suggestion, "label": "Create new" if ai_pick == "create" else (
                f"{picked['name']} (candidate {picked['key']})" if picked else "a candidate no longer listed")}
        compared = golden
        if compared is None and candidates and row.mapping_state == MS.PROPOSED:
            compared = targets.get((model, str(candidates[0]["id"])))
        claim = proposal.get("claim") or {}
        live = bool(claim) and int(claim.get("until") or 0) >= stamp
        flags = list(proposal.get("flags") or [])
        if (proposal.get("ai") or {}).get("disagrees") and FLAG_AI_DISAGREES not in flags:
            flags.append(FLAG_AI_DISAGREES)
        data = raw.get("data") or {}
        stages_closed = [s.get("key") for s in data.get("stages") or [] if s.get("outcome") == "closed"] \
            if otype == "pipeline" else []
        out.append({
            "row": row,
            "name": source_name(otype, raw, can_pii=can_pii),
            "fingerprint": fingerprint(proposal),
            "sentence": sentence(otype, proposal, row, golden, type_label),
            "decision": proposal.get("decision") or "none",
            "confidence": row.mapping_confidence,
            "flags": [(f, FLAG_TEXT.get(f, f.replace("_", " "))) for f in flags],
            "candidates": candidates,
            "compared_name": target_name(compared) if compared is not None else "",
            "comparison": comparison(otype, raw, compared, can_pii=can_pii) if not match.is_erased(raw) else [],
            "diff": field_diff(otype, raw, proposal, golden, can_pii=can_pii),
            "ai": suggestion,
            "claim": {"by": claim.get("by"), "until": dt.datetime.fromtimestamp(int(claim["until"]), tz=dt.UTC),
                      "mine": claim.get("by") == username} if live else None,
            "decidable": row.mapping_state == MS.PROPOSED or outcome_retry(row, otype),
            "skippable": row.mapping_state in (MS.PROPOSED, MS.APPROVED),
            "can_create": proposal.get("decision") not in (match.UPDATE, match.CONFLICT),
            "outcome_choice": otype in ("deal", "relationship") and (
                match.FLAG_OUTCOME_CONFLICT in flags or outcome_retry(row, otype)),
            "closed_stages": stages_closed,
            "error_code": (row.error or "").split(":", 1)[0] if row.error else "",
            "raw_json": json.dumps({"data": data, "properties": raw.get("properties") or {}}, indent=2,
                                   ensure_ascii=False, sort_keys=True, default=str)[:20000] if can_pii else "",
        })
    for item, nxt in zip(out, out[1:] + [None]):
        item["next_id"] = nxt["row"].pk if nxt is not None else ""
    return out


def fingerprint(proposal: dict) -> str:
    """What the reviewer saw: the decision, target, rule and candidates. A
    decision posted with another fingerprint is refused (the proposal changed
    under the reviewer: a re-match, a re-stage)."""
    import hashlib

    seen = {"decision": proposal.get("decision"), "target": proposal.get("target"), "rule": proposal.get("rule"),
            "candidates": [c.get("id") for c in proposal.get("candidates") or []]}
    return hashlib.sha256(json.dumps(seen, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --- decisions ---------------------------------------------------------------------------


def outcome_retry(row, otype: str) -> bool:
    """A deal (or ticket engagement) that failed ``outcome_conflict`` at apply:
    re-decided with an outcome (``stage.approve_row(outcome=…)``, 2.5)."""
    return (row.mapping_state == MS.FAILED and otype in ("deal", "relationship")
            and (row.error or "").startswith(f"invalid_value: {match.FLAG_OUTCOME_CONFLICT}"))


def check_claim(row, username: str, now=None) -> None:
    claim = (row.proposed_mapping or {}).get("claim") or {}
    if claim and claim.get("by") != username and int(claim.get("until") or 0) >= epoch(now):
        until = dt.datetime.fromtimestamp(int(claim["until"]), tz=dt.UTC)
        raise ClaimHeld(f"Row {row.row_number} is claimed by {claim['by']} until {until:%H:%M} UTC; "
                        "it is theirs to decide until the claim lapses.")


def locked_row(batch, pk):
    """The row, locked for the decision (``select_for_update``; a no-op on SQLite)."""
    return StagedRow.objects.select_for_update(of=("self",)).select_related("batch").get(batch=batch, pk=pk)


def decide(batch, pk, *, otype: str, actor: str, action: str, candidate: str | None = None,
           keep=(), take=(), outcome: str | None = None, stage_outcomes: dict | None = None,
           reason: str = match.REJECTED, seen: str | None = None, now=None) -> StagedRow:
    """One reviewer decision (4.6): ``approve``, ``pick`` (a candidate),
    ``create`` (create new instead / reject the link) or ``skip`` (N/A).
    ``seen`` is the proposal's ``fingerprint`` as the screen showed it."""
    with transaction.atomic():
        row = locked_row(batch, pk)
        check_claim(row, actor, now)
        if seen and action != "skip" and seen != fingerprint(row.proposed_mapping or {}):
            raise ReviewError(f"Row {row.row_number}'s proposal changed since the page was loaded (matching ran "
                              "again). Look at it again, then decide.")
        try:
            if action == "skip":
                if row.mapping_state == MS.APPROVED and apply_open(batch):
                    raise ReviewError(f"Row {row.row_number} is approved and an apply run of this step is open: "
                                      "it may be written right now. Skip it after the run.")
                if reason not in dict(SKIP_REASONS):
                    raise ReviewError("Choose why the row is skipped.")
                staging.skip_row(row, actor, reason)
            elif action in ("approve", "pick", "create"):
                if action == "pick" and not candidate:
                    raise ReviewError("Pick a candidate.")
                if otype == "pipeline" and action != "skip":
                    stage_outcomes = _stage_outcomes(row, stage_outcomes)
                staging.approve_row(row, actor, candidate=candidate if action == "pick" else None,
                                    create=action == "create", keep=list(keep), take=list(take),
                                    outcome=outcome or None)
                if otype == "pipeline" and stage_outcomes:
                    row.mapped_values["stage_outcomes"] = stage_outcomes
                    row.save(update_fields=["mapped_values", "updated_at"])
            else:
                raise ReviewError(f"Unknown action {action!r}.")
        except staging.DecisionError as exc:
            raise ReviewError(str(exc)) from exc
        _drop_claim(row)
    return row


def apply_open(batch) -> bool:
    from unify.models import ApprovalRun

    return ApprovalRun.objects.filter(batch=batch, kind=ApprovalRun.Kind.APPLY,
                                      status__in=(ApprovalRun.Status.OPEN, ApprovalRun.Status.APPLYING)).exists()


def _stage_outcomes(row, given: dict | None) -> dict:
    """A pipeline's closed stages each need won / lost / neither (4.11)."""
    closed = [s.get("key") for s in ((row.raw or {}).get("data") or {}).get("stages") or []
              if s.get("outcome") == "closed"]
    given = given or {}
    missing = [k for k in closed if k not in given or given[k] not in ("won", "lost", "")]
    if missing:
        raise ReviewError(f"Say whether each closed stage is won, lost or neither: {', '.join(missing)}.")
    return {k: given[k] for k in closed}


def _drop_claim(row) -> None:
    proposal = row.proposed_mapping or {}
    if "claim" in proposal:
        proposal = dict(proposal)
        proposal.pop("claim")
        StagedRow.objects.filter(pk=row.pk).update(proposed_mapping=proposal)
        row.proposed_mapping = proposal


# --- bulk actions (4.8) -------------------------------------------------------------------


@dataclass
class BulkResult:
    done: int = 0
    held: int = 0
    refused: int = 0
    errors: list | None = None

    def message(self, verb: str) -> str:
        text = f"{verb} {self.done} row{'s' if self.done != 1 else ''}."
        if self.held:
            text += f" {self.held} claimed by another reviewer left alone."
        if self.refused:
            text += f" {self.refused} refused"
            if self.errors:
                text += f" ({self.errors[0]})"
            text += "."
        return text


def _audit(batch, job, actor: str, action: str, result: BulkResult, user=None, **extra) -> None:
    from core.models import AuditLog

    AuditLog.objects.create(action="update", model_name="unify.ImportBatch", object_id=batch.pk,
                            user=user if getattr(user, "pk", None) else None, changes={
        "actor": actor, "bulk": action, "job": str(job.pk), "done": result.done, "held": result.held,
        "refused": result.refused, **extra})


def selected(batch, ids):
    """The batch's rows among ``ids``: anything else (another batch, another
    tenant, garbage) is ignored."""
    import uuid

    clean = []
    for value in ids:
        try:
            clean.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return StagedRow.objects.filter(batch=batch, pk__in=clean)


def flagged_among(batch, ids) -> list[tuple[int, list]]:
    """``(row number, flags)`` of the flagged rows of a selection (the confirmation)."""
    out = []
    for number, proposal in selected(batch, ids).filter(mapping_state=MS.PROPOSED).values_list(
            "row_number", "proposed_mapping"):
        flags = list((proposal or {}).get("flags") or [])
        if (proposal or {}).get("ai", {}) and (proposal.get("ai") or {}).get("disagrees"):
            flags.append(FLAG_AI_DISAGREES)
        if flags:
            out.append((number, flags))
    return sorted(out)


def bulk_decide(batch, job, ids, *, otype: str, actor: str, user=None, action: str, reason: str = "", now=None) -> BulkResult:
    """Approve / create new / skip a selection, row by row through the same
    services as one decision; rows held by another reviewer's claim are left."""
    if action == "skip" and reason not in dict(SKIP_REASONS):
        raise ReviewError("Skipping needs a reason: choose one from the list.")
    result = BulkResult(errors=[])
    states = (MS.PROPOSED, MS.APPROVED) if action == "skip" else (MS.PROPOSED,)
    pks = list(selected(batch, ids).filter(mapping_state__in=states).order_by("row_number")
               .values_list("pk", flat=True))
    for pk in pks:
        try:
            decide(batch, pk, otype=otype, actor=actor, action=action, reason=reason or match.REJECTED, now=now)
            result.done += 1
        except ClaimHeld:
            result.held += 1
        except (ReviewError, StagedRow.DoesNotExist) as exc:
            result.refused += 1
            if len(result.errors) < 3:
                result.errors.append(str(exc))
    _audit(batch, job, actor, f"{action}_selected", result, user=user, reason=reason or None)
    return result


def approve_eligible(batch, job, filters: Filters, *, otype: str, actor: str, user=None, now=None) -> BulkResult:
    """"Approve all unflagged ≥ 0.95 in this view" (4.8): never a flagged row,
    an R6 link, a conflict or an AI disagreement (``match.bulk_approvable``,
    the auto-approval rule), never a row another reviewer holds. Chunks of
    1,000 with ``bulk_update``, so 9,000 rows are a few dozen queries."""
    result = BulkResult(errors=[])
    base = filtered(batch, filters, username=actor, now=now).filter(eligible_q(actor, now))
    last = -1
    stamp = timezone.now()
    while True:
        with transaction.atomic():
            chunk = list(StagedRow.objects.select_for_update().filter(
                pk__in=base.filter(row_number__gt=last).order_by("row_number").values("pk")[:staging.CHUNK]
            ).order_by("row_number"))
            if not chunk:
                break
            last = chunk[-1].row_number
            picked = []
            for row in chunk:
                proposal = row.proposed_mapping or {}
                if row.mapping_state != MS.PROPOSED or not match.bulk_approvable(proposal, row.mapping_confidence):
                    result.refused += 1
                    continue
                claim = proposal.get("claim") or {}
                if claim and claim.get("by") != actor and int(claim.get("until") or 0) >= epoch(now):
                    result.held += 1
                    continue
                if match.is_erased(row.raw):
                    result.refused += 1
                    continue
                row.mapped_values = staging.approval_values(row.raw, otype, proposal["decision"],
                                                            proposal.get("target"), rule=proposal.get("rule"))
                row.mapping_state = MS.APPROVED
                row.mapping_actor = actor
                row.updated_at = stamp
                if "claim" in proposal:
                    row.proposed_mapping = {k: v for k, v in proposal.items() if k != "claim"}
                picked.append(row)
            StagedRow.objects.bulk_update(
                picked, ["mapped_values", "mapping_state", "mapping_actor", "proposed_mapping", "updated_at"],
                batch_size=staging.CHUNK)
            result.done += len(picked)
    _audit(batch, job, actor, "approve_eligible", result, user=user, threshold=str(THRESHOLD))
    return result


def claim_next(batch, job, filters: Filters, *, actor: str, user=None, now=None, size: int | None = None) -> BulkResult:
    """"Claim next 50" (4.9): the first undecided, unclaimed rows of the view."""
    size = size or CLAIM_BATCH
    now = now or timezone.now()
    until = epoch(now + CLAIM_TTL)
    result = BulkResult(errors=[])
    view = Filters(**{**filters.__dict__, "state": "undecided", "mine": False, "after": ""})
    qs = filtered(batch, view, username=actor, now=now).filter(unclaimed_q(now))
    qs = qs.order_by("-conf", "-row_number") if view.order == "desc" else qs.order_by("conf", "row_number")
    with transaction.atomic():
        ids = list(qs.values_list("pk", flat=True)[:size])
        rows = list(StagedRow.objects.select_for_update().filter(pk__in=ids, mapping_state=MS.PROPOSED))
        for row in rows:
            claim = (row.proposed_mapping or {}).get("claim") or {}
            if claim and int(claim.get("until") or 0) >= epoch(now) and claim.get("by") != actor:
                result.held += 1
                continue
            row.proposed_mapping = {**(row.proposed_mapping or {}), "claim": {"by": actor, "until": until}}
            result.done += 1
        StagedRow.objects.bulk_update([r for r in rows if (r.proposed_mapping.get("claim") or {}).get("by") == actor],
                                      ["proposed_mapping"])
    _audit(batch, job, actor, "claim", result, user=user, until=until)
    return result


def release_claims(batch, job, *, actor: str, user=None) -> BulkResult:
    result = BulkResult(errors=[])
    with transaction.atomic():
        rows = list(StagedRow.objects.select_for_update().filter(batch=batch, proposed_mapping__claim__by=actor))
        for row in rows:
            row.proposed_mapping = {k: v for k, v in (row.proposed_mapping or {}).items() if k != "claim"}
        StagedRow.objects.bulk_update(rows, ["proposed_mapping"])
        result.done = len(rows)
    _audit(batch, job, actor, "release_claims", result, user=user)
    return result


def rematch(batch, job, ids, *, otype: str, actor: str, user=None, limit: int | None = None) -> BulkResult:
    """"Re-run matching" (4.8) for a selection, or the whole batch when
    ``ids`` is None: undecided rows only, never a row another reviewer holds,
    claims kept, no auto-approval (nothing a reviewer has not seen becomes
    approved). Refused above ``limit`` rows and while another process holds
    the job (``stage.job_run_lock``: a CLI run, a staging)."""
    qs = StagedRow.objects.filter(batch=batch, mapping_state=MS.PROPOSED).filter(free_for_q(actor))
    if ids is not None:
        qs = qs.filter(pk__in=selected(batch, ids).values("pk"))
    if limit is not None and qs.count() > limit:
        raise ReviewError(f"Re-matching more than {limit} rows from the browser is refused: select fewer rows, "
                          "or re-stage the export on the server.")
    rows = list(qs.order_by("row_number"))
    claims = {r.pk: (r.proposed_mapping or {}).get("claim") for r in rows if (r.proposed_mapping or {}).get("claim")}
    result = BulkResult(errors=[])
    if rows:
        previous = staging.previous_chain(job)
        try:
            with staging.job_run_lock(job, "rematched"), transaction.atomic():
                # match_batch decides unmapped rows only: put the selection back first.
                StagedRow.objects.filter(pk__in=[r.pk for r in rows], mapping_state=MS.PROPOSED).update(
                    mapping_state=MS.UNMAPPED)
                result.done = staging.match_batch(job, batch, otype, previous_jobs=previous, rows=rows)
                staging.mark_shared_targets(batch)
                if claims:
                    again = list(StagedRow.objects.filter(pk__in=list(claims), mapping_state=MS.PROPOSED))
                    for row in again:
                        row.proposed_mapping = {**(row.proposed_mapping or {}), "claim": claims[row.pk]}
                    StagedRow.objects.bulk_update(again, ["proposed_mapping"])
        except staging.StagingError as exc:  # the job is busy in another process
            raise ReviewError(str(exc)) from exc
    _audit(batch, job, actor, "rematch", result, user=user, scope="selected" if ids is not None else "batch")
    return result


def ai_route() -> str:
    """The llmhub route the AI proposals would use (3.5), for the "AI off" note:
    the settings page's lane first, then the purpose's default (``ai.route_status``)."""
    return ai_status().route


def ai_status():
    """``crm_import.ai.route_status()`` for ``crm_match_propose``; a broken
    registry is llmhub's error, not the queue's (reported as not ready)."""
    from . import ai

    try:
        return ai.route_status()
    except Exception as exc:  # noqa: BLE001
        return ai.RouteStatus(ai.MATCH_PURPOSE, reason=f"{type(exc).__name__}: {exc}"[:200])


#: Rows one "Run AI suggestions" click sends at most (one call each, inside
#: the request, which holds a worker and the job's lock); more is
#: ``crm_import ai --job <id>`` on the server.
AI_WEB_LIMIT = 10
#: Per call and for the whole click, in seconds: a slow or down gateway must
#: not hold a worker (sync views share one thread per ASGI worker) or the job.
AI_WEB_TIMEOUT = 15
AI_WEB_DEADLINE = 25
#: The "rows pending" count on the button stops here ("1,000+").
AI_PENDING_CAP = 1000


def _ai_candidates(batch, filters: Filters, *, username: str, now=None):
    """The view's rows the match pass would send, in queue order: the same
    rule as ``ai.eligible_rows`` (proposed, below 0.95, not erased, a link or
    no decision with candidates, no suggestion yet), so a click never picks
    rows the pass then drops."""
    from . import ai

    view = filtered(batch, filters, username=username, now=now).filter(
        mapping_state=MS.PROPOSED, mapping_confidence__lt=match.AUTO_APPROVE_THRESHOLD)
    order = ("-conf", "-row_number") if filters.order == "desc" else ("conf", "row_number")
    for pk, proposal, erased in view.order_by(*order).values_list(
            "pk", "proposed_mapping", "raw__erased").iterator(chunk_size=1000):
        if erased is not True and ai.is_eligible(proposal):
            yield pk


def run_ai(batch, job, filters: Filters, *, otype: str, actor: str, user=None, now=None) -> tuple:
    """"Run AI suggestions" (4.8) for the undecided rows of the view, at most
    ``AI_WEB_LIMIT`` in queue order, under the job's lock. Suggestions only:
    nothing here decides a row. Returns ``(BulkResult, ai.Result)``."""
    from . import ai

    if not ai.enabled(job):
        raise ReviewError("AI suggestions are off for this job: turn them on with "
                          f"`manage.py crm_import ai --job {job.pk} --enable`.")
    status = ai_status()
    if not status.ok:
        raise ReviewError(f"AI suggestions are not configured: {status.reason}")
    if otype not in ai.TYPES:
        raise ReviewError("AI suggestions are for contacts and companies only.")
    ids = []
    for pk in _ai_candidates(batch, filters, username=actor, now=now):
        ids.append(pk)
        if len(ids) >= AI_WEB_LIMIT:
            break
    out = BulkResult(errors=[])
    if not ids:
        return out, ai.Result(otype)
    result = ai.Result(otype)
    calls_before = batch.llm_calls_used
    limits = ai.Limits(timeout=AI_WEB_TIMEOUT, deadline_s=AI_WEB_DEADLINE)
    try:
        with staging.job_run_lock(job, "given AI suggestions"):
            result = ai.propose(job, batch, otype, ids=ids, limits=limits)
    except staging.StagingError as exc:  # another process holds the job
        raise ReviewError(str(exc)) from exc
    except ai.MinimisationError as exc:
        raise ReviewError(f"Nothing more was sent: {exc}") from exc
    finally:
        # Every paid call is recorded, also when the pass raised part-way.
        batch.refresh_from_db(fields=["llm_calls_used", "llm_call_budget"])
        out.done = result.suggested
        out.refused = result.malformed + result.dropped + result.failed
        calls = batch.llm_calls_used - calls_before
        _audit(batch, job, actor, "ai_suggest", out, user=user, calls=calls,
               budget_exhausted=result.budget_exhausted, disagrees=result.disagrees, stopped=result.stopped)
        _record_web_run(job, otype, result, calls)
    return out, result


def _record_web_run(job, otype: str, result, calls: int) -> None:
    """Keep ``job.report["ai"]`` current after a click (the CLI's report)."""
    from .models import ImportJob

    with transaction.atomic():
        locked = ImportJob.objects.select_for_update().get(pk=job.pk)
        report = dict(locked.report or {})
        section = dict(report.get("ai") or {"enabled": True})
        entry = dict(section.get(otype) or {})
        web = entry.get("web") or {"runs": 0, "calls": 0, "suggested": 0, "disagrees": 0}
        entry["web"] = {"runs": web["runs"] + 1, "calls": web["calls"] + calls,
                        "suggested": web["suggested"] + result.suggested,
                        "disagrees": web["disagrees"] + result.disagrees,
                        "last": result.as_dict()}
        entry["budget_exhausted"] = bool(entry.get("budget_exhausted") or result.budget_exhausted)
        section[otype] = entry
        report["ai"] = section
        ImportJob.objects.filter(pk=job.pk).update(report=report)
    job.report = report


def ai_context(batch, job, filters: Filters, *, otype: str, username: str, now=None) -> dict:
    """What the queue shows about AI: on or off, the route, and for "Run AI
    suggestions" how many rows of the view it would send against the budget."""
    from . import ai

    status = ai_status()
    out = {"ai_on": ai.enabled(job), "ai_route": status.route, "ai_ready": False, "ai_pending": 0,
           "ai_budget": batch.llm_call_budget, "ai_budget_left": ai.budget_left(batch),
           "ai_web_limit": AI_WEB_LIMIT, "ai_reason": status.reason}
    out["ai_blocked"] = out["ai_on"] and not status.ok and otype in ai.TYPES
    if out["ai_on"] and status.ok and otype in ai.TYPES:
        out["ai_ready"] = True
        pending = 0
        for _pk in _ai_candidates(batch, filters, username=username, now=now):
            pending += 1
            if pending > AI_PENDING_CAP:
                break
        out["ai_pending"] = min(pending, AI_PENDING_CAP)
        out["ai_pending_more"] = pending > AI_PENDING_CAP
    return out


def now_epoch() -> int:
    return int(time.time())
