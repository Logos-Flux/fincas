"""AI-assisted matching: suggestions only (docs/crm/phase5-importer.md 3.5, step 10).

Two ``llmhub`` purposes, called through the same path ``unify.propose``
uses (``llmhub.client.complete``, ``_spend`` against
``ImportBatch.llm_call_budget``, ``_parse_json`` / ``_confidence``):

- ``crm_match_propose``: for a contact or company row the rules left
  undecided below 0.95 with at least one candidate, which candidate (or
  ``create``) the model would pick. Stored at ``proposed_mapping["ai"]``.
- ``crm_dedupe_hint``: for pairs of *incoming* records of one batch that
  share evidence (the rule hints of 3.3 plus name-and-postcode pairs, which
  the rules do not hint), whether they look like one person or organisation.
  Stored at ``proposed_mapping["ai_duplicates"]``; a "same" verdict adds the
  earlier record to ``possible_duplicate_of``. Never a merge.

**What the model sees.** Comparison features only, computed here: per
candidate "same family name", "same e-mail domain", "same last four phone
digits", a bucketed name similarity, the rule and its confidence, the row's
flags. Never a name, address, e-mail, phone number, party id, source id,
note body, custom property or any other free text. Candidates are opaque
handles (``c1`` ... ``c5``, pairs ``p1`` ...) that map back here; a handle
the model invents is dropped. ``payload_violations`` is the check the tests
run over every request body, and ``propose`` runs it before every call too:
a payload that fails it is not sent.

**What the model changes.** Nothing a decision depends on: not
``decision``, ``target``, ``mapping_confidence``, ``mapping_state``,
``candidates``, the queue's sort or ``mapped_values``. A suggestion that
disagrees with the rule's link adds the flag ``ai_disagrees`` (and
``ai.disagrees``), which keeps the row out of bulk approval
(``match.bulk_approvable``). Malformed or refused output leaves the row as
it was.

**When it runs.** Only when the job opted in (``options["ai_match"]``,
``crm_import stage --ai-hints``) *and* the purpose has a route in this
deployment's ``llmhub`` (``route_status``). Otherwise nothing is sent and
the report says why.

**Budget.** One call per row, one per ``PAIRS_PER_CALL`` pairs, against the
batch's ``llm_call_budget`` (set at staging from
``GOLDEN_CRM_IMPORT["ai_budget_per_batch"]``, default 500). At the limit the
run stops, and ``report["ai"][<type>]["budget_exhausted"]`` says so.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction

from unify.models import ImportBatch, StagedRow

from . import match

MATCH_PURPOSE = "crm_match_propose"
DEDUPE_PURPOSE = "crm_dedupe_hint"
PURPOSES = (MATCH_PURPOSE, DEDUPE_PURPOSE)
#: The object types the AI pass looks at (people and organisations; 3.5).
TYPES = match.PERSON_TYPES + match.ORG_TYPES
OPTION = "ai_match"
DEFAULT_BUDGET = 500
PAIRS_PER_CALL = 20
MAX_PAIRS_PER_ROW = match.MAX_DUPLICATE_HINTS
REASON_MAX = 200
CREATE = "create"
FLAG_AI_DISAGREES = "ai_disagrees"  # one of match.FLAGS: keeps the row out of bulk approval
#: The keys a decision depends on: the AI pass refuses to write a row where any
#: of them changed while its call was out.
DECISION_KEYS = ("decision", "target", "rule", "candidates")
#: A suggestion needs an undecided row; a duplicate hint (display only, never
#: read by an applier) may also go on an approved row that is not applied yet.
HINT_STATES = (StagedRow.MappingState.PROPOSED, StagedRow.MappingState.APPROVED)

MATCH_SYSTEM = (
    "You review one record of a CRM import that the matching rules could not settle. You never see "
    "names, addresses or contact details: only comparisons between the incoming record and each "
    "candidate already in the system. Booleans are true, false or null (null: one side has no value). "
    "Similarities are buckets: exact, high, mid, low, none. Reply with exactly one JSON object, no prose: "
    "{\"suggested\": \"<candidate handle, or create>\", \"confidence\": <0-1>, \"reason\": \"<short>\"}. "
    "Suggest create when no candidate is clearly the same person or organisation."
)

DEDUPE_SYSTEM = (
    "You review pairs of incoming CRM records that share some evidence, to hint which pairs are the "
    "same person or organisation. You never see names, addresses or contact details: only comparisons "
    "between the two records of each pair, in the same encoding as the matching review (booleans or "
    "null, similarity buckets exact/high/mid/low/none). Reply with exactly one JSON object, no prose: "
    "{\"pairs\": [{\"pair\": \"<handle>\", \"same\": <true|false>, \"confidence\": <0-1>, "
    "\"reason\": \"<short>\"}]}. Omit a pair you cannot judge."
)


def budget_per_batch() -> int:
    conf = getattr(settings, "GOLDEN_CRM_IMPORT", None) or {}
    try:
        return max(0, int(conf.get("ai_budget_per_batch", DEFAULT_BUDGET)))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET


class MinimisationError(RuntimeError):
    """A payload broke the schema of ``payload_violations``: it was not sent."""


# --- is it on? ----------------------------------------------------------------------


@dataclass
class RouteStatus:
    purpose: str
    route: str = ""
    mode: str = ""
    ok: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {"purpose": self.purpose, "route": self.route, "mode": self.mode, "ok": self.ok,
                "reason": self.reason}


def budget_left(batch: ImportBatch) -> int:
    return max(0, batch.llm_call_budget - batch.llm_calls_used)


def route_status(purpose: str = MATCH_PURPOSE) -> RouteStatus:
    """Where ``purpose`` would go in this deployment, without calling it (the
    job screen states the route before a reviewer turns AI on, 3.5)."""
    from llmhub import conf
    from llmhub.client import LLMHubConfigError, endpoint_for

    status = RouteStatus(purpose)
    try:
        snap = conf.snapshot()
        status.mode = snap.mode
        endpoint = endpoint_for(purpose, snap)
    except KeyError:
        status.reason = f"{purpose} is not in LLMHUB_PURPOSES"
        return status
    except LLMHubConfigError as exc:
        status.route = exc.route
        status.reason = exc.message
        return status
    status.route, status.ok = endpoint.route, True
    return status


def enabled(job) -> bool:
    return bool((job.options or {}).get(OPTION))


# --- features (input minimisation) ------------------------------------------------

#: Every string value a payload may carry, besides handles (``c1``, ``p1``) and
#: rule ids. ``payload_violations`` refuses anything else.
BUCKETS = ("exact", "high", "mid", "low", "none")
KINDS = ("person", "organisation")
RULE_DECISIONS = ("create", "link", "none")
_HANDLE = re.compile(r"^[cp][1-9][0-9]?$")
_RULE = re.compile(r"^(R[1-6]|C1|owner_email|pipeline_key)$")

PERSON_FEATURES = ("email_same", "email_domain_same", "email_local_similarity", "role_address", "phone_same",
                   "phone_last4_same", "family_name_same", "given_name", "name_similarity", "postcode_same",
                   "country_same", "needs_individual_review")
ORG_FEATURES = ("org_name_same", "org_name_similarity", "domain_same")
GIVEN_NAME = ("same", "initial", "different")


def _bucket(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio >= 1.0:
        return "exact"
    if ratio >= 0.85:
        return "high"
    if ratio >= 0.6:
        return "mid"
    if ratio > 0:
        return "low"
    return "none"


def _ratio(a: set[str], b: set[str]) -> float | None:
    if not a or not b:
        return None
    return max(difflib.SequenceMatcher(None, x, y).ratio() for x in sorted(a) for y in sorted(b))


def _same(a: set, b: set) -> bool | None:
    if not a or not b:
        return None
    return bool(a & b)


def _person_features(a, b) -> dict:
    local = {e.rpartition("@")[0] for e in a.emails}, {e.rpartition("@")[0] for e in b.emails}
    domains = {e.rpartition("@")[2] for e in a.emails}, {e.rpartition("@")[2] for e in b.emails}
    families = {f for _g, f in a.names if f}, {f for _g, f in b.names if f}
    givens = {g for g, _f in a.names if g}, {g for g, _f in b.names if g}
    full = ({f"{g} {f}".strip() for g, f in a.names}, {f"{g} {f}".strip() for g, f in b.names})
    given = None
    if givens[0] and givens[1]:
        if givens[0] & givens[1]:
            given = "same"
        elif any(match.given_names_compatible(x, y) for x in givens[0] for y in givens[1]):
            given = "initial"
        else:
            given = "different"
    shared = a.emails & b.emails
    return {
        "email_same": _same(a.emails, b.emails),
        "email_domain_same": _same(*domains),
        "email_local_similarity": _bucket(_ratio(*local)),
        "role_address": any(match.is_role_address(e) for e in shared) if shared else None,
        "phone_same": _same(a.phones, b.phones),
        "phone_last4_same": _same({p[-4:] for p in a.phones}, {p[-4:] for p in b.phones}),
        "family_name_same": _same(*families),
        "given_name": given,
        "name_similarity": _bucket(_ratio(*(s - {""} for s in full))),
        "postcode_same": _same({p for p, _c in a.postcodes}, {p for p, _c in b.postcodes}),
        "country_same": _same({c for _p, c in a.postcodes if c}, {c for _p, c in b.postcodes if c}),
        "needs_individual_review": bool(b.is_staff or b.user or b.is_account_holder),
    }


def _org_features(a, b) -> dict:
    return {
        "org_name_same": _same(a.org_names, b.org_names),
        "org_name_similarity": _bucket(_ratio(a.org_names, b.org_names)),
        "domain_same": _same(a.domains, b.domains),
    }


def compare_nodes(a, b) -> dict:
    """The comparison of two dedupe nodes the model sees: booleans, buckets, nothing else."""
    return _person_features(a, b) if a.kind == match.PERSON else _org_features(a, b)


def _rule_confidence(value) -> float:
    return round(float(value or 0), 3)


def match_payload(row_proposal: dict, node, candidates: list[tuple[str, dict, object]]) -> dict:
    """The ``crm_match_propose`` input for one row. ``candidates`` are
    ``(handle, stored candidate, node)``; the rule's decision is expressed by
    handle, never by id."""
    decision = row_proposal.get("decision")
    target = (row_proposal.get("target") or {}).get("id")
    handle_of = {str(c.get("id")): h for h, c, _n in candidates}
    if decision == match.LINK and str(target) in handle_of:
        rule_decision, rule_pick = "link", handle_of[str(target)]
    elif decision == match.CREATE:
        rule_decision, rule_pick = "create", None
    else:
        rule_decision, rule_pick = "none", None
    return {
        "kind": node.kind,
        "rule_decision": rule_decision,
        "rule_pick": rule_pick,
        # Never the AI's own flag: a --force re-run must not show the model its last verdict.
        "flags": sorted(f for f in row_proposal.get("flags") or [] if f in match.FLAGS and f != FLAG_AI_DISAGREES),
        "candidates": [
            {"handle": h, "rule": c.get("rule"), "rule_confidence": _rule_confidence(c.get("confidence")),
             "features": compare_nodes(node, cand_node)}
            for h, c, cand_node in candidates
        ],
    }


def dedupe_payload(pairs: list[tuple[str, object, object]]) -> dict:
    return {"pairs": [{"handle": h, "kind": a.kind, "features": compare_nodes(a, b)} for h, a, b in pairs]}


#: The values each key may hold: a tuple is a closed list (``None`` = null).
_BOOL = (True, False, None)
_SCHEMA: dict[str, tuple | str] = {
    "kind": KINDS, "rule_decision": RULE_DECISIONS, "rule_pick": "handle_or_null", "flags": "flags",
    "candidates": "list", "pairs": "list", "handle": "handle", "rule": "rule", "rule_confidence": "unit",
    "features": "dict",
    **{k: _BOOL for k in PERSON_FEATURES + ORG_FEATURES},
    "email_local_similarity": (*BUCKETS, None), "name_similarity": (*BUCKETS, None),
    "org_name_similarity": (*BUCKETS, None), "given_name": (*GIVEN_NAME, None),
}


def payload_violations(payload) -> list[str]:
    """Why a payload may not be sent: a key outside the schema, a value the
    key does not allow (each key has its own closed list or type; the only
    number is ``rule_confidence``, in [0, 1] with at most three decimals).
    Empty means it is safe. Run before every call and by the tests over every
    request the gateway received. Messages name the path, never the value."""
    problems: list[str] = []

    def check(key, value, path):
        kind = _SCHEMA.get(key)
        if kind is None:
            problems.append(f"{path}: key is not in the payload schema")
        elif isinstance(kind, tuple):
            if not any(value is k if k is None or isinstance(k, bool) else (isinstance(value, str) and value == k)
                       for k in kind):
                problems.append(f"{path}: value is not one of the allowed values")
        elif kind == "handle_or_null":
            if not (value is None or (isinstance(value, str) and _HANDLE.fullmatch(value))):
                problems.append(f"{path}: not a handle")
        elif kind == "handle":
            if not (isinstance(value, str) and _HANDLE.fullmatch(value)):
                problems.append(f"{path}: not a handle")
        elif kind == "rule":
            if not (isinstance(value, str) and _RULE.fullmatch(value)):
                problems.append(f"{path}: not a rule id")
        elif kind == "unit":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1 \
                    or round(value, 3) != value:
                problems.append(f"{path}: not a number in [0, 1] with at most three decimals")
        elif kind == "flags":
            if not (isinstance(value, list) and all(isinstance(f, str) and f in match.FLAGS for f in value)):
                problems.append(f"{path}: not a list of known flags")
        elif kind == "list":
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                problems.append(f"{path}: not a list of objects")
            else:
                for i, v in enumerate(value):
                    walk(v, f"{path}[{i}]")
        elif kind == "dict":
            if not isinstance(value, dict):
                problems.append(f"{path}: not an object")
            else:
                walk(value, path)

    def walk(obj, path):
        for k, v in obj.items():
            check(k, v, f"{path}.{k}" if isinstance(k, str) and k in _SCHEMA else f"{path}.<key>")

    if not isinstance(payload, dict):
        return ["$: not an object"]
    walk(payload, "$")
    return problems


def _message(payload: dict) -> list[dict]:
    # sort_keys + compact separators: the same row gives the same bytes on every
    # run, so recorded responses keyed by the request stay valid.
    return [{"role": "user", "content": json.dumps(payload, sort_keys=True, separators=(",", ":"))}]


# --- the run --------------------------------------------------------------------


@dataclass
class Result:
    object_type: str
    eligible: int = 0
    suggested: int = 0
    disagrees: int = 0
    malformed: int = 0
    dropped: int = 0  # the model named a handle it was not given
    failed: int = 0  # the gateway refused or errored
    changed: int = 0  # decided by a reviewer while the call was out: not written
    pairs: int = 0
    pairs_same: int = 0
    hints_added: int = 0
    calls: int = 0
    budget: int = 0
    calls_used: int = 0
    budget_exhausted: bool = False
    stopped: str = ""  # "deadline" or "gateway_failing": the pass ended early, nothing more sent
    streak: int = 0  # consecutive gateway failures
    skipped: str = ""
    route: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if k not in ("object_type", "streak")}
        out["errors"] = self.errors[:10]
        return out


class _Budget:
    """The batch's ``llm_call_budget``, spent one call at a time with one
    conditional UPDATE: two runs on one batch can never spend past it (the
    read-check-save of ``unify.propose._spend`` could, by losing an increment)."""

    def __init__(self, batch: ImportBatch, result: Result):
        self.batch, self.result = batch, result
        result.budget = batch.llm_call_budget

    def take(self) -> bool:
        from django.db.models import F

        spent = ImportBatch.objects.filter(
            pk=self.batch.pk, llm_calls_used__lt=F("llm_call_budget"),
        ).update(llm_calls_used=F("llm_calls_used") + 1)
        self.batch.refresh_from_db(fields=["llm_calls_used", "llm_call_budget"])
        if not spent:
            self.result.budget_exhausted = True
            return False
        self.result.calls += 1
        return True


@dataclass
class Limits:
    """How long one pass may take. ``timeout``: seconds per call;
    ``deadline_s``: wall clock for the whole pass (None: no limit);
    ``max_failures``: consecutive gateway failures that stop it (an outage
    must not spend a budget one timeout at a time under the job's lock)."""

    timeout: float = 60.0
    deadline_s: float | None = None
    max_failures: int = 3
    started: float = field(default_factory=lambda: time.monotonic())

    def stop(self, result: "Result") -> bool:
        if result.streak >= self.max_failures:
            result.stopped = "gateway_failing"
        elif self.deadline_s is not None and time.monotonic() - self.started >= self.deadline_s:
            result.stopped = "deadline"
        return bool(result.stopped)


def _call(purpose: str, system: str, payload: dict, result: Result, limits: Limits | None = None):
    """One completion; ``None`` when the gateway failed or the output is not a JSON object."""
    from llmhub.client import LLMHubError, complete
    from unify.propose import ProposalError, _parse_json

    problems = payload_violations(payload)
    if problems:  # a bug here must never become a leak: refuse to send
        raise MinimisationError(f"{purpose} payload failed minimization: {problems[:3]}")
    try:
        completion = complete(purpose, _message(payload), system=system, max_tokens=400, temperature=0,
                              timeout=(limits or Limits()).timeout)
    except LLMHubError as exc:
        result.failed += 1
        result.streak += 1
        result.errors.append(f"{exc.category}: {exc.message[:160]}")
        return None, None
    result.streak = 0
    try:
        return _parse_json(completion.text, purpose), completion
    except ProposalError:
        result.malformed += 1
        return None, completion


def _reason(value) -> str:
    return " ".join(str(value or "").split())[:REASON_MAX]


def propose(job, batch: ImportBatch, object_type: str, *, force: bool = False, ids=None,
            limit: int | None = None, limits: Limits | None = None) -> Result:
    """``crm_match_propose`` over the eligible rows of one batch (3.5), or of
    ``ids`` only (the review screen's "Run AI suggestions", at most ``limit``)."""
    from unify.propose import ProposalError, _confidence

    result = Result(object_type)
    if object_type not in TYPES:
        result.skipped = "type"
        return result
    status = route_status(MATCH_PURPOSE)
    result.route = status.as_dict()
    if not status.ok:
        result.skipped = "no_route"
        return result
    _check_tenant(job, batch)
    if in_rolled_back_scope():
        result.skipped = "transaction"
        return result
    rows = eligible_rows(batch, force=force, ids=ids, limit=limit)
    result.eligible = len(rows)
    if not rows:
        return result
    index = match.PartyIndex(job.company)
    budget = _Budget(batch, result)
    limits = limits or Limits()
    for row in rows:
        proposal = row.proposed_mapping or {}
        node = match.record_node(job.company, object_type, row.raw)
        candidates = ordered_candidates(proposal, node, index)
        if not candidates:
            continue
        if limits.stop(result) or not budget.take():
            break
        payload = match_payload(proposal, node, candidates)
        data, completion = _call(MATCH_PURPOSE, MATCH_SYSTEM, payload, result, limits)
        if data is None:
            continue
        by_handle = {h: str(c.get("id")) for h, c, _n in candidates}
        handle = data.get("suggested")
        if handle != CREATE and handle not in by_handle:
            result.dropped += 1
            continue
        try:
            confidence = float(_confidence(data.get("confidence", 0), MATCH_PURPOSE))
        except ProposalError:
            result.malformed += 1
            continue
        suggested = CREATE if handle == CREATE else by_handle[handle]
        # Judged against the rule decision the model was shown: a link whose
        # target is no longer a live candidate was shown as "none".
        disagrees = _disagrees(proposal, suggested) if payload["rule_decision"] != "none" else False
        ai = {"suggested": suggested, "confidence": confidence, "reason": _reason(data.get("reason")),
              "disagrees": disagrees, "model": completion.resolved_model or completion.route,
              "route": completion.route}
        if _write(row, proposal, ai=ai, disagrees=disagrees) is not None:
            result.suggested += 1
            result.disagrees += int(disagrees)
        else:
            result.changed += 1
    result.calls_used = batch.llm_calls_used
    return result


def ordered_candidates(proposal: dict, node, index) -> list[tuple[str, dict, object]]:
    """``(handle, stored candidate, node)`` for the candidates of the tenant
    still live, in an order that depends on the evidence only (never on a
    party id), so the same row gives the same payload bytes on every run."""
    found = []
    for stored in proposal.get("candidates") or []:
        cand_node = index.nodes.get(f"party:{stored.get('id')}")
        if cand_node is None or cand_node.kind != node.kind:
            continue  # gone, merged, erased or another tenant's: never shown to the model
        features = compare_nodes(node, cand_node)
        found.append((-_rule_confidence(stored.get("confidence")), str(stored.get("rule") or ""),
                      json.dumps(features, sort_keys=True), stored, cand_node))
    found.sort(key=lambda item: item[:3])
    return [(f"c{i + 1}", stored, cand_node) for i, (*_k, stored, cand_node) in
            enumerate(found[:match.MAX_CANDIDATES])]


def _disagrees(proposal: dict, suggested: str) -> bool:
    """Only a rule *decision* can be disagreed with: a link to another target
    (or to none). An undecided row (several candidates) has none."""
    if proposal.get("decision") == match.LINK:
        return suggested != str((proposal.get("target") or {}).get("id"))
    if proposal.get("decision") == match.CREATE:
        return suggested != CREATE
    return False


def is_eligible(proposal: dict, *, force: bool = False) -> bool:
    """A proposal the match pass would send (the row must also be ``proposed``,
    below 0.95 and not erased): a link or no decision, with candidates, and
    without ``force`` no suggestion yet."""
    proposal = proposal or {}
    if not proposal.get("candidates") or proposal.get("decision") not in (match.LINK, None):
        return False
    return force or not proposal.get("ai")


def eligible_rows(batch: ImportBatch, *, force: bool = False, ids=None, limit: int | None = None) -> list[StagedRow]:
    """Undecided (``proposed``), below 0.95, a link or no decision, at least one
    candidate, not erased; without ``force`` only rows without a suggestion yet.
    ``ids`` narrows to those rows (the review screen's view); ``limit`` stops
    after that many, in row order."""
    qs = batch.rows.filter(mapping_state=StagedRow.MappingState.PROPOSED,
                           mapping_confidence__lt=match.AUTO_APPROVE_THRESHOLD).order_by("row_number")
    if ids is not None:
        qs = qs.filter(pk__in=list(ids))
    out = []
    for row in qs.iterator(chunk_size=1000):
        if match.is_erased(row.raw) or not is_eligible(row.proposed_mapping, force=force):
            continue
        out.append(row)
        if limit is not None and len(out) >= limit:
            break
    return out


def _write(row: StagedRow, seen: dict, *, ai: dict | None = None, duplicates: list | None = None,
           disagrees: bool = False) -> int | None:
    """Merge the AI's output into the row's proposal under a row lock, only if
    the row is still the row the call was made for. ``None`` when it is not;
    else the number of duplicate hints added."""
    states = (StagedRow.MappingState.PROPOSED,) if ai is not None else HINT_STATES
    with transaction.atomic():
        fresh = StagedRow.objects.select_for_update().filter(pk=row.pk).first()
        if fresh is None or fresh.mapping_state not in states:
            return None
        proposal = dict(fresh.proposed_mapping or {})
        if any(proposal.get(k) != seen.get(k) for k in DECISION_KEYS):
            return None
        flags = set(proposal.get("flags") or [])
        if ai is not None:
            proposal["ai"] = ai
            flags.discard(FLAG_AI_DISAGREES)
            if disagrees:
                flags.add(FLAG_AI_DISAGREES)
        added = 0
        if duplicates is not None:
            # Appended to what the row holds now (pairs of one row may span calls).
            stored = list(proposal.get("ai_duplicates") or [])
            known = {d["of"] for d in stored}
            fresh_items = []
            for d in duplicates:
                if d["of"] not in known:
                    known.add(d["of"])
                    fresh_items.append(d)
            proposal["ai_duplicates"] = stored + fresh_items
            hints = list(proposal.get("possible_duplicate_of") or [])
            for item in fresh_items:
                if item["same"] and item["of"] not in hints and len(hints) < match.MAX_DUPLICATE_HINTS:
                    hints.append(item["of"])
                    added += 1
            if hints:
                proposal["possible_duplicate_of"] = hints
        proposal["flags"] = sorted(flags)
        StagedRow.objects.filter(pk=fresh.pk).update(proposed_mapping=proposal)
    return added


# --- dedupe hints between incoming records ---------------------------------------


def candidate_pairs(job, batch: ImportBatch, object_type: str) -> list[tuple[StagedRow, str, object, object]]:
    """``(later row, earlier source id, later node, earlier node)`` for every
    pair of incoming records of the batch that share a blocking key (r4
    included), at most ``MAX_PAIRS_PER_ROW`` per row, in row order."""
    seen: dict[tuple, list[tuple[str, object]]] = {}
    out = []
    rows = batch.rows.exclude(mapping_state__in=(StagedRow.MappingState.NA, StagedRow.MappingState.UNMAPPED))
    for row in rows.order_by("row_number").iterator(chunk_size=1000):
        if match.is_erased(row.raw) or not row.raw.get("source_id"):
            continue
        sid = str(row.raw["source_id"])
        node = match.record_node(job.company, object_type, row.raw)
        partners: list[tuple[str, object]] = []
        known: set[str] = set()
        for key in match._blocking_keys(node):
            for other_sid, other_node in seen.get(key, []):
                if other_sid != sid and other_sid not in known and len(partners) < MAX_PAIRS_PER_ROW:
                    known.add(other_sid)
                    partners.append((other_sid, other_node))
            seen.setdefault(key, []).append((sid, node))
        done = {item["of"] for item in (row.proposed_mapping or {}).get("ai_duplicates") or []}
        for other_sid, other_node in partners:
            if other_sid not in done and row.mapping_state in HINT_STATES:
                out.append((row, other_sid, node, other_node))
    return out


def dedupe_hints(job, batch: ImportBatch, object_type: str, *, limits: Limits | None = None) -> Result:
    """``crm_dedupe_hint`` over pairs of incoming records (3.5, last bullet)."""
    from unify.propose import ProposalError, _confidence

    result = Result(object_type)
    if object_type not in TYPES:
        result.skipped = "type"
        return result
    status = route_status(DEDUPE_PURPOSE)
    result.route = status.as_dict()
    if not status.ok:
        result.skipped = "no_route"
        return result
    _check_tenant(job, batch)
    if in_rolled_back_scope():
        result.skipped = "transaction"
        return result
    pairs = candidate_pairs(job, batch, object_type)
    result.eligible = len(pairs)
    budget = _Budget(batch, result)
    limits = limits or Limits()
    for start in range(0, len(pairs), PAIRS_PER_CALL):
        verdicts: dict[int, tuple[StagedRow, list]] = {}
        chunk = pairs[start:start + PAIRS_PER_CALL]
        if limits.stop(result) or not budget.take():
            break
        handles = {f"p{i + 1}": item for i, item in enumerate(chunk)}
        data, completion = _call(DEDUPE_PURPOSE, DEDUPE_SYSTEM,
                                 dedupe_payload([(h, a, b) for h, (_r, _s, a, b) in handles.items()]), result,
                                 limits)
        if data is None:
            continue
        answers = data.get("pairs")
        if not isinstance(answers, list):
            result.malformed += 1
            continue
        for answer in answers:
            if not isinstance(answer, dict) or answer.get("pair") not in handles:
                result.dropped += 1
                continue
            if not isinstance(answer.get("same"), bool):
                result.malformed += 1
                continue
            try:
                confidence = float(_confidence(answer.get("confidence", 0), DEDUPE_PURPOSE))
            except ProposalError:
                result.malformed += 1
                continue
            row, other_sid, _a, _b = handles[answer["pair"]]
            verdicts.setdefault(row.pk, (row, []))[1].append({
                "of": other_sid, "same": answer["same"], "confidence": confidence,
                "reason": _reason(answer.get("reason")), "model": completion.resolved_model or completion.route,
            })
            result.pairs += 1
            result.pairs_same += int(answer["same"])
        # Written per call, so a later failure never loses verdicts already paid for.
        for row, items in verdicts.values():
            added = _write(row, row.proposed_mapping or {}, duplicates=items)
            if added is None:
                result.changed += 1
            else:
                result.hints_added += added
    result.calls_used = batch.llm_calls_used
    return result


def _check_tenant(job, batch: ImportBatch) -> None:
    from .models import ImportJobBatch

    if batch.company_id != job.company_id or not ImportJobBatch.objects.filter(job=job, batch=batch).exists():
        raise ValueError("The batch does not belong to this job.")


def in_rolled_back_scope() -> bool:
    """Inside a ``transaction.atomic()`` that is not a test case's own wrapper
    (the step 8 runner uses the same test): a rollback there would undo the
    budget spend of calls that were really made, so the AI pass stays off."""
    from django.db import connection

    return any(not getattr(block, "_from_testcase", False) for block in connection.atomic_blocks)


def run(job, *, force: bool = False, types=TYPES) -> dict:
    """Both passes over the job's people and organisations, if the job opted
    in; the summary goes on ``job.report["ai"]`` and is returned."""
    from .models import ImportJobBatch

    if not enabled(job):
        summary = {"enabled": False, "reason": "the job did not opt in (crm_import stage --ai-hints)"}
    elif in_rolled_back_scope():
        # A dry run (step 8) runs inside one transaction it rolls back: the
        # budget spend would be undone with it while the calls were real.
        return {"enabled": False, "reason": "inside a transaction (a dry run): no calls made"}
    else:
        summary = {"enabled": True, "route": {p: route_status(p).as_dict() for p in PURPOSES}}
        links = ImportJobBatch.objects.filter(job=job, object_type__in=types).select_related("batch")
        for link in links.order_by("step"):
            try:
                matched = propose(job, link.batch, link.object_type, force=force)
                if matched.stopped == "gateway_failing":  # the gateway is down: ask nothing more
                    hinted = Result(link.object_type, skipped="gateway_failing")
                else:
                    hinted = dedupe_hints(job, link.batch, link.object_type)
            except (MinimisationError, ValueError) as exc:
                # The pass stops (nothing more is sent for this type); the job and
                # the suggestions already written stay. Loud in the report and the command.
                summary[link.object_type] = {"error": f"{type(exc).__name__}: {exc}", "budget_exhausted": False}
                continue
            summary[link.object_type] = {"match": matched.as_dict(), "dedupe": hinted.as_dict(),
                                         "budget_exhausted": matched.budget_exhausted or hinted.budget_exhausted}
    job.report = {**(job.report or {}), "ai": summary}
    job.save(update_fields=["report", "updated_at"])
    return summary
