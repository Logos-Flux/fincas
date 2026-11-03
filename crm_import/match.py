"""Matching and provenance (docs/crm/phase5-importer.md section 3).

``decide(company, record)`` answers, for one canonical record, what the
import should do with it. The staging pass (``crm_import.stage``) calls
``MatchContext.decide_chunk`` instead, which resolves a whole chunk with a
fixed number of queries (4.3) and gives the same answers.

The order of evidence (3.1), first hit wins:

== ==================================== ==========================================
1  the record is in the export twice     ``na`` ``duplicate_in_export``
2  ExternalRef of an **erased** record   ``na`` ``erased``; raw data dropped at once
3  an address with an active erasure     ``na`` ``erased``; raw dropped (people only)
   suppression
4  archived in the source                ``na`` ``archived_in_source`` (unless the
                                         job has ``include_archived``)
5  a live ExternalRef (id or alias)      re-import, 3.4: ``update`` 1.0, ``conflict``
                                         0.0, or ``na`` ``unchanged``
6  people: Phase 2's exact matcher       ``link`` at R1 0.95 (email), else R2 0.85 /
                                         R3 0.60 (phone, flag ``phone_only``)
7  dedupe evidence (Phase 2 rules)       ``link`` at R4 0.50 (people), R5 0.70 /
                                         R6 0.30 (organisations)
8  nothing                               ``create`` 1.0
== ==================================== ==========================================

Several candidates at the deciding rule propose nothing (``decision`` null,
confidence 0, flag ``ambiguous_match``) and list the candidates for review.

Owners match only by email, against the tenant's active staff (2.3);
pipelines match by key (2.4); every other type is ``create`` or re-import
by ExternalRef only (3.1, last paragraph).

Tenant isolation: every lookup is filtered by the job's company, and a
target an ExternalRef names is loaded only from that company. Nothing here
writes to the database.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from core.provenance import erased_source_id
from parties.constants import ORGANISATION, PERSON
from parties.dedupe import Node, find_clusters
from parties.dedupe_legacy import normalize_postcode
from parties.merge_queue import mask
from parties.normalize import normalize_email, normalize_name, normalize_org_name

from . import compare

# --- decisions (3.3) -----------------------------------------------------------

CREATE = "create"
LINK = "link"
UPDATE = "update"
CONFLICT = "conflict"
NA = "na"
DECISIONS = (CREATE, LINK, UPDATE, CONFLICT, NA)

# N/A reasons (4.1).
UNCHANGED = "unchanged"
ERASED = "erased"
ARCHIVED_IN_SOURCE = "archived_in_source"
DUPLICATE_IN_EXPORT = "duplicate_in_export"
REJECTED = "rejected"
OUT_OF_SCOPE = "out_of_scope"
NA_REASONS = (UNCHANGED, ERASED, ARCHIVED_IN_SOURCE, OUT_OF_SCOPE, REJECTED, DUPLICATE_IN_EXPORT)

# Flags (3.3). Any flag keeps a row out of bulk approval (4.8).
FLAG_NEEDS_INDIVIDUAL_REVIEW = "needs_individual_review"
FLAG_PHONE_ONLY = "phone_only"
FLAG_ARCHIVED_OWNER = "archived_owner"
FLAG_AMBIGUOUS = "ambiguous_match"          # several candidates at the deciding rule
FLAG_NAME_MISMATCH = "name_mismatch"        # linked on a contact point, names disagree
FLAG_ALREADY_LINKED = "party_already_linked"  # the target already holds another record of this source
FLAG_SHARED_TARGET = "shared_target"        # another record of this batch links to the same target
FLAG_TARGET_MISSING = "ref_target_missing"  # the ExternalRef names a row that is gone
FLAG_STAGE_KEYS_DIFFER = "stage_keys_differ"  # pipeline key exists, stages are not a superset (2.4)
FLAG_STAGE_OUTCOME_CLOSED = "stage_outcome_closed"  # a closed stage that is neither won nor lost (2.4)
FLAG_COMPETING = "competing_candidate"  # another party has evidence at R3 (0.60) or above
FLAG_ROLE_ADDRESS = "role_address"  # linked by a shared-mailbox address (info@, sales@, ...)
FLAG_ERASED_POINT_REMOVED = "erased_contact_point_removed"  # an organisation lost an erased person's phone
# The deal flags (2.5, step 5): staged here so they keep the row out of bulk
# approval; the deal applier enforces the same rules at apply time.
FLAG_OUTCOME_CONFLICT = "outcome_conflict"  # the source's flags and the stage disagree
FLAG_PARTY_AMBIGUOUS = "party_ambiguous"    # several contacts (or companies), none primary
FLAG_CLOSED_AT_INFERRED = "closed_at_inferred"  # closed with no close date
FLAG_OWNER_UNRESOLVED = "owner_unresolved"  # the owner is not in the export
FLAGS = (
    FLAG_NEEDS_INDIVIDUAL_REVIEW, FLAG_OUTCOME_CONFLICT, FLAG_PARTY_AMBIGUOUS, FLAG_PHONE_ONLY,
    FLAG_ARCHIVED_OWNER, FLAG_CLOSED_AT_INFERRED, FLAG_OWNER_UNRESOLVED, "ai_disagrees",
    FLAG_AMBIGUOUS, FLAG_NAME_MISMATCH, FLAG_ALREADY_LINKED, FLAG_SHARED_TARGET,
    FLAG_TARGET_MISSING, FLAG_STAGE_KEYS_DIFFER, FLAG_STAGE_OUTCOME_CLOSED, FLAG_COMPETING,
    FLAG_ROLE_ADDRESS, FLAG_ERASED_POINT_REMOVED,
)
COMPETING_FLOOR = 0.60

CONFIDENCE_SURE = Decimal("1.000")
CONFIDENCE_NONE = Decimal("0.000")
OWNER_EMAIL_CONFIDENCE = Decimal("0.950")
ARCHIVED_OWNER_CAP = Decimal("0.600")
MAX_CANDIDATES = 5
MAX_DUPLICATE_HINTS = 5

PARTY_MODEL = "parties.party"
PROFILE_MODEL = "core.userprofile"
USER_MODEL = "auth.user"
PIPELINE_MODEL = "crm.pipeline"

PERSON_TYPES = ("contact",)
ORG_TYPES = ("company",)


def _conf(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"))


@dataclass
class Decision:
    """What the importer proposes for one record (``StagedRow.proposed_mapping``)."""

    decision: str | None
    confidence: Decimal
    reason: str = ""
    target: dict | None = None
    rule: str | None = None
    candidates: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    diff: dict | None = None
    possible_duplicate_of: list = field(default_factory=list)
    drop_raw: bool = False  # the record is an erased person's: keep only the fact (and a hashed key)
    replace_raw: dict | None = None  # the record without an erased person's contact points

    @property
    def is_na(self) -> bool:
        return self.decision == NA

    def proposal(self) -> dict:
        if self.drop_raw:  # nothing about the person survives, not even masked evidence
            return {"decision": NA, "reason": self.reason}
        out = {
            "decision": self.decision,
            "target": self.target,
            "rule": self.rule,
            "candidates": self.candidates,
            "flags": sorted(set(self.flags)),
            "evidence": self.evidence,
            "diff": self.diff,
            "ai": None,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.possible_duplicate_of:
            out["possible_duplicate_of"] = self.possible_duplicate_of
        return out


def external_ref_key(record: dict) -> dict:
    """The ``external_ref`` approval copies into ``mapped_values``, so
    ``unify.apply``'s erased-source skip runs before every applier (3.3)."""
    key = {
        "source": record["source"],
        "object_type": record.get("source_object_type") or record["object_type"],
        "source_id": str(record["source_id"]),
    }
    aliases = record_keys(record)[1:]
    if aliases:
        key["aliases"] = aliases  # unify.apply skips the row when any of them is erased
    return key


def record_keys(record: dict) -> list[str]:
    """The source id, then the aliases (each alias has its own ExternalRef)."""
    keys = [str(record["source_id"])]
    keys += [str(a) for a in record.get("aliases") or [] if str(a) not in keys]
    return keys


# --- the tenant's parties, loaded once per batch (4.3) ---------------------------


class PartyIndex:
    """The tenant's live parties as dedupe nodes, indexed by blocking key."""

    def __init__(self, company):
        from parties.dedupe_parties import party_nodes

        nodes, _stats = party_nodes(company)
        self.company_key = str(company.pk)
        self.nodes: dict[str, Node] = {n.key: n for n in nodes}
        self.keys: dict[tuple, set[str]] = defaultdict(set)
        for n in nodes:
            for key in _blocking_keys(n):
                self.keys[key].add(n.key)

    def candidates(self, node: Node) -> list[Node]:
        keys: set[str] = set()
        for key in _blocking_keys(node):
            keys |= self.keys.get(key, set())
        return [self.nodes[k] for k in sorted(keys)]


def _blocking_keys(n: Node):
    """Every key a rule blocks on (a superset: the engine decides the rule)."""
    if n.kind == PERSON:
        for e in n.emails:
            yield ("email", e)
        for p in n.phones:
            yield ("phone", p)
        for given, family in n.names:
            if given and family:
                for postcode, country in n.postcodes:
                    yield ("r4", f"{given} {family}|{postcode}|{country}")
    else:
        for name in n.org_names:
            yield ("org", name)
        for d in n.domains:
            yield ("domain", d)


def record_node(company, object_type: str, record: dict) -> Node:
    """A canonical contact or company as a ``parties.dedupe.Node``, key
    ``import:<source_id>`` (3.1 step 3)."""
    data = record.get("data") or {}
    node = Node(key=f"import:{record['source_id']}", company=str(company.pk),
                kind=PERSON if object_type in PERSON_TYPES else ORGANISATION)
    if node.kind == PERSON:
        node.emails = set(compare.contact_emails(record))
        node.phones = {p for p in compare.contact_phones(record) if not p.startswith("raw:")}
        given, family = normalize_name(data.get("given_name")), normalize_name(data.get("family_name"))
        if given or family:
            node.names.add((given, family))
        for address in data.get("addresses") or []:
            key = normalize_postcode(address.get("postal_code"), address.get("country"))
            if key[0]:
                node.postcodes.add(key)
    else:
        for raw in {data.get("name") or "", data.get("legal_name") or ""} - {""}:
            key = normalize_org_name(raw)
            if key:
                node.org_names.add(key)
        domain = compare.primary_domain(record)
        if domain:
            node.domains.add(domain)
    return node


def _party_id(node_key: str) -> str:
    return node_key.split(":", 1)[1]


def _scored_candidates(node: Node, cands: list[Node]) -> tuple[list[dict], list]:
    """Run the Phase 2 engine over the record and its blocking neighbours and
    keep only evidence that involves the record. Returns candidates (best rule
    per party, strongest first) and the evidence."""
    if not cands:
        return [], []
    best: dict[str, tuple[float, str]] = {}
    evidence = []
    for cluster in find_clusters([node, *cands]):
        if node.key not in cluster.members:
            continue
        for ev in cluster.evidence:
            if node.key not in ev.members:
                continue
            evidence.append(ev)
            for member in ev.members:
                if member == node.key:
                    continue
                if member not in best or ev.confidence > best[member][0]:
                    best[member] = (ev.confidence, ev.rule)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))
    candidates = [{"id": _party_id(k), "rule": rule, "confidence": conf} for k, (conf, rule) in ranked]
    return candidates, evidence


def given_names_compatible(a: str, b: str) -> bool:
    """Equal, or one is an initial of the other (``j`` / ``j.`` and ``john``)."""
    a, b = a.rstrip("."), b.rstrip(".")
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) == 1 and long_.startswith(short)


def names_disagree(a_names, b_names) -> bool:
    """Both sides have a family name and none is shared, or both have a given
    name and none is compatible (``Mary Smith`` is not ``John Smith``, even on
    the family mailbox they share)."""
    fam_a = {f for _g, f in a_names if f}
    fam_b = {f for _g, f in b_names if f}
    if fam_a and fam_b and not (fam_a & fam_b):
        return True
    giv_a = {g for g, _f in a_names if g}
    giv_b = {g for g, _f in b_names if g}
    return bool(giv_a and giv_b and not any(given_names_compatible(x, y) for x in giv_a for y in giv_b))


def _names_disagree(a: Node, b: Node) -> bool:
    return names_disagree(a.names, b.names)


# Local parts that name a function, not a person: a shared mailbox is the
# classic way two different people "match" by email (3.2).
ROLE_LOCAL_PARTS = frozenset({
    "accounting", "accounts", "admin", "billing", "bookings", "careers", "contact", "enquiries", "enquiry",
    "events", "finance", "hello", "help", "hr", "info", "inquiries", "inquiry", "jobs", "mail", "marketing",
    "media", "noreply", "no-reply", "office", "orders", "postmaster", "press", "purchasing", "reception",
    "sales", "service", "support", "team", "webmaster",
})
_LOCAL_SPLIT = re.compile(r"[._-]")


def is_role_address(email: str) -> bool:
    """``info@``, ``sales.uk@``, ``info+web@``: an address that names a function."""
    local = email.rpartition("@")[0].split("+", 1)[0]
    return local in ROLE_LOCAL_PARTS or _LOCAL_SPLIT.split(local, 1)[0] in ROLE_LOCAL_PARTS


def match_party(index: PartyIndex, node: Node) -> Decision:
    """3.1 steps 2-4 for one contact or company node."""
    cands = index.candidates(node)
    candidates, evidence = _scored_candidates(node, cands)
    masked = [{"rule": ev.rule, "value": mask(ev.rule, ev.value)} for ev in evidence]
    stored = candidates[:MAX_CANDIDATES]
    if not candidates:
        return Decision(CREATE, CONFIDENCE_SURE)

    def link(cand: dict, *flags) -> Decision:
        target_node = index.nodes[f"party:{cand['id']}"]
        out = list(flags)
        if target_node.is_staff or target_node.user or target_node.is_account_holder:
            out.append(FLAG_NEEDS_INDIVIDUAL_REVIEW)
        if node.kind == PERSON and _names_disagree(node, target_node):
            out.append(FLAG_NAME_MISMATCH)
        if any(c["id"] != cand["id"] and c["confidence"] >= COMPETING_FLOOR for c in candidates):
            out.append(FLAG_COMPETING)
        if cand["rule"] == "R1" and any(is_role_address(e) for e in node.emails & target_node.emails):
            out.append(FLAG_ROLE_ADDRESS)
        return Decision(LINK, _conf(cand["confidence"]), target={"model": PARTY_MODEL, "id": cand["id"]},
                        rule=cand["rule"], candidates=stored, flags=out, evidence=masked)

    def ambiguous(*flags) -> Decision:
        return Decision(None, CONFIDENCE_NONE, candidates=stored, flags=[FLAG_AMBIGUOUS, *flags],
                        evidence=masked)

    if node.kind == PERSON:
        # 2. The exact matcher: parties.match.find over each email, then each E.164.
        by_email = [c for c in candidates if c["rule"] == "R1"]
        if by_email:
            return link(by_email[0]) if len(by_email) == 1 else ambiguous()
        by_phone = [c for c in candidates if c["rule"] in ("R2", "R3")]
        if by_phone:
            return link(by_phone[0], FLAG_PHONE_ONLY) if len(by_phone) == 1 else ambiguous(FLAG_PHONE_ONLY)
    # 3. Dedupe evidence: R4 (people), R5 / R6 (organisations).
    top = candidates[0]["confidence"]
    best = [c for c in candidates if c["confidence"] == top]
    return link(best[0]) if len(best) == 1 else ambiguous()


# --- owners (2.3) and pipelines (2.4) ---------------------------------------------


class OwnerIndex:
    """The tenant's active staff by normalised email."""

    def __init__(self, company):
        from django.contrib.auth import get_user_model
        from django.core.exceptions import ValidationError
        from django.db.models import Q

        self.by_email: dict[str, list[dict]] = defaultdict(list)
        rows = get_user_model().objects.filter(
            Q(is_staff=True) | Q(is_superuser=True), is_active=True, userprofile__company=company,
        ).values_list("pk", "email", "userprofile__pk", "first_name", "last_name")
        for user_pk, email, profile_pk, first_name, last_name in rows:
            try:
                key = normalize_email(email or "")
            except ValidationError:
                continue
            self.by_email[key].append({"user": user_pk, "profile": str(profile_pk),
                                       "names": {(normalize_name(first_name), normalize_name(last_name))}})


def match_owner(index: OwnerIndex, record: dict) -> Decision:
    data = record.get("data") or {}
    try:
        email = normalize_email(data.get("email") or "")
    except Exception:  # noqa: BLE001 — a blank or invalid address matches nobody
        email = ""
    archived = record.get("archived") or data.get("active") is False
    hits = index.by_email.get(email, []) if email else []
    if not hits:
        return Decision(CREATE, CONFIDENCE_SURE, flags=[FLAG_ARCHIVED_OWNER] if archived else [])
    candidates = [{"id": h["profile"], "rule": "owner_email", "confidence": float(OWNER_EMAIL_CONFIDENCE)}
                  for h in hits]
    evidence = [{"rule": "owner_email", "value": mask("R1", email)}]
    if len(hits) > 1:
        return Decision(None, CONFIDENCE_NONE, candidates=candidates, flags=[FLAG_AMBIGUOUS], evidence=evidence)
    confidence = min(OWNER_EMAIL_CONFIDENCE, ARCHIVED_OWNER_CAP) if archived else OWNER_EMAIL_CONFIDENCE
    flags = [FLAG_ARCHIVED_OWNER] if archived else []
    if names_disagree({(normalize_name(data.get("given_name")), normalize_name(data.get("family_name")))},
                      hits[0]["names"]):
        flags.append(FLAG_NAME_MISMATCH)
    if is_role_address(email):
        flags.append(FLAG_ROLE_ADDRESS)
    return Decision(LINK, confidence, target={"model": PROFILE_MODEL, "id": hits[0]["profile"]},
                    rule="owner_email", candidates=candidates, evidence=evidence, flags=flags)


class PipelineIndex:
    """The tenant's pipelines by key, with their stage keys."""

    def __init__(self, company):
        from crm.models import Pipeline, Stage

        self.by_key: dict[str, dict] = {}
        for pk, key in Pipeline.objects.filter(company=company).values_list("pk", "key"):
            self.by_key[key] = {"id": str(pk), "stages": set()}
        by_pk = {v["id"]: v for v in self.by_key.values()}
        for pipeline_id, key in Stage.objects.filter(company=company).values_list("pipeline_id", "key"):
            if str(pipeline_id) in by_pk:
                by_pk[str(pipeline_id)]["stages"].add(key)


def match_pipeline(index: PipelineIndex, record: dict, *, tickets_as_engagements: bool = False) -> Decision:
    data = record.get("data") or {}
    stages = data.get("stages") or []
    flags = [FLAG_STAGE_OUTCOME_CLOSED] if any(s.get("outcome") == "closed" for s in stages) else []
    if data.get("applies_to") == "ticket" and not tickets_as_engagements:
        # Tickets become relationships (2.8): their pipeline is kept, never
        # merged into a golden engagement pipeline.
        return Decision(CREATE, CONFIDENCE_SURE, flags=flags)
    existing = index.by_key.get(data.get("key") or "")
    if existing is None:
        return Decision(CREATE, CONFIDENCE_SURE, flags=flags)
    incoming = {s.get("key") for s in stages}
    candidate = {"id": existing["id"], "rule": "pipeline_key", "confidence": 1.0}
    if existing["stages"] <= incoming:
        return Decision(LINK, CONFIDENCE_SURE, target={"model": PIPELINE_MODEL, "id": existing["id"]},
                        rule="pipeline_key", candidates=[candidate], flags=flags)
    diff = {"missing_stages": sorted(existing["stages"] - incoming), "new_stages": sorted(incoming - existing["stages"])}
    return Decision(None, CONFIDENCE_NONE, candidates=[candidate], rule="pipeline_key",
                    flags=[FLAG_STAGE_KEYS_DIFFER, *flags], diff=diff)


# --- the per-chunk context --------------------------------------------------------


@dataclass
class _Chunk:
    refs: dict  # (object_type, source_id) -> ref dict
    erased_keys: set  # (object_type, source_id) of erased refs, hashed ids included
    erased_hashes: set  # address hashes with an active erasure suppression
    targets: dict  # (content_type_id, object_id str) -> target row or None
    contact_points: dict  # party id str -> {"emails": set, "phones": set}
    previous: dict  # source_id -> previous applied StagedRow (newest job first)
    already_linked: set  # party ids holding a ref of this source+type for another record


class MatchContext:
    """Everything ``decide`` needs for one (company, source, object type),
    loaded with a fixed number of queries per chunk."""

    def __init__(self, company, object_type: str, *, source: str, previous_jobs=(), options=None):
        self.company = company
        self.object_type = object_type
        self.source = source
        self.provider = source.split(":", 1)[0]
        self.previous_jobs = [getattr(j, "pk", j) for j in previous_jobs]  # newest first
        self.options = dict(options or {})
        self._parties: PartyIndex | None = None
        self._owners: OwnerIndex | None = None
        self._pipelines: PipelineIndex | None = None
        self.job = None  # set by the stager: the deal flags read the job's other staged types
        self._deal_index: dict | None = None
        self.seen: set[str] = set()  # source ids (and aliases) already decided in this batch
        self.incoming: dict[tuple, list[str]] = defaultdict(list)  # duplicate hints between incoming records

    # Lazily loaded tenant indexes (once per batch).
    @property
    def parties(self) -> PartyIndex:
        if self._parties is None:
            self._parties = PartyIndex(self.company)
        return self._parties

    @property
    def owners(self) -> OwnerIndex:
        if self._owners is None:
            self._owners = OwnerIndex(self.company)
        return self._owners

    @property
    def pipelines(self) -> PipelineIndex:
        if self._pipelines is None:
            self._pipelines = PipelineIndex(self.company)
        return self._pipelines

    def warm(self) -> None:
        """Load the tenant index this type needs now (so per-chunk counts are flat)."""
        if self.object_type in PERSON_TYPES + ORG_TYPES:
            self.parties  # noqa: B018
        elif self.object_type == "owner":
            self.owners  # noqa: B018
        elif self.object_type == "pipeline":
            self.pipelines  # noqa: B018

    # --- per chunk -----------------------------------------------------------------

    def prefetch(self, records: list[dict]) -> _Chunk:
        from core.models import ExternalRef

        records = [r for r in records if not is_erased(r)]
        company_id = self.company.pk
        object_types = {r.get("source_object_type") or r["object_type"] for r in records}
        ids = {k for r in records for k in record_keys(r)}
        lookup = ids | {erased_source_id(i) for i in ids}
        refs, erased = {}, set()
        for sid, otype, ct_id, obj_id, meta in ExternalRef._base_manager.filter(
            company_id=company_id, source=self.source, object_type__in=object_types, source_id__in=lookup,
        ).values_list("source_id", "object_type", "content_type_id", "object_id", "metadata"):
            if (isinstance(meta, dict) and meta.get("erased")) or sid.startswith("erased:"):
                erased.add((otype, sid))
            else:
                refs[(otype, sid)] = {"content_type_id": ct_id, "object_id": str(obj_id)}
        targets = self._load_targets(refs.values())
        contact_points = self._load_contact_points(targets)
        return _Chunk(
            refs=refs, erased_keys=erased, erased_hashes=self._erased_hashes(records), targets=targets,
            contact_points=contact_points, previous=self._load_previous(records),
            already_linked=self._already_linked(records),
        )

    def _erased_hashes(self, records) -> set:
        """Address hashes of this chunk's emails and phones under an active
        erasure suppression (``parties.erasure.erased_address``, in bulk)."""
        if self.object_type not in ERASABLE_TYPES:
            return set()
        from parties.models import Suppression

        hashes = set()
        for r in records:
            hashes |= address_hashes(self.object_type, r)
        if not hashes:
            return set()
        from parties.erasure import erasure_suppression_q

        return set(Suppression._base_manager.filter(
            erasure_suppression_q(), company_id=self.company.pk, lifted_at__isnull=True, address_hash__in=hashes,
        ).values_list("address_hash", flat=True))

    def _load_targets(self, refs) -> dict:
        """ExternalRef targets, loaded only from this company; a merged party
        resolves to its survivor."""
        by_ct: dict[int, set[str]] = defaultdict(set)
        for ref in refs:
            by_ct[ref["content_type_id"]].add(ref["object_id"])
        out: dict = {}
        for ct_id, ids in by_ct.items():
            model = ContentType.objects.get_for_id(ct_id).model_class()
            if model is None:
                continue
            qs = model._base_manager.filter(pk__in=ids)
            if any(f.name == "company" for f in model._meta.get_fields()):
                qs = qs.filter(company_id=self.company.pk)
            else:
                continue  # an unscoped model cannot prove its tenant: treat as missing
            rows = {str(o.pk): o for o in qs}
            if model._meta.label_lower == PARTY_MODEL:
                rows = self._follow_merges(rows)
                # A chain longer than the hops followed: no survivor known.
                rows = {k: (o if o is None or not o.merged_into_id else None) for k, o in rows.items()}
            for obj_id in ids:
                out[(ct_id, obj_id)] = rows.get(obj_id)
        return out

    def _follow_merges(self, rows: dict) -> dict:
        from parties.models import Party

        for _hop in range(5):
            pending = {k: o.merged_into_id for k, o in rows.items() if o is not None and o.merged_into_id}
            if not pending:
                break
            survivors = {str(p.pk): p for p in Party._base_manager.filter(
                pk__in=set(pending.values()), company_id=self.company.pk)}
            for key, survivor_id in pending.items():
                rows[key] = survivors.get(str(survivor_id))
        return rows

    def _load_contact_points(self, targets: dict) -> dict:
        if self.object_type not in PERSON_TYPES + ORG_TYPES:
            return {}
        from parties.models import PartyEmail, PartyPhone

        ids = {o.pk for o in targets.values() if o is not None}
        out: dict = defaultdict(lambda: {"emails": set(), "phones": set()})
        if not ids:
            return out
        for pid, email in PartyEmail.objects.filter(party_id__in=ids).values_list("party_id", "email_normalized"):
            out[str(pid)]["emails"].add(email)
        for pid, e164, raw in PartyPhone.objects.filter(party_id__in=ids).values_list(
                "party_id", "number_e164", "number_raw"):
            out[str(pid)]["phones"].add(e164 or f"raw:{raw}")
        return out

    def _load_previous(self, records) -> dict:
        """The newest applied staged row of each record in the previous-job chain."""
        if not self.previous_jobs:
            return {}
        from unify.models import StagedRow

        from .stage import job_rows_filter

        rank = {str(j): i for i, j in enumerate(self.previous_jobs)}
        sids = sorted({k for r in records for k in record_keys(r)})
        out: dict = {}
        rows = StagedRow.objects.filter(
            job_rows_filter(self.previous_jobs, self.object_type),
            mapping_state=StagedRow.MappingState.APPLIED, raw__source_id__in=sids,
        ).values_list("raw__source_id", "content_hash", "mapped_values", "raw", "batch__crm_import_job_batch__job_id")
        for sid, digest, mapped, raw, job_id in rows:
            sid = str(sid)
            current = out.get(sid)
            if current is None or rank[str(job_id)] < current["rank"]:
                out[sid] = {"rank": rank[str(job_id)], "content_hash": digest, "mapped": mapped, "raw": raw}
        return out

    def _already_linked(self, records) -> set:
        """Parties that already hold a ref of this source and type: a second
        source record linking to one is a review item, never automatic."""
        if self.object_type not in PERSON_TYPES + ORG_TYPES:
            return set()
        from core.models import ExternalRef

        # Only the candidates' ids matter; collect them from the index once.
        nodes = [record_node(self.company, self.object_type, r) for r in records]
        ids = set()
        for node in nodes:
            for cand in self.parties.candidates(node):
                ids.add(_party_id(cand.key))
        if not ids:
            return set()
        object_types = {r.get("source_object_type") or r["object_type"] for r in records}
        party_ct = ContentType.objects.get_for_model(_party_model())
        return {str(pk) for pk in ExternalRef._base_manager.filter(
            company_id=self.company.pk, source=self.source, object_type__in=object_types,
            content_type=party_ct, object_id__in=ids,
        ).exclude(source_id__startswith="erased:").values_list("object_id", flat=True)}

    # --- decide --------------------------------------------------------------------

    def decide_chunk(self, records: list[dict]) -> list[Decision]:
        chunk = self.prefetch(records)
        return [self._decide(r, chunk) for r in records]

    def _decide(self, record: dict, chunk: _Chunk) -> Decision:
        if is_erased(record):  # erasure already scrubbed this staged row
            return Decision(NA, CONFIDENCE_NONE, reason=ERASED, drop_raw=True)
        otype = record.get("source_object_type") or record["object_type"]
        keys = record_keys(record)
        # 1. An erased source record: never re-created, raw dropped at once (Phase 2 7.5).
        if any((otype, k) in chunk.erased_keys or (otype, erased_source_id(k)) in chunk.erased_keys
               for k in keys):
            self.seen.update(keys)
            return Decision(NA, CONFIDENCE_NONE, reason=ERASED, drop_raw=True)
        # 2. An address golden erased (the person may have been entered by
        # hand), on any record that names it. An organisation is not the
        # person: it loses only the erased contact points.
        stripped = None
        if chunk.erased_hashes and address_hashes(self.object_type, record) & chunk.erased_hashes:
            if self.object_type not in ORG_TYPES:
                self.seen.update(keys)
                return Decision(NA, CONFIDENCE_NONE, reason=ERASED, drop_raw=True)
            record = stripped = strip_erased_points(record, chunk.erased_hashes)
        # 3. The same record twice in one export.
        if any(k in self.seen for k in keys):
            return Decision(NA, CONFIDENCE_NONE, reason=DUPLICATE_IN_EXPORT)
        self.seen.update(keys)
        # 4. Archived in the source. Not owners: an archived owner is still the
        # name on old deals (2.3), and is matched with a capped confidence.
        if record.get("archived") and self.object_type != "owner" and not self.options.get("include_archived"):
            return Decision(NA, CONFIDENCE_NONE, reason=ARCHIVED_IN_SOURCE)
        # 5. A live ExternalRef: re-import (3.4).
        ref = next((chunk.refs[(otype, k)] for k in keys if (otype, k) in chunk.refs), None)
        if ref is not None:
            decision = self._reimport(record, ref, chunk)
            if stripped is not None and not decision.drop_raw:
                decision.replace_raw = stripped
                decision.flags.append(FLAG_ERASED_POINT_REMOVED)
            return decision
        # 6-8. Match against golden, or create.
        decision = self._match_new(record)
        if stripped is not None:
            decision.replace_raw = stripped
            decision.flags.append(FLAG_ERASED_POINT_REMOVED)
        self._hint_duplicates(record, decision)
        if decision.decision == LINK and decision.target and decision.target["model"] == PARTY_MODEL \
                and decision.target["id"] in chunk.already_linked:
            decision.flags.append(FLAG_ALREADY_LINKED)
        if decision.decision == CREATE and (self.object_type == "deal" or (
                self.object_type == "relationship" and self.options.get("tickets_as_engagements"))):
            decision.flags.extend(self.deal_flags(record))
        return decision

    # --- the deal flags (2.5) --------------------------------------------------------

    @property
    def deal_index(self) -> dict:
        """The job's staged pipelines (canonical stage outcomes), primary
        deal associations and owner ids: three queries per batch."""
        if self._deal_index is None:
            self._deal_index = _deal_index(self.job, self.company, self.source)
        return self._deal_index

    def deal_flags(self, record: dict) -> list[str]:
        """``outcome_conflict``, ``closed_at_inferred``, ``party_ambiguous``
        and ``owner_unresolved`` for a deal, from the canonical files alone.
        Needs the job (the stager sets ``ctx.job``); without one, none."""
        if self.job is None:
            return []
        from .appliers.deals import canonical_outcome_conflict

        index = self.deal_index
        data = record.get("data") or {}
        flags = []
        pipeline_sid = str((data.get("pipeline_ref") or {}).get("source_id"))
        stage_sid = str((data.get("stage_ref") or {}).get("source_id"))
        outcome = index["outcomes"].get(pipeline_sid, {}).get(stage_sid)
        if record.get("object_type") == "relationship":  # a ticket: open, or closed with the stage deciding
            status = "open" if data.get("state") == "open" else "closed"
        else:
            status = data.get("status")
        # A closed ticket in a closed-but-unhinted stage waits for the pipeline
        # row's reviewer (stage_outcomes), not for one decision per ticket.
        if canonical_outcome_conflict(status, outcome) and not (status == "closed" and outcome == "closed"):
            flags.append(FLAG_OUTCOME_CONFLICT)
        resolved = status if status in ("open", "won", "lost") else (outcome if outcome in ("won", "lost") else None)
        if resolved in ("won", "lost") and not data.get("closed_at"):
            flags.append(FLAG_CLOSED_AT_INFERRED)
        companies, contacts = data.get("company_refs") or [], data.get("contact_refs") or []
        primary = index["primary"].get(str(record.get("source_id")), {})
        if record.get("object_type") == "deal" and (
                (len(companies) > 1 and "company" not in primary)
                or (not companies and len(contacts) > 1 and "contact" not in primary)):
            flags.append(FLAG_PARTY_AMBIGUOUS)  # a ticket's partner follows 2.8's own order
        owner = data.get("owner_ref")
        if owner and str(owner.get("source_id")) not in index["owners"]:
            flags.append(FLAG_OWNER_UNRESOLVED)
        return flags

    def _match_new(self, record: dict) -> Decision:
        if self.object_type in PERSON_TYPES + ORG_TYPES:
            return match_party(self.parties, record_node(self.company, self.object_type, record))
        if self.object_type == "owner":
            return match_owner(self.owners, record)
        if self.object_type == "pipeline":
            return match_pipeline(self.pipelines, record,
                                  tickets_as_engagements=bool(self.options.get("tickets_as_engagements")))
        return Decision(CREATE, CONFIDENCE_SURE)

    def _hint_duplicates(self, record: dict, decision: Decision) -> None:
        """``possible_duplicate_of``: earlier incoming records sharing an email,
        phone, organisation name or domain (3.3). A hint, never a merge."""
        if self.object_type not in PERSON_TYPES + ORG_TYPES:
            return
        node = record_node(self.company, self.object_type, record)
        sid = str(record["source_id"])
        hints: list[str] = []
        for key in _blocking_keys(node):
            if key[0] == "r4":
                continue
            for other in self.incoming[key]:
                if other != sid and other not in hints:
                    hints.append(other)
            self.incoming[key].append(sid)
        decision.possible_duplicate_of = hints[:MAX_DUPLICATE_HINTS]

    def _reimport(self, record: dict, ref: dict, chunk: _Chunk) -> Decision:
        target_obj = chunk.targets.get((ref["content_type_id"], ref["object_id"]))
        model_class = ContentType.objects.get_for_id(ref["content_type_id"]).model_class()
        model_label = model_class._meta.label_lower if model_class is not None else "unknown"
        if target_obj is None:
            return Decision(None, CONFIDENCE_NONE, flags=[FLAG_TARGET_MISSING],
                            target={"model": model_label, "id": ref["object_id"]})
        if model_label == PARTY_MODEL and target_obj.erased_at is not None:
            return Decision(NA, CONFIDENCE_NONE, reason=ERASED, drop_raw=True)
        target = {"model": model_label, "id": str(target_obj.pk)}
        source_values = compare.project(self.object_type, record)
        # The previous import of this record, under its id or an alias (a
        # record merged inside the provider keeps the old ids as aliases).
        previous = next((chunk.previous[k] for k in record_keys(record) if k in chunk.previous), None)
        if previous is not None and previous["content_hash"] == staged_hash(record):
            return Decision(NA, CONFIDENCE_NONE, reason=UNCHANGED, target=target, rule="external_ref")
        previous_values = None
        if previous is not None:
            previous_values = (previous["mapped"] or {}).get("values")
            if previous_values is None and isinstance(previous["raw"], dict) and "data" in previous["raw"]:
                previous_values = compare.project(self.object_type, previous["raw"])
        golden = None
        if compare.TARGET_MODELS.get(self.object_type) == model_label:
            points = chunk.contact_points.get(str(target_obj.pk), {"emails": set(), "phones": set()})
            golden = compare.golden_values(self.object_type, target_obj, provider=self.provider,
                                           emails=points["emails"], phones=points["phones"])
        diff = compare.three_way(self.object_type, previous_values, golden, source_values)
        diff["previous_import"] = previous is not None
        result = compare.outcome(diff)
        if result == compare.CONFLICT:
            return Decision(CONFLICT, CONFIDENCE_NONE, target=target, rule="external_ref", diff=diff)
        if result == compare.UPDATE:
            return Decision(UPDATE, CONFIDENCE_SURE, target=target, rule="external_ref", diff=diff)
        return Decision(NA, CONFIDENCE_NONE, reason=UNCHANGED, target=target, rule="external_ref", diff=diff)


def _deal_index(job, company, source) -> dict:
    from core.models import ExternalRef
    from unify.models import StagedRow

    from .stage import job_rows_filter

    outcomes: dict[str, dict[str, str]] = {}
    for raw in StagedRow.objects.filter(job_rows_filter([job.pk], "pipeline")).values_list("raw", flat=True):
        data = (raw or {}).get("data") or {}
        outcomes[str((raw or {}).get("source_id"))] = {
            str(s.get("source_id")): s.get("outcome") or "" for s in data.get("stages") or []}
    primary: dict[str, dict[str, str]] = defaultdict(dict)
    for raw in StagedRow.objects.filter(job_rows_filter([job.pk], "association"),
                                        raw__data__primary=True).values_list("raw", flat=True):
        data = (raw or {}).get("data") or {}
        a, b = data.get("from_ref") or {}, data.get("to_ref") or {}
        for mine, other in ((a, b), (b, a)):
            if mine.get("object_type") == "deal" and other.get("object_type") in ("contact", "company"):
                primary[str(mine.get("source_id"))].setdefault(other["object_type"], str(other.get("source_id")))
    owners = set()
    for raw, state in StagedRow.objects.filter(job_rows_filter([job.pk], "owner")).values_list(
            "raw", "mapping_state"):
        if state != StagedRow.MappingState.NA and isinstance(raw, dict) and raw.get("source_id") is not None:
            owners.add(str(raw["source_id"]))
            owners.update(str(a) for a in raw.get("aliases") or [])
    owners.update(str(sid) for sid in ExternalRef._base_manager.filter(
        company_id=company.pk, source=source, object_type__in=("owner",)).values_list("source_id", flat=True))
    return {"outcomes": outcomes, "primary": dict(primary), "owners": owners}


ERASABLE_TYPES = ("owner", "company", "contact", "communication", "consent")


def is_erased(record) -> bool:
    """A staged row erasure already scrubbed (``{"erased": true, ...}``)."""
    return not isinstance(record, dict) or bool(record.get("erased")) or "source_id" not in record


def strip_erased_points(record: dict, erased_hashes: set) -> dict:
    """``record`` without the emails and phones under an erasure suppression."""
    from parties.consent import address_hash

    out = {**record, "data": dict(record.get("data") or {})}
    region = compare.record_region(record)
    kept = []
    for phone in out["data"].get("phones") or []:
        e164 = compare.contact_phones({"data": {"phones": [phone]}}, region)
        if e164 and not e164[0].startswith("raw:") and address_hash(e164[0]) in erased_hashes:
            continue
        kept.append(phone)
    out["data"]["phones"] = kept  # companies carry no emails in the canonical format
    return out


def address_hashes(object_type: str, record: dict) -> set[str]:
    """``Suppression.address_hash`` of every email or phone the record names:
    a contact's or company's contact points, an owner's email, a consent
    fact's address, a communication's participants."""
    from parties.consent import address_hash, normalize_address

    data = record.get("data") or {}
    points: list[str] = []
    if object_type in ("contact", "company"):
        points = compare.contact_emails(record) + [
            p for p in compare.contact_phones(record) if not p.startswith("raw:")]
    elif object_type == "owner":
        points = [normalize_address("email", data.get("email") or "")]
    elif object_type == "consent":
        points = [normalize_address(data.get("channel") or "", data.get("address") or "")]
    elif object_type == "communication":
        for participant in data.get("participants") or []:
            address = participant.get("address") or ""
            points.append(normalize_address("email", address) if "@" in address
                          else normalize_address("phone", address))
    return {address_hash(p) for p in points if p}


def _party_model():
    from parties.models import Party

    return Party


def staged_hash(record: dict) -> str:
    """``StagedRow.content_hash``: the record minus ``raw_ref`` (1.5), so a
    re-export that only moved a record to another page is unchanged."""
    from unify.models import content_hash

    return content_hash({k: v for k, v in record.items() if k != "raw_ref"})


def decide(company, record: dict, *, source: str | None = None, previous_jobs=(), options=None) -> Decision:
    """The decision for one canonical record (3.1). For many records use
    ``MatchContext.decide_chunk``: same answers, constant queries per chunk."""
    ctx = MatchContext(company, record["object_type"], source=source or record["source"],
                       previous_jobs=previous_jobs, options=options)
    return ctx.decide_chunk([record])[0]


# --- bulk eligibility (4.8) -------------------------------------------------------

AUTO_APPROVE_THRESHOLD = Decimal("0.950")  # unify.propose.AUTO_APPLY_THRESHOLD, shared with products


def bulk_approvable(proposal: dict, confidence) -> bool:
    """At or above 0.95, a decision to act on, no flag, never an R6 link, never
    a conflict, never an AI disagreement (I7, 4.8)."""
    if confidence is None or Decimal(str(confidence)) < AUTO_APPROVE_THRESHOLD:
        return False
    if proposal.get("decision") not in (CREATE, LINK, UPDATE):
        return False
    if proposal.get("flags") or proposal.get("rule") == "R6":
        return False
    return not (proposal.get("ai") or {}).get("disagrees")
