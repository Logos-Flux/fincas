"""CSV ingest: messy file → content-hashed staged rows (Step 3).

Duplicates (same content hash, in-batch or previously applied for the same
company) are marked NA with ``dedupe_of_hash`` — represented already, so
excluded from completeness and skipped by apply. Deterministic header
templates (AD-6) map known columns at confidence 1.0 under actor
``system:template``; unknown headers stay UNMAPPED for the LLM step.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import ColumnMapTemplate, ImportBatch, StagedRow, content_hash

MAX_ROWS = 10_000


def ensure_default_templates() -> ColumnMapTemplate:
    """The generic product-CSV baseline the LLM must beat (AD-6)."""
    template, _ = ColumnMapTemplate.objects.get_or_create(
        source_kind=ImportBatch.SourceKind.CSV,
        name="generic-product-csv",
        defaults={
            "mapping": {
                "sku": "sku",
                "title": "title",
                "name": "title",
                "part_number": "part_number",
                "partnumber": "part_number",
                "retail_price": "retail_price",
                "price": "retail_price",
                "msrp": "msrp_price",
                "description": "description",
            }
        },
    )
    return template


def ingest_csv(company, source_name: str, file_bytes: bytes) -> ImportBatch:
    """Parse, hash, dedupe, stage. Raises ValueError on malformed input."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "Could not decode CSV as UTF-8. Save the export as UTF-8 "
            "and re-upload."
        ) from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row — nothing to map.")
    headers = [h for h in reader.fieldnames if h]
    if not headers:
        raise ValueError("CSV headers are all blank — nothing to map.")

    with transaction.atomic():
        batch = ImportBatch.objects.create(
            company=company,
            source_kind=ImportBatch.SourceKind.CSV,
            source_name=source_name,
        )
        seen: dict[str, int] = {}
        applied_hashes = set(
            StagedRow.objects.filter(
                batch__company=company,
                mapping_state=StagedRow.MappingState.APPLIED,
            ).values_list("content_hash", flat=True)
        )
        count = 0
        for number, raw in enumerate(reader, start=1):
            count = number
            if number > MAX_ROWS:
                raise ValueError(
                    f"CSV exceeds the {MAX_ROWS:,}-row slice cap. Split the "
                    "export and upload in parts — each part gets its own "
                    "reviewable batch."
                )
            row = {h: (raw.get(h) or "").strip() for h in headers}
            digest = content_hash(row)
            first = seen.get(digest)
            if first is None and digest not in applied_hashes:
                seen[digest] = number
                StagedRow.objects.create(
                    batch=batch, row_number=number, raw=row,
                    content_hash=digest,
                )
            else:
                StagedRow.objects.create(
                    batch=batch, row_number=number, raw=row,
                    content_hash=digest,
                    mapping_state=StagedRow.MappingState.NA,
                    dedupe_of_hash=digest,
                    mapping_actor="system:dedupe",
                )
        if count == 0:
            raise ValueError("CSV has headers but no data rows.")
    return batch


def apply_template(batch: ImportBatch) -> dict:
    """Deterministic header mapping over UNMAPPED rows. Returns counts."""
    template = ensure_default_templates()
    mapping = {k.lower(): v for k, v in template.mapping.items()}
    counts = {"mapped": 0, "unmapped": 0}
    for row in batch.rows.filter(
        mapping_state=StagedRow.MappingState.UNMAPPED
    ):
        mapped = {}
        for header, value in row.raw.items():
            field = mapping.get(header.lower())
            if field and value:
                mapped[field] = _coerce(field, value)
        if mapped:
            row.mapped_values = mapped
            row.mapping_state = StagedRow.MappingState.APPROVED
            row.mapping_confidence = Decimal("1.000")
            row.mapping_actor = "system:template"
            row.save(
                update_fields=[
                    "mapped_values", "mapping_state",
                    "mapping_confidence", "mapping_actor", "updated_at",
                ]
            )
            counts["mapped"] += 1
        else:
            counts["unmapped"] += 1
    return counts


def _coerce(field: str, value: str):
    if field in ("retail_price", "msrp_price"):
        try:
            return str(Decimal(value.replace(",", "").replace("$", "")))
        except InvalidOperation:
            return value
    return value


def dry_run(batch: ImportBatch) -> dict:
    """What apply would do — the report the approver sees first."""
    from django.db.models import Count

    by_state = dict(
        batch.rows.values("mapping_state").annotate(n=Count("id")).values_list(
            "mapping_state", "n"
        )
    )
    return {
        "batch": str(batch.pk),
        "source": batch.source_name,
        "by_state": by_state,
        "completeness": batch.completeness,
        "would_create": by_state.get(StagedRow.MappingState.APPROVED, 0),
        "needs_review": by_state.get(StagedRow.MappingState.UNMAPPED, 0)
        + by_state.get(StagedRow.MappingState.PROPOSED, 0),
    }
