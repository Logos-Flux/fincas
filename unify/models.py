"""Messy-source unification staging (Steps 3-4, T-1 interim shape).

Every customer mess lands here first as content-hashed rows, never straight
into domain tables. Mapping proposals ride as flags ON the staged rows
(``proposed_mapping``/``mapping_state``) — no standalone ledger models until
5+ real migrations prove the shape (T-1). Bulk application runs through
``ApprovalRun``/``ApprovalRow`` (ED-4): server-side, idempotent per row,
cancellable, with a single undo token per run.
"""

from __future__ import annotations

import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import models

from core.models import BaseModel


def content_hash(payload: dict) -> str:
    """Stable sha256 over canonical JSON — the dedupe key for raw rows."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ImportBatch(BaseModel):
    """One messy source file: CSV today, shop-platform syncs tomorrow."""

    class SourceKind(models.TextChoices):
        CSV = "csv", "CSV upload"
        SHOPIFY = "shopify", "Shopify"
        QBO = "qbo", "QuickBooks"
        DEDUPE = "dedupe", "Duplicate scan"
        CRM = "crm", "CRM import"

    class Status(models.TextChoices):
        STAGED = "staged", "Staged"
        REVIEWING = "reviewing", "In review"
        APPLYING = "applying", "Applying"
        APPLIED = "applied", "Applied"
        PARTIAL = "partial", "Partially applied"

    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, related_name="import_batches"
    )
    source_kind = models.CharField(max_length=20, choices=SourceKind.choices)
    source_name = models.CharField(max_length=255)
    entity_kind = models.CharField(
        max_length=20, default="product",
        help_text="Which unify.appliers applier applies the rows: product, party_merge.",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.STAGED
    )
    # Server-side LLM budget (AD-4): proposal calls stop when exhausted.
    llm_call_budget = models.PositiveIntegerField(default=25)
    llm_calls_used = models.PositiveIntegerField(default=0)

    objects = models.Manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.source_name} ({self.get_status_display()})"

    @property
    def completeness(self) -> dict:
        """Mapped share excluding N/A — the tri-state promise as arithmetic."""
        from django.db.models import Count

        counts = dict(
            self.rows.values("mapping_state").annotate(n=Count("id")).values_list(
                "mapping_state", "n"
            )
        )
        total = sum(counts.values())
        na = counts.get(StagedRow.MappingState.NA, 0)
        mapped = counts.get(StagedRow.MappingState.APPLIED, 0) + counts.get(
            StagedRow.MappingState.APPROVED, 0
        )
        denom = total - na
        return {
            "total": total,
            "na": na,
            "mapped": mapped,
            "percent": round(100 * mapped / denom, 1) if denom else 100.0,
        }


class StagedRow(BaseModel):
    """One raw row + its mapping flags. Raw is verbatim; mapped is derived."""

    class MappingState(models.TextChoices):
        UNMAPPED = "unmapped", "Unmapped"
        PROPOSED = "proposed", "Proposed"
        APPROVED = "approved", "Approved"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        NA = "na", "Not applicable"

    batch = models.ForeignKey(
        ImportBatch, on_delete=models.CASCADE, related_name="rows"
    )
    row_number = models.PositiveIntegerField()
    raw = models.JSONField()
    content_hash = models.CharField(max_length=64, db_index=True)
    # Mapping flags (T-1): the proposal lives here, audited per transition.
    mapping_state = models.CharField(
        max_length=20, choices=MappingState.choices,
        default=MappingState.UNMAPPED,
    )
    proposed_mapping = models.JSONField(default=dict, blank=True)
    mapping_confidence = models.DecimalField(
        max_digits=4, decimal_places=3, null=True, blank=True
    )
    mapping_actor = models.CharField(max_length=255, blank=True)
    mapping_model = models.CharField(max_length=255, blank=True)
    mapped_values = models.JSONField(default=dict, blank=True)
    dedupe_of_hash = models.CharField(max_length=64, blank=True)
    applied_ref = models.CharField(max_length=100, blank=True)
    error = models.TextField(blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["row_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"],
                name="uniq_staged_row_number_per_batch",
            ),
        ]

    def __str__(self):
        return f"{self.batch.source_name} row {self.row_number} ({self.mapping_state})"

    def clean(self):
        super().clean()
        if self.mapping_state == self.MappingState.APPROVED and not self.mapped_values:
            raise ValidationError(
                "Approved rows must carry mapped_values — approval without a "
                "mapping is a rubber stamp with no content."
            )


class ColumnMapTemplate(BaseModel):
    """Deterministic header→field baseline (AD-6) the LLM must beat."""

    source_kind = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    mapping = models.JSONField(
        help_text="Lowercased header → domain field path."
    )

    objects = models.Manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_kind", "name"],
                name="uniq_column_map_template",
            ),
        ]

    def __str__(self):
        return f"{self.source_kind}/{self.name}"


class ApprovalRun(BaseModel):
    """One bulk decision session over a batch (ED-4).

    ``actor`` is a username, or ``system:auto-apply(<run-id>, <threshold>)``
    for the auto band — never null, so every applied row names its decider.
    """

    class Kind(models.TextChoices):
        MAPPING = "mapping", "Mapping approval"
        APPLY = "apply", "Apply to domain"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        APPLYING = "applying", "Applying"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Canceled"

    batch = models.ForeignKey(
        ImportBatch, on_delete=models.CASCADE, related_name="approval_runs"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    actor = models.CharField(max_length=255)
    undo_token = models.CharField(max_length=64, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} run on {self.batch} by {self.actor}"


class ApprovalRow(BaseModel):
    """Per-row fate inside a run — idempotent, resumable, cancellable."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    run = models.ForeignKey(
        ApprovalRun, on_delete=models.CASCADE, related_name="rows"
    )
    staged_row = models.ForeignKey(
        StagedRow, on_delete=models.CASCADE, related_name="approval_rows"
    )
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.PENDING
    )
    error = models.TextField(blank=True)

    objects = models.Manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "staged_row"],
                name="uniq_approval_row_per_run",
            ),
        ]

    def __str__(self):
        return f"{self.run_id} / row {self.staged_row_id} ({self.state})"
