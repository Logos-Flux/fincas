"""CRM import jobs (docs/crm/phase5-importer.md 1.5 and 4.1; step 2).

Every model has ``company`` NOT NULL and ``CompanyScopedManager``. On
``ImportJobBatch`` and ``ImportWriteLog`` it is copied from the job on every
save (``CompanyFromParentMixin``); ``manage.py check_tenancy --app crm_import``
asserts the copies still agree, using each model's ``tenancy_parents``.

``unify`` holds no FK to this app: a job reaches its ``unify.ImportBatch``es
through ``ImportJobBatch``.
"""

from __future__ import annotations

from functools import reduce
from operator import or_

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from core.managers import CompanyScopedManager
from core.models import BaseModel
from parties.models import CompanyFromParentMixin

#: Applied object type -> its step in the apply order (spec 4.2). Order
#: matches ``canonical.schema.OBJECT_TYPES``; ``property_def`` is read, never
#: applied, so it has no batch and no step.
STEPS: dict[str, int] = {
    "owner": 1,
    "pipeline": 2,
    "company": 3,
    "contact": 4,
    "deal": 5,
    "association": 6,
    "relationship": 7,
    "task": 8,
    "communication": 9,
    "consent": 10,
}

#: ``unify.ImportBatch.entity_kind`` of an applied object type (spec 1.5).
ENTITY_KIND_PREFIX = "crm_"


def entity_kind_for(object_type: str) -> str:
    return f"{ENTITY_KIND_PREFIX}{object_type}"


class JobStatus(models.TextChoices):
    VALIDATING = "validating", "Validating"
    INVALID = "invalid", "Invalid"
    STAGING = "staging", "Staging"
    REVIEWING = "reviewing", "In review"
    APPLYING = "applying", "Applying"
    APPLIED = "applied", "Applied"
    PARTIALLY_APPLIED = "partially_applied", "Partially applied"
    UNDOING = "undoing", "Undoing"
    UNDONE = "undone", "Undone"
    CANCELLED = "cancelled", "Canceled"


class JobCoverage(models.TextChoices):
    COMPLETE = "complete", "Complete"
    PARTIAL = "partial", "Partial"


class WriteAction(models.TextChoices):
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    LINK = "link", "Link"


class ImportJob(BaseModel):
    """One import of one canonical export directory into one tenant (spec 1.5)."""

    Status = JobStatus
    Coverage = JobCoverage

    company = models.ForeignKey(
        "core.Company", on_delete=models.PROTECT, related_name="crm_import_jobs"
    )
    source = models.CharField(max_length=64, help_text="<provider>:<account id>")
    provider = models.CharField(max_length=32)
    adapter_version = models.CharField(max_length=32, blank=True)
    manifest = models.JSONField(default=dict, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    options = models.JSONField(default=dict, blank=True)
    # 20, not the spec's 16: "partially_applied" is 17 characters.
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.VALIDATING)
    # Fail-safe default: a job never claims complete coverage until
    # validation has derived it from the manifest.
    coverage = models.CharField(max_length=8, choices=Coverage.choices, default=Coverage.PARTIAL)
    previous_job = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="reimports"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    report = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    tenancy_parents = ("previous_job",)

    class Meta:
        ordering = ["-created_at"]
        permissions = [
            ("apply_import", "Can apply and undo CRM imports"),
            ("view_import_pii", "Can view unmasked values in CRM import reports"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=JobStatus.values), name="crmimportjob_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(coverage__in=JobCoverage.values),
                name="crmimportjob_coverage_valid",
            ),
            models.CheckConstraint(condition=~Q(source=""), name="crmimportjob_source_not_blank"),
            models.CheckConstraint(
                condition=~Q(provider=""), name="crmimportjob_provider_not_blank"
            ),
            models.CheckConstraint(
                condition=~Q(previous_job=F("id")), name="crmimportjob_previous_not_self"
            ),
        ]
        indexes = [
            # The source lock's busy check (crm_import.locks).
            models.Index(fields=["company", "source", "status"], name="crmimportjob_source_idx"),
        ]

    def __str__(self):
        return f"{self.source} ({self.get_status_display()})"

    def clean(self):
        super().clean()
        self._check_previous_job()

    def save(self, *args, **kwargs):
        self._check_previous_job()
        super().save(*args, **kwargs)

    def _check_previous_job(self):
        if self.previous_job_id is None:
            return
        previous = self.previous_job
        if previous.company_id != self.company_id:
            raise ValidationError({"previous_job": "The previous job belongs to another company."})
        if previous.source != self.source:
            raise ValidationError({"previous_job": "The previous job imported another source."})


class ImportJobBatch(CompanyFromParentMixin, BaseModel):
    """A job's ``unify.ImportBatch`` for one object type, with its step (spec 1.5, 4.2)."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name="job_batches")
    batch = models.OneToOneField(
        "unify.ImportBatch", on_delete=models.CASCADE, related_name="crm_import_job_batch"
    )
    object_type = models.CharField(max_length=20, choices=[(t, t) for t in STEPS])
    step = models.PositiveSmallIntegerField()

    objects = CompanyScopedManager()

    company_from = "job"
    tenancy_parents = ("job", "batch")

    class Meta:
        ordering = ["job_id", "step"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "object_type"], name="uniq_crmimportjobbatch_job_type"
            ),
            # The step is a function of the object type (spec 4.2).
            models.CheckConstraint(
                condition=reduce(or_, (Q(object_type=t, step=s) for t, s in STEPS.items())),
                name="crmimportjobbatch_step_matches_type",
            ),
        ]

    def __str__(self):
        return f"{self.job_id} step {self.step} ({self.object_type})"

    def save(self, *args, **kwargs):
        self.step = STEPS.get(self.object_type, self.step)
        if self.job_id is not None and self.batch_id is not None:
            batch, job = self.batch, self.job
            if batch.company_id != job.company_id:
                raise ValidationError({"batch": "The batch belongs to another company."})
            if batch.source_name != job.source:
                raise ValidationError({"batch": "The batch imports another source."})
            if batch.source_kind != "crm" or batch.entity_kind != entity_kind_for(self.object_type):
                raise ValidationError(
                    {"batch": f"The batch must be source_kind=crm, entity_kind="
                              f"{entity_kind_for(self.object_type)}."}
                )
        super().save(*args, **kwargs)


class ImportWriteLog(CompanyFromParentMixin, BaseModel):
    """One write an applier made, with what it takes to undo it (spec 1.5, 4.5).

    Append-only: after the insert only ``undone_at`` / ``undone_by`` may
    change (``save`` refuses anything else). Erasure is the one exception: it
    may scrub ``before`` to ``{}`` with ``QuerySet.update()``, and undo treats
    a scrubbed ``update`` as "refuse". ``seq`` orders every write of the job; undo walks
    it backwards. ``before`` is the full target row as it was before an
    ``update`` (``{attname: value}``, every concrete column, encoded the way
    ``parties.merge`` encodes ``PartyMergeLog.before``); it is null for
    ``create`` and ``link``. ``after_digest`` fingerprints the row just after
    the write, so undo can tell whether anything changed it since.
    """

    Action = WriteAction

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name="write_log")
    approval_row = models.ForeignKey(
        "unify.ApprovalRow", on_delete=models.CASCADE, related_name="+"
    )
    seq = models.PositiveBigIntegerField()
    content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.PROTECT, related_name="+"
    )
    # A string so any target pk (UUID or integer) fits; save() stores the
    # pk's canonical text (str(uuid), never uuid.hex) so undo's lookup cannot miss.
    object_id = models.CharField(max_length=64)
    target = GenericForeignKey("content_type", "object_id")
    action = models.CharField(max_length=8, choices=Action.choices)
    before = models.JSONField(null=True, blank=True)
    after_digest = models.CharField(max_length=64)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.CharField(max_length=255, blank=True)

    objects = CompanyScopedManager()

    company_from = "job"
    # The run's batch too: bulk_create and update() skip save()'s check.
    tenancy_parents = ("job", "approval_row__run__batch")

    #: The only columns an existing row may change.
    MUTABLE_FIELDS = frozenset({"undone_at", "undone_by", "updated_at"})

    class Meta:
        ordering = ["job_id", "seq"]
        constraints = [
            # Also the spec's (job, seq) index.
            models.UniqueConstraint(fields=["job", "seq"], name="uniq_crmimportwritelog_job_seq"),
            models.CheckConstraint(condition=Q(seq__gte=1), name="crmimportwritelog_seq_positive"),
            models.CheckConstraint(
                condition=Q(action__in=WriteAction.values),
                name="crmimportwritelog_action_valid",
            ),
            models.CheckConstraint(
                condition=Q(action="update", before__isnull=False)
                | (~Q(action="update") & Q(before__isnull=True)),
                name="crmimportwritelog_before_iff_update",
            ),
            models.CheckConstraint(
                condition=~Q(after_digest=""), name="crmimportwritelog_digest_not_blank"
            ),
            models.CheckConstraint(
                condition=~Q(object_id=""), name="crmimportwritelog_target_not_blank"
            ),
        ]
        indexes = [
            # "Which import writes touched this row?" (undo's refusal check).
            models.Index(fields=["content_type", "object_id"], name="crmimportwritelog_target_idx"),
        ]

    def __str__(self):
        return f"{self.job_id} #{self.seq} {self.action} {self.content_type_id}:{self.object_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or not set(update_fields) <= self.MUTABLE_FIELDS:
                raise ValueError(
                    "ImportWriteLog is append-only: only undone_at and undone_by may "
                    "change (save(update_fields=['undone_at', 'undone_by']))."
                )
            kwargs["update_fields"] = {*update_fields, "updated_at"}
            return super().save(*args, **kwargs)
        self._check_target()
        if not ImportJobBatch.objects.filter(
            job_id=self.job_id, batch__approval_runs__rows=self.approval_row_id
        ).exists():
            # Tenant isolation: the run that made the write belongs to one of
            # this job's batches, hence to the job's company.
            raise ValidationError(
                {"approval_row": "The approval row belongs to a batch outside this job."}
            )
        super().save(*args, **kwargs)

    def _check_target(self):
        """Canonicalise ``object_id`` and refuse a target in another tenant."""
        model = self.content_type.model_class()
        if model is None:
            raise ValidationError({"content_type": "The target model is not installed."})
        try:
            self.object_id = str(model._meta.pk.to_python(self.object_id))
        except ValidationError as exc:
            raise ValidationError({"object_id": exc.messages}) from exc
        if any(f.attname == "company_id" for f in model._meta.concrete_fields):
            target_company = (
                model._base_manager.filter(pk=self.object_id)
                .values_list("company_id", flat=True)
                .first()
            )
            if target_company is not None and target_company != self.job.company_id:
                raise ValidationError({"object_id": "The target belongs to another company."})


class ImportSourceLock(BaseModel):
    """The row ``crm_import.locks`` locks per (company, source) (spec 7)."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=64)

    objects = CompanyScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "source"], name="uniq_crmimportsourcelock_company_source"
            ),
        ]

    def __str__(self):
        return f"lock {self.source}"
