"""LLM mapping/dedupe/CoA proposals (Step 4, XD-6).

Every proposal is validated before it touches a row: known fields only,
confidence in [0,1], malformed/refused output leaves the row UNMAPPED with
the error recorded — never applied. Server-side budget (AD-4): each batch
carries ``llm_call_budget``; exhausted budgets refuse loudly. No live model
calls in tests — patch ``unify.propose.complete`` with recorded JSON.
"""

from __future__ import annotations

import json
from decimal import Decimal

from llmhub.client import LLMHubError, complete

from .models import ImportBatch, StagedRow

PRODUCT_FIELDS = {
    "title", "sku", "part_number", "retail_price", "msrp_price",
    "description",
}

MAPPING_SYSTEM = (
    "You map messy CSV headers to product fields. Reply with exactly one "
    "JSON object, no prose: {\"mappings\": {\"<header>\": {\"field\": "
    "\"<field>\", \"confidence\": <0-1>}}, \"row_confidence\": <0-1>}. "
    f"Allowed fields: {sorted(PRODUCT_FIELDS)}. Headers that match nothing "
    "must be omitted, never guessed."
)

DEDUPE_SYSTEM = (
    "You decide whether candidate product rows are the same real product. "
    "Reply with exactly one JSON object, no prose: {\"duplicates\": "
    "[{\"winner_row\": <n>, \"loser_row\": <n>, \"confidence\": <0-1>, "
    "\"reason\": \"<short>\"}]}. Only group rows that are clearly the same "
    "product (same SKU, or same title and price); when unsure, return "
    "{\"duplicates\": []}."
)

COA_SYSTEM = (
    "You map a company's chart of accounts to posting-rule fields. Reply "
    "with exactly one JSON object, no prose: {\"mapping\": {\"<field>\": "
    "\"<account_number>\", ...}, \"confidence\": <0-1>}. Allowed fields: "
    "receivable, revenue, tax, discount, shipping_income, cash, fee, "
    "inventory, payable. Use only account numbers from the chart given."
)


class ProposalError(Exception):
    """A proposal run was refused (budget, unknown purpose, no rows)."""


def _spend(batch: ImportBatch, calls: int = 1):
    batch.llm_calls_used += calls
    # Unsaved holders (CoA mapping outside a batch) enforce the per-call
    # bound only — real accounting lives on persisted batches.
    if batch.pk:
        batch.save(update_fields=["llm_calls_used", "updated_at"])
    if batch.llm_calls_used > batch.llm_call_budget:
        raise ProposalError(
            f"LLM call budget exhausted for {batch.source_name} "
            f"({batch.llm_call_budget} calls). Narrow the batch or raise "
            "the budget — the run stops here, applied rows stay applied."
        )


def _parse_json(text: str, purpose: str) -> dict:
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProposalError(
            f"{purpose} returned non-JSON output — nothing applied. "
            f"Output head: {text[:120]!r}."
        ) from exc
    if not isinstance(data, dict):
        raise ProposalError(
            f"{purpose} returned a JSON non-object — nothing applied."
        )
    return data


def _confidence(value, purpose: str) -> Decimal:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProposalError(
            f"{purpose} returned a non-numeric confidence — nothing applied."
        ) from exc
    if not 0 <= number <= 1:
        raise ProposalError(
            f"{purpose} returned confidence {value} outside [0,1] — "
            "nothing applied."
        )
    return Decimal(str(number)).quantize(Decimal("0.001"))


def propose_mappings(batch: ImportBatch, max_rows: int = 50) -> dict:
    """LLM-propose field mappings for UNMAPPED rows. Returns counts."""
    rows = list(
        batch.rows.filter(mapping_state=StagedRow.MappingState.UNMAPPED)
        .order_by("row_number")[:max_rows]
    )
    if not rows:
        return {"proposed": 0, "skipped": 0}
    counts = {"proposed": 0, "skipped": 0}
    headers = sorted({h for r in rows for h in r.raw})
    for row in rows:
        _spend(batch)
        try:
            completion = complete(
                "mapping_propose",
                [{"role": "user", "content": json.dumps(
                    {"headers": headers, "row": row.raw,
                     "known_headers_hint": "sku,title,price"}
                )}],
                system=MAPPING_SYSTEM,
                max_tokens=1000,
                temperature=0,
            )
            data = _parse_json(completion.text, "mapping_propose")
        except (LLMHubError, ProposalError) as exc:
            row.error = f"{type(exc).__name__}: {exc}"[:1000]
            row.save(update_fields=["error", "updated_at"])
            counts["skipped"] += 1
            continue
        mappings = data.get("mappings") or {}
        fields = {}
        headers = {}
        for header, spec in mappings.items():
            if not isinstance(spec, dict):
                continue
            field = spec.get("field")
            if (
                field in PRODUCT_FIELDS
                and header in row.raw
                and row.raw[header]
            ):
                fields[field] = row.raw[header]
                headers[header] = field
        if not fields:
            row.error = "mapping_propose returned no usable mappings."
            row.save(update_fields=["error", "updated_at"])
            counts["skipped"] += 1
            continue
        # Normalized once: downstream approval copies fields verbatim.
        row.proposed_mapping = {"fields": fields, "headers": headers}
        row.mapping_confidence = _row_confidence(data, mappings)
        row.mapping_state = StagedRow.MappingState.PROPOSED
        row.mapping_model = completion.resolved_model or completion.route
        row.error = ""
        row.save(
            update_fields=[
                "proposed_mapping", "mapping_confidence", "mapping_state",
                "mapping_model", "error", "updated_at",
            ]
        )
        counts["proposed"] += 1
    return counts


def _row_confidence(data: dict, mappings: dict) -> Decimal:
    if data.get("row_confidence") is not None:
        return _confidence(data["row_confidence"], "mapping_propose")
    specs = [s for s in mappings.values() if isinstance(s, dict)]
    if not specs:
        return Decimal("0.000")
    return _confidence(
        sum(float(s.get("confidence", 0)) for s in specs) / len(specs),
        "mapping_propose",
    )


def propose_dedupes(batch: ImportBatch) -> dict:
    """Group likely-duplicate staged rows; approval marks losers NA."""
    from collections import defaultdict

    rows = list(
        batch.rows.exclude(
            mapping_state__in=(
                StagedRow.MappingState.NA, StagedRow.MappingState.APPLIED
            )
        ).order_by("row_number")
    )
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        title = (
            row.mapped_values.get("title") or row.raw.get("title")
            or row.raw.get("name") or ""
        ).strip().lower()
        sku = (
            row.mapped_values.get("sku") or row.raw.get("sku") or ""
        ).strip().lower()
        if title or sku:
            groups[(sku, title)].append(row)
    candidates = [g for g in groups.values() if len(g) > 1]
    if not candidates:
        return {"proposed": 0, "groups": 0}
    _spend(batch)
    payload = [
        {
            "row": r.row_number,
            "sku": r.mapped_values.get("sku") or r.raw.get("sku"),
            "title": r.mapped_values.get("title") or r.raw.get("title"),
            "price": r.mapped_values.get("retail_price"),
        }
        for g in candidates
        for r in g
    ]
    try:
        completion = complete(
            "dedupe_propose",
            [{"role": "user", "content": json.dumps({"rows": payload})}],
            system=DEDUPE_SYSTEM,
            max_tokens=1000,
            temperature=0,
        )
        data = _parse_json(completion.text, "dedupe_propose")
    except (LLMHubError, ProposalError) as exc:
        return {"proposed": 0, "groups": len(candidates),
                "error": f"{type(exc).__name__}: {exc}"}
    by_number = {r.row_number: r for g in candidates for r in g}
    proposed = 0
    for dupe in data.get("duplicates") or []:
        winner = by_number.get(dupe.get("winner_row"))
        loser = by_number.get(dupe.get("loser_row"))
        if winner is None or loser is None or winner == loser:
            continue
        loser.proposed_mapping = {
            "__dedupe_of__": winner.content_hash,
            "__reason__": str(dupe.get("reason", ""))[:200],
        }
        loser.mapping_confidence = _confidence(
            dupe.get("confidence", 0), "dedupe_propose"
        )
        loser.mapping_state = StagedRow.MappingState.PROPOSED
        loser.mapping_model = completion.resolved_model or completion.route
        loser.save(
            update_fields=[
                "proposed_mapping", "mapping_confidence", "mapping_state",
                "mapping_model", "updated_at",
            ]
        )
        proposed += 1
    return {"proposed": proposed, "groups": len(candidates)}


RULE_FIELDS = {
    "receivable", "revenue", "tax", "discount", "shipping_income",
    "cash", "fee", "inventory", "payable",
}


def propose_coa_mapping(company, doc_type: str) -> dict:
    """Propose posting-rule account assignments from the company chart.

    Returns a validated ``{field: account_number}`` mapping — never applied
    here. Applying means creating the DocumentPostingRule through the
    normal admin/API path, which full-cleans it. Unknown or foreign
    account numbers are dropped, never guessed.
    """
    from accounting.models import Account

    chart = list(
        Account.objects.filter(company=company, is_active=True).values(
            "account_number", "name", "account_type"
        )
    )
    if not chart:
        raise ProposalError(
            f"No active chart of accounts at {company} — import or seed "
            "one before mapping."
        )
    _spend(_coa_budget_holder(company))
    try:
        completion = complete(
            "coa_map",
            [{"role": "user", "content": json.dumps(
                {"doc_type": doc_type, "chart": chart}
            )}],
            system=COA_SYSTEM,
            max_tokens=1000,
            temperature=0,
        )
        data = _parse_json(completion.text, "coa_map")
    except LLMHubError as exc:
        raise ProposalError(
            f"coa_map failed ({exc}) — nothing proposed."
        ) from exc
    numbers = {a["account_number"] for a in chart}
    mapping = {}
    for field, number in (data.get("mapping") or {}).items():
        if field in RULE_FIELDS and number in numbers:
            mapping[field] = number
    if not mapping:
        raise ProposalError(
            "coa_map returned no usable account assignments — nothing "
            "proposed."
        )
    return {
        "mapping": mapping,
        "confidence": str(_confidence(
            data.get("confidence", 0), "coa_map"
        )),
        "model": completion.resolved_model or completion.route,
    }


def _coa_budget_holder(company):
    """CoA mapping has no batch; budget against a transient holder.

    The holder is never saved — the spend call only enforces the default
    budget constant. Callers needing accounting should wrap in a batch.
    """
    holder = ImportBatch(
        company=company, source_kind="csv", source_name="coa-map",
        llm_call_budget=5,
    )
    return holder


AUTO_APPLY_THRESHOLD = Decimal("0.950")


def approve_high_confidence(
    batch: ImportBatch, actor: str, threshold: Decimal = AUTO_APPLY_THRESHOLD
) -> int:
    """Auto-apply band (DD-2): ≥threshold PROPOSED rows become APPROVED.

    Dedupe proposals are excluded — merging is never automatic.
    """
    if not actor:
        raise ValueError("Approval always names its decider — actor required.")
    if batch.entity_kind != "product":
        return 0  # other kinds approve through their own service (party merges: parties.merge_queue)
    approved = 0
    for row in batch.rows.filter(
        mapping_state=StagedRow.MappingState.PROPOSED,
        mapping_confidence__gte=threshold,
    ):
        proposal = row.proposed_mapping or {}
        if "__dedupe_of__" in proposal:
            continue
        fields = {
            f: v for f, v in (proposal.get("fields") or {}).items()
            if f in PRODUCT_FIELDS and v
        }
        if not fields:
            continue
        row.mapped_values = fields
        row.mapping_state = StagedRow.MappingState.APPROVED
        row.mapping_actor = actor
        row.save(
            update_fields=[
                "mapped_values", "mapping_state", "mapping_actor",
                "updated_at",
            ]
        )
        approved += 1
    return approved


def approve_rows(batch: ImportBatch, row_ids: list, actor: str) -> int:
    """Human bulk approval of PROPOSED rows (mapping kind only)."""
    if not actor:
        raise ValueError("Approval always names its decider — actor required.")
    if batch.entity_kind != "product":
        return 0  # other kinds approve through their own service (party merges: parties.merge_queue)
    wanted = {str(pk) for pk in row_ids}
    approved = 0
    for row in batch.rows.filter(
        mapping_state=StagedRow.MappingState.PROPOSED
    ):
        # An empty selection approves nothing — bulk approval is always an
        # explicit pick, never "everything not yet decided".
        if str(row.pk) not in wanted:
            continue
        proposal = row.proposed_mapping or {}
        if "__dedupe_of__" in proposal:
            winner_hash = proposal["__dedupe_of__"]
            row.dedupe_of_hash = winner_hash
            row.mapping_state = StagedRow.MappingState.NA
            row.mapping_actor = actor
            row.proposed_mapping = {}
            row.save(
                update_fields=[
                    "dedupe_of_hash", "mapping_state", "mapping_actor",
                    "proposed_mapping", "updated_at",
                ]
            )
            approved += 1
            continue
        mapped = {
            f: v for f, v in (proposal.get("fields") or {}).items()
            if f in PRODUCT_FIELDS and v
        }
        if not mapped:
            continue
        row.mapped_values = mapped
        row.mapping_state = StagedRow.MappingState.APPROVED
        row.mapping_actor = actor
        row.save(
            update_fields=[
                "mapped_values", "mapping_state", "mapping_actor",
                "updated_at",
            ]
        )
        approved += 1
    return approved
