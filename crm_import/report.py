"""The import report: JSON for files and machines, rendered by the job's report page (spec 4.4).

``build(job)`` reads the job's staged rows, its manifest and its write log,
and returns one dict. It holds **counts, codes and names only**: the source
id, object types, coverage and reason codes, decisions, N/A reasons, error
codes (the part of a row error before ``:``; never the message, which can
name a person), flags, field and property names (schema, not values), and
per-currency sums of known deal amounts. No address, name, phone, note body
or field value is ever copied into it, so the file can sit next to an export
and the page needs no PII permission.

Per object type, in step order:

- ``coverage``: the manifest's verdict and reason codes; ``exact`` is false
  unless coverage is ``complete``, and every count of that type is then "at
  least" (1.3);
- ``applied``: created / linked / updated (by the approved decision);
- ``unchanged`` and ``skipped`` (N/A by reason);
- ``need_review`` (undecided, by proposed decision; ``unmatched`` for none);
- ``approved_pending`` (decided, not applied yet);
- ``failed`` by code, and ``dependency_pending``: in a dry run a
  ``dependency_missing`` failure is a row waiting for an upstream row that
  needs a human (4.4), so it is counted there instead;
- ``flags``, and the fields an ``update`` / ``conflict`` names;
- ``properties``: the source properties kept in the sidecar without a
  mapping line (``unmapped_kept``) and the ones the mapping drops;
- ``state`` (``applied`` / ``ready`` / ``blocked`` / ``in_review`` /
  ``failed`` / ``empty``) with the step-order gate's blockers;
- ``undo``: applied rows, whether the write log can still undo them, and the
  later steps that must be undone first (4.5).

Plus ``deals`` (source outcome counts, closed deals without stage history,
deals without party, amount unknown, known amounts per currency "at least"),
``consent`` (``appliers.consent.report``) and ``consequence``: the 2.11
line, with the number of contacts it applies to.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db.models import Count
from django.utils import timezone

from unify.apply import ERASED_SOURCE_ERROR
from unify.models import StagedRow

from .appliers.base import ERROR_CODES
from .models import STEPS, ImportWriteLog

SCHEMA_VERSION = 1
MS = StagedRow.MappingState
STEP_ORDER = tuple(sorted(STEPS, key=STEPS.get))
APPLIED_KEYS = {"create": "created", "link": "linked", "update": "updated"}
UNCHANGED = "unchanged"
DEPENDENCY_MISSING = "dependency_missing"
CLOSED = ("won", "lost")


def build(job, *, dry_run: bool = False, steps: list | None = None, generated_at=None) -> dict:
    """The report of ``job`` as it stands (inside a dry run's transaction: as it would be)."""
    from . import runner
    from .appliers.consent import CONSEQUENCE
    from .appliers.consent import report as consent_report

    counts = runner.state_counts(job)
    manifest_objects = (job.manifest or {}).get("objects") or {}
    types = {otype: _empty(otype, manifest_objects.get(otype) or {}) for otype in STEP_ORDER}
    _count_rows(job, types, dry_run=dry_run)
    _count_properties(job, types)
    _count_undo(job, types, counts)
    for otype, item in types.items():
        item["state"], item["blocked_by"] = _state(job, otype, item, counts)
    consent = consent_report(job)
    out = {
        "schema_version": SCHEMA_VERSION,
        "job": str(job.pk),
        "source": job.source,
        "provider": job.provider,
        "adapter_version": job.adapter_version,
        "status": job.status,
        "coverage": job.coverage,
        "dry_run": dry_run,
        "generated_at": (generated_at or timezone.now()).isoformat().replace("+00:00", "Z"),
        "types": types,
        "deals": _deals(job),
        "consent": {k: v for k, v in consent.items() if k != "consequence"},
        "consequence": {
            "text": CONSEQUENCE,
            "contacts": consent["contacts"],
            "contacts_no_consent_for_marketing": consent["contacts_no_consent_for_marketing"],
        },
        "totals": _totals(types),
        "undo": {
            "job_available": ImportWriteLog._base_manager.filter(job_id=job.pk, undone_at__isnull=True).exists(),
        },
    }
    if steps is not None:
        out["run"] = {"steps": steps}
    return out


def _empty(otype: str, manifest_entry: dict) -> dict:
    coverage = manifest_entry.get("coverage") or "unknown"
    return {
        "step": STEPS[otype],
        "coverage": {
            "coverage": coverage,
            "exact": coverage == "complete",
            "rows_in_export": manifest_entry.get("rows"),
            "declared_total": manifest_entry.get("declared_total"),
            "reasons": sorted({r.get("code") for r in manifest_entry.get("reasons") or [] if r.get("code")}),
        },
        "rows": 0,
        "applied": {"created": 0, "linked": 0, "updated": 0, "other": 0, "total": 0},
        "unchanged": 0,
        "skipped": {},
        "need_review": {"total": 0, "by_decision": {}},
        "approved_pending": 0,
        "failed": {"total": 0, "by_code": {}},
        "dependency_pending": 0,
        "flags": {},
        "fields": {"update": {}, "conflict": {}},
        "properties": {"unmapped_kept": [], "dropped": []},
        "undo": {"applied_rows": 0, "available": False, "undo_first": []},
    }


def _bump(d: dict, key, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


def _count_rows(job, types: dict, *, dry_run: bool) -> None:
    rows = StagedRow.objects.filter(batch__crm_import_job_batch__job=job).values_list(
        "batch__crm_import_job_batch__object_type", "mapping_state", "proposed_mapping__decision",
        "proposed_mapping__reason", "proposed_mapping__flags", "proposed_mapping__diff__fields",
        "mapped_values__decision", "error",
    )
    for otype, state, proposed, reason, flags, diff_fields, decided, error in rows.iterator():
        item = types[otype]
        item["rows"] += 1
        if state == MS.NA:
            if reason is None and error == ERASED_SOURCE_ERROR:
                reason = "erased"  # skipped by unify.apply's erased-source check
            if reason == UNCHANGED:
                item["unchanged"] += 1
            else:
                _bump(item["skipped"], reason or "unknown")
            continue
        for flag in flags or ():
            _bump(item["flags"], flag)
        for name, change in (diff_fields or {}).items():
            if change in ("update", "conflict"):
                _bump(item["fields"][change], name)
        if state == MS.APPLIED:
            key = APPLIED_KEYS.get(decided or "", "other")
            item["applied"][key] += 1
            item["applied"]["total"] += 1
        elif state in (MS.PROPOSED, MS.UNMAPPED):
            item["need_review"]["total"] += 1
            _bump(item["need_review"]["by_decision"], proposed if proposed and state == MS.PROPOSED else "unmatched")
        elif state == MS.APPROVED:
            item["approved_pending"] += 1
        elif state == MS.FAILED:
            code = (error or "").split(":", 1)[0].strip()
            if code not in ERROR_CODES:  # not a coded error: never copy the message
                code = "error"
            if dry_run and code == DEPENDENCY_MISSING:
                item["dependency_pending"] += 1
                continue
            item["failed"]["total"] += 1
            _bump(item["failed"]["by_code"], code)


def _count_properties(job, types: dict) -> None:
    """Source property names with no mapping line (kept in the sidecar) and those dropped."""
    mapping = job.mapping or {}
    rows = StagedRow.objects.filter(batch__crm_import_job_batch__job=job).exclude(
        mapping_state=MS.NA).values_list(
        "batch__crm_import_job_batch__object_type", "raw__source_object_type", "raw__properties")
    seen: dict[str, dict[str, set]] = {}
    for otype, source_type, properties in rows.iterator():
        if not isinstance(properties, dict):
            continue
        table = ((mapping.get(source_type or otype) or {}).get("properties") or {}) if isinstance(
            mapping.get(source_type or otype), dict) else {}
        bucket = seen.setdefault(otype, {"unmapped_kept": set(), "dropped": set()})
        for name in properties:
            target = table.get(name)
            if target is None:
                bucket["unmapped_kept"].add(name)
            elif target == "drop":
                bucket["dropped"].add(name)
    for otype, bucket in seen.items():
        types[otype]["properties"] = {k: sorted(v) for k, v in bucket.items()}


def _count_undo(job, types: dict, counts: dict) -> None:
    from . import runner

    live = dict(
        ImportWriteLog._base_manager.filter(job_id=job.pk, undone_at__isnull=True)
        .values("approval_row__run__batch__crm_import_job_batch__object_type")
        .annotate(n=Count("id"))
        .values_list("approval_row__run__batch__crm_import_job_batch__object_type", "n")
    )
    for otype, item in types.items():
        applied = counts[otype].get(MS.APPLIED, 0)
        first = sorted(runner.undo_blockers(job, otype, counts))
        item["undo"] = {"applied_rows": applied, "available": bool(applied and live.get(otype) and not first),
                        "undo_first": first}


def _state(job, otype: str, item: dict, counts: dict) -> tuple[str, list]:
    from . import runner

    if item["approved_pending"]:
        blockers = runner.gate(job, otype, counts)
        return ("blocked" if blockers else "ready"), sorted(blockers)
    if item["need_review"]["total"]:
        return "in_review", []
    if item["failed"]["total"] or item["dependency_pending"]:
        return "failed", []
    return ("applied" if item["rows"] else "empty"), []


def _deals(job) -> dict:
    source_status: dict[str, int] = {}
    amounts: dict[str, Decimal] = {}
    out = {"rows": 0, "amount_unknown": 0, "without_party": 0, "closed_without_history": 0}
    rows = StagedRow.objects.filter(batch__crm_import_job_batch__job=job,
                                    batch__crm_import_job_batch__object_type="deal").values_list(
        "raw__data", "raw__history")
    for data, history in rows.iterator():
        if not isinstance(data, dict):  # an erased record: nothing left to count
            continue
        out["rows"] += 1
        status = data.get("status") or "unknown"
        _bump(source_status, status)
        amount = data.get("amount")
        try:
            value = Decimal(str(amount)) if amount not in (None, "") else None
        except InvalidOperation:
            value = None
        if value is None:
            out["amount_unknown"] += 1
        else:
            currency = data.get("currency") or "unknown"
            amounts[currency] = amounts.get(currency, Decimal("0")) + value
        if not data.get("company_refs") and not data.get("contact_refs"):
            out["without_party"] += 1
        if status in CLOSED and not any(
                (h or {}).get("property") == "dealstage" for h in history or ()):
            out["closed_without_history"] += 1
    out["source_status"] = source_status
    # "At least": a deal without an amount is not zero (2.5), so a sum is a floor.
    out["known_amounts_at_least"] = {c: _decimal_text(v) for c, v in sorted(amounts.items())}
    return out


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _totals(types: dict) -> dict:
    total = {"rows": 0, "applied": 0, "unchanged": 0, "skipped": 0, "need_review": 0,
             "approved_pending": 0, "failed": 0, "dependency_pending": 0}
    for item in types.values():
        total["rows"] += item["rows"]
        total["applied"] += item["applied"]["total"]
        total["unchanged"] += item["unchanged"]
        total["skipped"] += sum(item["skipped"].values())
        total["need_review"] += item["need_review"]["total"]
        total["approved_pending"] += item["approved_pending"]
        total["failed"] += item["failed"]["total"]
        total["dependency_pending"] += item["dependency_pending"]
    total["exact"] = all(item["coverage"]["exact"] for item in types.values())
    return total


def summary(report: dict) -> dict:
    """What the job keeps of its last run (``ImportJob.report["last_run"]``)."""
    return {"generated_at": report["generated_at"], "status": report["status"], "totals": report["totals"],
            "steps": (report.get("run") or {}).get("steps")}


def dumps(report: dict) -> str:
    """Stable JSON: sorted keys, two-space indent, a final newline (diffable)."""
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write(report: dict, path) -> Path:
    path = Path(path)
    path.write_text(dumps(report), encoding="utf-8")
    return path
