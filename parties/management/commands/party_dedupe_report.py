"""``manage.py party_dedupe_report`` (docs/crm/phase2-party.md step 4, D11).

Runs the dedupe rules over Customer, CustomerContact and Lead rows before
any Party exists (``--source legacy``), or over Party rows once the step-5
backfill has run (``--source parties``; ``auto``, the default, picks parties
when any exist), and prints what the step-5 backfill and step-6 queue would
face: counts per rule, a cluster-size histogram, the top clusters, rows no
rule can ever match, and near-misses the rules deliberately leave apart.

Read-only, enforced: any write statement during the run raises. The default
output carries ids only; ``--show-pii`` adds names, emails and the matched
values for an operator.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from contextlib import ExitStack

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.utils import timezone

from core.models import Company
from parties.dedupe import APPLIED_RULES, RULES, email_near_misses, find_clusters
from parties.dedupe_legacy import LegacyStats, has_usable_key, legacy_nodes, null_company_counts
from parties.dedupe_parties import has_parties, party_nodes

SCHEMA = "party_dedupe_report/1"
# Only plain SELECTs pass (leading comments and whitespace ignored). A
# SELECT ... FOR UPDATE takes locks and is refused too.
_LEADING_COMMENTS = re.compile(r"^(\s+|/\*.*?\*/|--[^\n]*\n)*", re.S)
_SELECT = re.compile(r"SELECT\b", re.I)
_FOR_UPDATE = re.compile(r"\bFOR\s+(NO\s+KEY\s+)?UPDATE\b|\bFOR\s+(KEY\s+)?SHARE\b", re.I)


class WriteAttempted(RuntimeError):
    pass


def _refuse_writes(execute, sql, params, many, context):
    body = _LEADING_COMMENTS.sub("", sql or "", count=1)
    if not _SELECT.match(body) or _FOR_UPDATE.search(body):
        raise WriteAttempted(f"party_dedupe_report is read-only; refused: {(sql or '')[:80]}")
    return execute(sql, params, many, context)


def run_read_only(fn):
    """Call ``fn`` with every non-SELECT statement refused on every alias.

    On Postgres the transaction is also ``READ ONLY``, so the server refuses
    any write the regex might miss.
    """
    with ExitStack() as stack:
        for alias in connections:
            conn = connections[alias]
            outermost = not conn.in_atomic_block
            stack.enter_context(transaction.atomic(using=alias))
            if conn.vendor == "postgresql" and outermost:  # must be the transaction's first statement
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
            stack.enter_context(conn.execute_wrapper(_refuse_writes))
        return fn()


def _histogram(sizes) -> dict[str, int]:
    counts = Counter("10+" if s >= 10 else str(s) for s in sizes)
    order = [str(n) for n in range(2, 10)] + ["10+"]
    return {k: counts[k] for k in order if counts[k]}


def build_report(companies, *, show_pii: bool = False, source: str = "legacy") -> dict:
    """The report as a JSON-serialisable dict (schema ``party_dedupe_report/1``).

    ``source`` is ``legacy`` (Customer/Contact/Lead rows) or ``parties``.
    """
    stats = LegacyStats()
    nodes = []
    clusters = []
    build = party_nodes if source == "parties" else legacy_nodes
    for company in companies:  # one engine run per tenant (3.1)
        company_nodes, _ = build(company, stats)
        nodes += company_nodes
        clusters += find_clusters(company_nodes)
    clusters.sort(key=lambda c: (-c.size, -c.confidence, c.id))
    by_key = {n.key: n for n in nodes}

    rules = {}
    for rule, meta in RULES.items():
        rules[rule] = {
            "confidence": meta["confidence"],
            "kind": meta["kind"],
            "description": meta["description"],
            "blocks": 0,  # shared keys (one GROUP BY row each)
            "clusters": 0,  # clusters whose strongest rule this is
        }
    for c in clusters:
        rules[c.rule]["clusters"] += 1
        for ev in c.evidence:
            rules[ev.rule]["blocks"] += 1

    applied = {rule: 0 for rule in APPLIED_RULES}
    for n in nodes:
        for rule, count in n.applied.items():
            applied[rule] += count

    def member(key: str) -> dict:
        n = by_key[key]
        out = {"key": key, "kind": n.kind, "sources": [{"source": s, "id": i} for s, i in n.sources]}
        if show_pii:
            out.update(n.display)
        return out

    flagged = Counter()
    for c in clusters:
        flagged.update(c.flags)
        flagged.update(f"blocked:{b}" for b in c.blocked)

    no_key = sorted(n.key for n in nodes if not has_usable_key(n))
    near = email_near_misses(nodes)
    return {
        "schema": SCHEMA,
        "generated_at": timezone.now().isoformat(),
        "show_pii": show_pii,
        "source": source,
        "companies": [{"id": str(c.pk), "name": c.name if show_pii else None} for c in companies],
        "rows": {
            "customers": stats.rows["customers"],
            "contacts": stats.rows["contacts"],
            "leads": stats.rows["leads"],
        },
        "nodes": {
            "person": sum(1 for n in nodes if n.kind == "person"),
            "organisation": sum(1 for n in nodes if n.kind == "organisation"),
        },
        "applied": applied,
        "rules": rules,
        "clusters_total": len(clusters),
        "nodes_in_clusters": sum(c.size for c in clusters),
        "histogram": _histogram(c.size for c in clusters),
        "review": {
            "bulk_approvable": sum(1 for c in clusters if c.bulk_approvable),
            "needs_individual_review": flagged["needs_individual_review"],
            "blocked": {k.split(":", 1)[1]: v for k, v in sorted(flagged.items()) if k.startswith("blocked:")},
        },
        "unusable": {
            "no_usable_key": no_key,
            "invalid_email": dict(sorted(stats.invalid_email.items())),
            "unparseable_phone": dict(sorted(stats.unparseable_phone.items())),
        },
        "name_inferred_orgs": sorted(stats.name_inferred_orgs),
        "mixed_script_emails": sorted(stats.mixed_script_email),
        "cross_tenant_conversions": sorted(stats.cross_tenant_conversions),
        "near_misses": {"email_folding": [list(g) for g in near]},
        "null_company": null_company_counts(),
        "clusters": [
            {
                "id": c.id,
                "company": c.company,
                "kind": c.kind,
                "size": c.size,
                "rule": c.rule,
                "confidence": c.confidence,
                "weakest_rule": c.weakest_rule,
                "weakest_link": c.weakest_link,
                "bulk_approvable": c.bulk_approvable,
                "flags": c.flags,
                "blocked": c.blocked,
                "members": [member(k) for k in c.members],
                "evidence": [
                    {"rule": ev.rule, "confidence": ev.confidence, "members": list(ev.members)}
                    | ({"value": ev.value} if show_pii else {})
                    for ev in c.evidence
                ],
            }
            for c in clusters
        ],
    }


class Command(BaseCommand):
    help = "Report duplicate-party candidates over Customer, Contact and Lead rows (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company id or exact name; default: every company, each on its own")
        parser.add_argument("--top", type=int, default=20, help="Clusters to print (default 20)")
        parser.add_argument("--json", dest="json_path", help="Also write the full report as JSON to this path")
        parser.add_argument(
            "--source", choices=["auto", "legacy", "parties"], default="auto",
            help="Rows to cluster: legacy Customer/Contact/Lead rows, or Party rows after the step-5 "
                 "backfill (default auto: parties when any exist)",
        )
        parser.add_argument(
            "--show-pii", action="store_true",
            help="Include names, emails and matched values (operator use only)",
        )

    def handle(self, *args, **opts):
        companies = self._companies(opts["company"])
        show_pii = opts["show_pii"]
        source = opts["source"]
        if source == "auto":
            source = "parties" if companies and has_parties(companies) else "legacy"
        report = run_read_only(lambda: build_report(companies, show_pii=show_pii, source=source))
        if opts["json_path"]:
            # Owner-only: the file can carry PII (--show-pii).
            fd = os.open(opts["json_path"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        self._print(report, max(opts["top"], 0))

    @staticmethod
    def _companies(value):
        qs = Company.objects.order_by("name", "id")
        if not value:
            return list(qs)
        try:
            found = list(qs.filter(pk=uuid.UUID(value)))
        except ValueError:
            found = list(qs.filter(name=value))
        if len(found) != 1:
            raise CommandError(f"--company {value!r} matches {len(found)} companies; pass its id.")
        return found

    def _print(self, r: dict, top: int) -> None:
        w = self.stdout.write
        w(f"Party dedupe report ({r['schema']}), {len(r['companies'])} company(ies), source: {r['source']}")
        rows = r["rows"]
        w(f"Rows: {rows['customers']} customers, {rows['contacts']} contacts, {rows['leads']} leads "
          f"-> {r['nodes']['person']} person + {r['nodes']['organisation']} organization candidates")
        w(f"Applied before matching: A1 {r['applied']['A1']}, A2 {r['applied']['A2']}")
        w("")
        w("Rule  Conf  Blocks  Clusters  Description")
        for rule, m in r["rules"].items():
            w(f"{rule:<5} {m['confidence']:.2f}  {m['blocks']:>6}  {m['clusters']:>8}  {m['description']}")
        w("")
        w(f"Clusters: {r['clusters_total']} covering {r['nodes_in_clusters']} candidates")
        hist = ", ".join(f"{k}: {v}" for k, v in r["histogram"].items()) or "none"
        w(f"Cluster sizes: {hist}")
        rev = r["review"]
        blocked = ", ".join(f"{k} {v}" for k, v in rev["blocked"].items()) or "none"
        w(f"Bulk-approvable: {rev['bulk_approvable']}; needs individual review: "
          f"{rev['needs_individual_review']}; blocked: {blocked}")
        un = r["unusable"]
        w(f"No usable key: {len(un['no_usable_key'])}; invalid emails: {sum(un['invalid_email'].values())}; "
          f"unparseable phones: {sum(un['unparseable_phone'].values())}")
        w(f"Organizations named after their person: {len(r['name_inferred_orgs'])}; "
          f"mixed-script emails: {len(r['mixed_script_emails'])}; "
          f"dot/plus near-misses: {len(r['near_misses']['email_folding'])}; "
          f"leads converted to another tenant's customer: {len(r['cross_tenant_conversions'])}")
        nc = r["null_company"]
        w(f"No company (skipped): {nc['customers']} customers, {nc['leads']} leads")
        if top and r["clusters"]:
            w("")
            w(f"Top {min(top, len(r['clusters']))} clusters:")
            for c in r["clusters"][:top]:
                state = "bulk" if c["bulk_approvable"] else ",".join(c["flags"] + c["blocked"]) or "weak-link"
                w(f"  {c['id']}  {c['kind']:<12} size {c['size']}  {c['rule']} {c['confidence']:.2f} "
                  f"(weakest {c['weakest_rule']} {c['weakest_link']:.2f})  [{state}]  company {c['company']}")
                for m in c["members"]:
                    extra = ""
                    if r["show_pii"]:
                        extra = "  " + " ".join(
                            str(v) for v in (m.get("name"), m.get("org_name"), m.get("emails"), m.get("phones")) if v
                        )
                    w(f"      {m['key']}{extra}")
                if r["show_pii"]:
                    for ev in c["evidence"]:
                        w(f"      {ev['rule']}: {ev['value']}")
        if top and un["no_usable_key"]:
            w("")
            w("No usable key (first {}): {}".format(min(top, len(un["no_usable_key"])),
                                                    ", ".join(un["no_usable_key"][:top])))
