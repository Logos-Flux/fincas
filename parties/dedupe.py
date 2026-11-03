"""Dedupe rule engine (docs/crm/phase2-party.md 3.3, 3.4).

Pure: no database access. Callers turn their rows into ``Node`` objects
(one node per would-be party), and ``find_clusters`` blocks them on each
rule's key, unions the blocks into clusters and scores them. Step 4's
``party_dedupe_report`` feeds it legacy Customer / CustomerContact / Lead
rows (``parties.dedupe_legacy``); steps 5 and 6 can feed it Party rows.

Rules (3.3):

====  =================================================================  ====
Id    Evidence                                                           Conf
====  =================================================================  ====
R1    same normalised email                                              0.95
R2    same E.164 and same normalised family name                         0.85
R3    same E.164, family names differ or are blank                       0.60
R4    same given + family name, same postal code + country on an address 0.50
R5    organisations: same normalised legal/display name                  0.70
R6    organisations: same primary domain (never bulk-approvable)         0.30
C1    a converted lead and the customer it converted to (2.5)            0.99
====  =================================================================  ====

A1 / A2 (a contact that *is* its customer's account holder, or two contacts
on one customer sharing an email) are applied before blocking: the caller
folds those rows into one node and records the fold on ``Node.applied``.

Every node belongs to exactly one tenant, blocking keys are prefixed with
it, and a C1 link to a node of another tenant is ignored, so no cluster
ever spans two companies (3.1).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from .constants import ORGANISATION, PERSON

RULES: dict[str, dict] = {
    "C1": {"confidence": 0.99, "kind": PERSON, "description": "converted lead and its customer"},
    "R1": {"confidence": 0.95, "kind": PERSON, "description": "same normalized email"},
    "R2": {"confidence": 0.85, "kind": PERSON, "description": "same E.164 and same family name"},
    "R3": {"confidence": 0.60, "kind": PERSON, "description": "same E.164 only"},
    "R4": {"confidence": 0.50, "kind": PERSON, "description": "same given+family name and postal code+country"},
    "R5": {"confidence": 0.70, "kind": ORGANISATION, "description": "same normalized organization name"},
    "R6": {"confidence": 0.30, "kind": ORGANISATION, "description": "same primary domain"},
}
APPLIED_RULES = {
    "A1": "contact email equals its own customer's email: same party",
    "A2": "contacts on one customer share an email: same party",
}

# Cluster flags (3.3 last row, 3.4 "blocked merges").
FLAG_NEEDS_INDIVIDUAL_REVIEW = "needs_individual_review"
BLOCK_TWO_ACCOUNT_HOLDERS = "two_account_holders"
BLOCK_DIFFERENT_USERS = "different_users"
BLOCK_SELF_PARTY = "self_party"


@dataclass
class Node:
    """One would-be party.

    ``key`` is a stable id such as ``"customer:<uuid>"``; ``sources`` lists
    every source row folded into it (A1/A2). Contact points are already
    normalised by the caller; blank values are simply absent.
    """

    key: str
    company: str
    kind: str = PERSON
    sources: list[tuple[str, str]] = field(default_factory=list)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)  # E.164
    # (normalised given, normalised family) pairs; a folded node may carry more than one
    names: set[tuple[str, str]] = field(default_factory=set)
    postcodes: set[tuple[str, str]] = field(default_factory=set)  # (postcode, country)
    org_names: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    links: set[str] = field(default_factory=set)  # node keys for C1
    is_account_holder: bool = False
    user: str = ""  # login identity, if any
    is_staff: bool = False
    is_self: bool = False
    applied: dict[str, int] = field(default_factory=dict)  # {"A1": n, "A2": n}
    display: dict = field(default_factory=dict)  # PII for --show-pii only


@dataclass
class Evidence:
    rule: str
    value: str  # the blocking key value (PII); hide unless asked
    members: tuple[str, ...]

    @property
    def confidence(self) -> float:
        return RULES[self.rule]["confidence"]


@dataclass
class Cluster:
    id: str
    company: str
    kind: str
    members: list[str]
    evidence: list[Evidence]
    confidence: float  # strongest rule (3.3)
    rule: str
    weakest_link: float  # lowest confidence still needed to hold the cluster together
    weakest_rule: str
    flags: list[str]
    blocked: list[str]

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def bulk_approvable(self) -> bool:
        """No flag, no block, and not held together by an R6 link."""
        return not self.flags and not self.blocked and self.weakest_rule != "R6"


def cluster_id(member_keys) -> str:
    """Hash of the sorted member keys: the future StagedRow.content_hash (3.4)."""
    return hashlib.sha256("\n".join(sorted(member_keys)).encode()).hexdigest()[:16]


def _blocks(nodes: list[Node], by_key: dict[str, Node]) -> list[Evidence]:
    """One Evidence per blocking key shared by two or more nodes."""
    groups: dict[tuple, set[str]] = defaultdict(set)
    for n in nodes:
        c = n.company
        if n.kind == PERSON:
            for e in n.emails:
                groups[("R1", c, e)].add(n.key)
            for p in n.phones:
                groups[("PHONE", c, p)].add(n.key)
                for _given, family in n.names:
                    if family:
                        groups[("R2", c, f"{p}|{family}")].add(n.key)
            for given, family in n.names:
                if given and family:
                    for postcode, country in n.postcodes:
                        groups[("R4", c, f"{given} {family}|{postcode}|{country}")].add(n.key)
            for other in n.links:
                if other not in by_key or by_key[other].company != c:
                    continue  # a link to another tenant (or outside the input) is never evidence
                groups[("C1", c, "|".join(sorted((n.key, other))))].update((n.key, other))
        else:
            for name in n.org_names:
                groups[("R5", c, name)].add(n.key)
            for d in n.domains:
                groups[("R6", c, d)].add(n.key)

    evidence: list[Evidence] = []
    r2_covered: dict[str, list[set[str]]] = defaultdict(list)
    for (rule, c, value), members in groups.items():
        if rule == "R2" and len(members) > 1:
            r2_covered[f"{c}|{value.split('|', 1)[0]}"].append(members)
    for (rule, c, value), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        if rule == "PHONE":
            # R3 only where the shared number is not fully explained by R2.
            if any(members <= sub for sub in r2_covered.get(f"{c}|{value}", [])):
                continue
            rule = "R3"
        evidence.append(Evidence(rule, value, tuple(sorted(members))))
    return evidence


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[max(ra, rb)] = min(ra, rb)
        return True


def find_clusters(nodes: list[Node]) -> list[Cluster]:
    """Block, union and score. Clusters come back largest first, then by
    confidence, then by id, so the order is deterministic."""
    by_key = {n.key: n for n in nodes}
    evidence = _blocks(nodes, by_key)

    uf = _UnionFind()
    for ev in evidence:
        for m in ev.members[1:]:
            uf.union(ev.members[0], m)
    clusters: dict[str, list[str]] = defaultdict(list)
    for key in by_key:
        if key in uf.parent:
            clusters[uf.find(key)].append(key)
    ev_by_root: dict[str, list[Evidence]] = defaultdict(list)
    for ev in evidence:
        ev_by_root[uf.find(ev.members[0])].append(ev)

    out: list[Cluster] = []
    for root, members in clusters.items():
        members = sorted(members)
        evs = sorted(ev_by_root[root], key=lambda e: (-e.confidence, e.rule, e.members))
        strongest = evs[0]
        weak = _weakest_link(members, evs)
        member_nodes = [by_key[m] for m in members]
        flags, blocked = _flags(member_nodes)
        out.append(Cluster(
            id=cluster_id(members),
            company=member_nodes[0].company,
            kind=member_nodes[0].kind,
            members=members,
            evidence=evs,
            confidence=strongest.confidence,
            rule=strongest.rule,
            weakest_link=weak.confidence,
            weakest_rule=weak.rule,
            flags=flags,
            blocked=blocked,
        ))
    out.sort(key=lambda c: (-c.size, -c.confidence, c.id))
    return out


def _weakest_link(members: list[str], evs: list[Evidence]) -> Evidence:
    """The weakest evidence on the strongest-first spanning forest."""
    uf = _UnionFind()
    last = evs[0]
    remaining = len(members) - 1
    for ev in evs:  # already strongest first
        for m in ev.members[1:]:
            if uf.union(ev.members[0], m):
                last = ev
                remaining -= 1
        if remaining == 0:
            break
    return last


def _flags(nodes: list[Node]) -> tuple[list[str], list[str]]:
    holders = sum(1 for n in nodes if n.is_account_holder)
    users = {n.user for n in nodes if n.user}
    blocked = []
    if holders > 1:
        blocked.append(BLOCK_TWO_ACCOUNT_HOLDERS)
    if len(users) > 1:
        blocked.append(BLOCK_DIFFERENT_USERS)
    if any(n.is_self for n in nodes):
        blocked.append(BLOCK_SELF_PARTY)
    flags = []
    if holders > 1 or users or any(n.is_staff for n in nodes):
        flags.append(FLAG_NEEDS_INDIVIDUAL_REVIEW)
    return flags, blocked


# --- near-misses the rules deliberately do not merge (3.3, 7.1) ------------


def folded_email(email: str) -> str:
    """Gmail-style fold (dots and ``+tag`` dropped from the local part).

    Never a match key (3.3): used only to *count* near-misses.
    """
    local, _, domain = email.rpartition("@")
    local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


def email_near_misses(nodes: list[Node]) -> list[tuple[str, ...]]:
    """Groups of person nodes whose emails collide only after folding."""
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    exact: dict[tuple[str, str], set[str]] = defaultdict(set)
    for n in nodes:
        if n.kind != PERSON:
            continue
        for e in n.emails:
            groups[(n.company, folded_email(e))].add(e)
            exact[(n.company, e)].add(n.key)
    out = []
    for (company, _), emails in sorted(groups.items()):
        if len(emails) > 1:
            keys = set()
            for e in emails:
                keys |= exact[(company, e)]
            if len(keys) > 1:
                out.append(tuple(sorted(keys)))
    return out


_SCRIPT_RE = re.compile(r"^(LATIN|CYRILLIC|GREEK|ARMENIAN|HEBREW|ARABIC|CJK|HIRAGANA|KATAKANA|HANGUL)")


def is_mixed_script(text: str) -> bool:
    """True when the letters of ``text`` come from more than one script
    (e.g. Latin plus Cyrillic look-alikes, 7.1). Digits and punctuation
    are ignored."""
    scripts = set()
    for ch in text:
        if not ch.isalpha():
            continue
        m = _SCRIPT_RE.match(unicodedata.name(ch, ""))
        scripts.add(m.group(1) if m else "OTHER")
    return len(scripts) > 1


__all__ = [
    "APPLIED_RULES",
    "BLOCK_DIFFERENT_USERS",
    "BLOCK_SELF_PARTY",
    "BLOCK_TWO_ACCOUNT_HOLDERS",
    "Cluster",
    "Evidence",
    "FLAG_NEEDS_INDIVIDUAL_REVIEW",
    "Node",
    "RULES",
    "cluster_id",
    "email_near_misses",
    "find_clusters",
    "folded_email",
    "is_mixed_script",
]
