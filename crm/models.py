import threading
from contextlib import contextmanager

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from core.managers import CompanyScopedManager
from core.models import BaseModel
from core.teams import OwnerTeamStamped
from core.tz import validate_timezone


class Lead(BaseModel):
    """Sales lead (45 fields)."""

    class Status(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        QUALIFIED = "qualified", "Qualified"
        PROPOSAL_SENT = "proposal_sent", "Proposal Sent"
        CONVERTED = "converted", "Converted"
        CLOSED_LOST = "closed_lost", "Closed/Lost"
        CLOSED_JUNK = "closed_junk", "Closed/Junk"

    class Source(models.TextChoices):
        WEBSITE_FORM = "website_form", "Website Form"
        PHONE = "phone", "Phone"
        EMAIL = "email", "Email"
        TRADE_SHOW = "trade_show", "Trade Show"
        REFERRAL = "referral", "Referral"
        MARKETPLACE = "marketplace", "Marketplace"
        SOCIAL = "social", "Social Media"
        AI_CHAT = "ai_chat", "AI Chat (Claude, Gemini, Muse)"
        OTHER = "other", "Other"

    class RequestType(models.TextChoices):
        QUOTE = "quote", "Quote Request"
        INFO = "info", "Information"
        SUPPORT = "support", "Support"
        PARTNERSHIP = "partnership", "Partnership"
        OTHER = "other", "Other"

    # Identification
    lead_number = models.CharField(max_length=20, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    status_note = models.TextField(blank=True)

    # Contact
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=50, blank=True)
    company_name = models.CharField(max_length=255, blank=True)
    job_title = models.CharField(max_length=100, blank=True)

    # Location
    city = models.CharField(max_length=100, blank=True)
    state_province = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, default="US")

    # Request
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.WEBSITE_FORM)
    source_detail = models.CharField(max_length=255, blank=True)
    request_type = models.CharField(max_length=20, choices=RequestType.choices, blank=True)
    message = models.TextField(blank=True)
    products_interested = models.JSONField(default=list, blank=True)
    estimated_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    project_type = models.CharField(max_length=100, blank=True)
    project_timeline = models.CharField(max_length=100, blank=True)

    # Assignment
    assigned_to = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_leads",
    )
    follow_up_date = models.DateField(null=True, blank=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    contact_count = models.IntegerField(default=0)

    # Conversion
    converted_to_customer = models.ForeignKey(
        "customers.Customer", on_delete=models.SET_NULL, null=True, blank=True,
    )
    converted_to_quote = models.ForeignKey(
        "orders.Quote", on_delete=models.SET_NULL, null=True, blank=True,
    )
    converted_at = models.DateTimeField(null=True, blank=True)
    converted_by = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="converted_leads",
    )

    # Loss
    loss_reason = models.CharField(max_length=255, blank=True)
    loss_notes = models.TextField(blank=True)
    lost_at = models.DateTimeField(null=True, blank=True)

    # Attribution
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    utm_source = models.CharField(max_length=255, blank=True)
    utm_medium = models.CharField(max_length=255, blank=True)
    utm_campaign = models.CharField(max_length=255, blank=True)
    referrer_url = models.URLField(blank=True)

    # Internal
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)
    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, null=True, blank=True,
    )
    # The lead's person party (phase2-party.md 2.5); set by ``parties.sync``
    # on match-0 / match-1, left NULL on match-N (the merge queue resolves it).
    party = models.ForeignKey(
        "parties.Party", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="leads", editable=False,
    )
    # The engagement the lead was triaged onto (spec 1.7, E6): set by
    # ``crm.intake`` on create (or the step-6 backfill), NULL for support
    # requests. Once set, the workflow fields below are frozen.
    engagement = models.ForeignKey(
        "crm.Engagement", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="leads", editable=False,
    )

    objects = CompanyScopedManager()

    tenancy_parents = ("engagement",)

    #: Workflow fields refused on API writes once ``engagement`` is set
    #: (``lead_triaged``); the work happens on the engagement. Phase 4 drops
    #: the columns.
    FROZEN_WHEN_TRIAGED = (
        "status", "status_note", "assigned_to", "follow_up_date", "last_contacted_at",
        "contact_count", "converted_to_customer", "converted_to_quote", "converted_at",
        "converted_by", "loss_reason", "loss_notes", "lost_at",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.lead_number} — {self.first_name} {self.last_name}"

    def compat_status(self):
        """``status`` for one release once triaged (spec 1.7): derived from
        the engagement, else the stored column."""
        from .intake import lead_status_for

        if self.engagement_id is None:
            return self.status
        return lead_status_for(self.engagement)

    NUMBER_PREFIX = "LD-"
    NUMBER_WIDTH = 5
    #: Collisions on an auto-minted number are retried this many times.
    NUMBER_TRIES = 5

    @classmethod
    def next_lead_number(cls) -> str:
        """The next ``LD-<n>`` number: one past the highest ``LD-`` number stored.

        Only ``LD-`` numbers count. A lead that came in with its own number
        (the API, an import: ``L-R8-…``, ``HS-123``) is not the sequence, so
        it never decides the next value: until 2026-10-17 the last *created*
        lead did, a foreign number parsed to nothing, the fallback was
        ``LD-00001``, and the staff lead form answered 500 with an
        IntegrityError. ``Lead.save`` retries on a collision (two forms at
        once); a caller-supplied number is never rewritten.
        """
        from django.db.models.functions import Length

        prefix = cls.NUMBER_PREFIX
        highest = (
            cls._base_manager.filter(lead_number__regex=rf"^{prefix}[0-9]+$")
            .order_by(Length("lead_number").desc(), "-lead_number")
            .values_list("lead_number", flat=True)
            .first()
        )
        num = int(highest[len(prefix):]) + 1 if highest else 1
        return f"{prefix}{num:0{cls.NUMBER_WIDTH}d}"

    def save(self, *args, **kwargs):
        from django.db import IntegrityError

        from parties.sync import LEAD_SYNC_FIELDS, run_guarded, sync_from_lead, wants_sync

        minted = not self.lead_number
        adding = self._state.adding
        for attempt in range(self.NUMBER_TRIES):
            if minted:
                self.lead_number = self.next_lead_number()
            try:
                with transaction.atomic(using=kwargs.get("using")):
                    super().save(*args, **kwargs)
                    if wants_sync(kwargs.get("update_fields"), LEAD_SYNC_FIELDS):
                        run_guarded(sync_from_lead, self)
                    if adding:
                        # After the party sync, so the engagement gets the lead's party.
                        from .intake import run_intake

                        run_intake(self)
                return
            except IntegrityError as exc:
                # A minted number taken between the read and the INSERT: mint again.
                if minted and adding and "lead_number" in str(exc) and attempt < self.NUMBER_TRIES - 1:
                    self._state.adding = True
                    continue
                raise


class ActivityLogFrozen(RuntimeError):
    """``ActivityLog`` is read-only since CRM Phase 3 step 6."""


class ActivityLog(BaseModel):
    """CRM activity record (calls, emails, meetings). **Frozen** (Phase 3 step 6).

    Every row was copied, with the same id, to a ``communications.Communication``
    (call / email / meeting / note) or a ``crm.Task`` (task, done), and
    ``/api/v1/crm/activities/`` serves those (``crm.activity_compat``).
    ``save()`` raises; the table is dropped in step 12. Erasure and cascades
    still reach it through ``QuerySet.update`` / delete.
    """

    class ActivityType(models.TextChoices):
        CALL = "call", "Phone Call"
        EMAIL = "email", "Email"
        MEETING = "meeting", "Meeting"
        NOTE = "note", "Note"
        TASK = "task", "Task"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, null=True, blank=True, related_name="activities")
    customer = models.ForeignKey("customers.Customer", on_delete=models.CASCADE, null=True, blank=True, related_name="activities")
    activity_type = models.CharField(max_length=10, choices=ActivityType.choices)
    subject = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    performed_by = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True)
    activity_date = models.DateTimeField()
    duration_minutes = models.IntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    # NOT NULL since CRM Phase 2 step 11 (D13). ``save()`` derives it from
    # lead, customer, performer, then the only Company (as crm/backfill.py did).
    company = models.ForeignKey(
        "core.Company", on_delete=models.PROTECT, blank=True,
    )

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["-activity_date"]
        verbose_name_plural = "activity logs"

    def __str__(self):
        return f"{self.activity_type} — {self.subject}"

    def save(self, *args, **kwargs):
        raise ActivityLogFrozen(
            "crm.ActivityLog is frozen (CRM Phase 3 step 6): log a communication with "
            "communications.ingest.record, or a task with crm.tasks, or use the compat "
            "endpoint /api/v1/crm/activities/."
        )


# --- CRM Phase 3: engagement (docs/crm/phase3-engagement.md section 1) -------
#
# Every model below carries ``company`` NOT NULL and uses
# CompanyScopedManager. Child tables copy ``company`` from their parent in
# ``save()`` (the Phase 2 rule) and list the parents in ``tenancy_parents``,
# which ``manage.py check_tenancy`` reads.


# The helpers below are abstract models, not plain mixins: migrations
# flatten abstract bases away, so no migration names them in ``bases`` and
# they can be renamed freely.


class _CompanyFromParent(models.Model):
    """Copy ``company`` from ``company_from`` on every save (Phase 2 rule)."""

    company_from = ""

    class Meta:
        abstract = True

    def _copy_company(self, kwargs):
        if getattr(self, f"{self.company_from}_id", None) is not None:
            self.company_id = getattr(self, self.company_from).company_id
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "company"}


class AppendOnlyError(RuntimeError):
    """An append-only history row was updated or deleted."""


class _AppendOnly(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError(f"{type(self).__name__} rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{type(self).__name__} rows are append-only.")


# --- 1.2 Pipeline and Stage ---------------------------------------------------


class _ImmutableKey(models.Model):
    """``key`` is persisted by imports and reports: refuse a change in place."""

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_key = getattr(instance, "key", None)
        return instance

    def _check_key_unchanged(self):
        loaded = getattr(self, "_loaded_key", None)
        if not self._state.adding and loaded is not None and self.key != loaded:
            raise ValidationError({"key": f"{type(self).__name__}.key is immutable ({loaded!r})."})


class Pipeline(_ImmutableKey, BaseModel):
    """A tenant's deal pipeline (spec 1.2). Stages are rows, never an enum."""

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="pipelines")
    key = models.SlugField(max_length=32)
    name = models.CharField(max_length=100)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    stale_after_days = models.PositiveSmallIntegerField(default=14)
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "key"], name="uniq_pipeline_co_key"),
            models.UniqueConstraint(
                fields=["company"], condition=Q(is_default=True), name="uniq_pipeline_one_default",
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self._check_key_unchanged()
        super().save(*args, **kwargs)
        self._loaded_key = self.key

    def first_stage(self):
        return self.stages.filter(is_active=True).order_by("sort_order", "key").first()


class Stage(_ImmutableKey, _CompanyFromParent, BaseModel):
    """An ordered stage of one pipeline. ``outcome_hint`` only proposes closing."""

    class OutcomeHint(models.TextChoices):
        NONE = "", "None"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT, related_name="stages")
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    key = models.SlugField(max_length=32)
    name = models.CharField(max_length=100)
    sort_order = models.PositiveSmallIntegerField()
    default_probability = models.PositiveSmallIntegerField(null=True, blank=True)
    outcome_hint = models.CharField(max_length=4, blank=True, choices=OutcomeHint.choices)
    stale_after_days = models.PositiveSmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = CompanyScopedManager()

    company_from = "pipeline"
    tenancy_parents = ("pipeline",)

    class Meta:
        ordering = ["pipeline", "sort_order"]
        constraints = [
            models.UniqueConstraint(fields=["pipeline", "key"], name="uniq_stage_pipeline_key"),
            models.CheckConstraint(
                condition=Q(default_probability__isnull=True) | Q(default_probability__lte=100),
                name="stage_probability_range",
            ),
        ]
        indexes = [models.Index(fields=["pipeline", "sort_order"], name="stage_pipeline_order_idx")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self._check_key_unchanged()
        self._copy_company(kwargs)
        super().save(*args, **kwargs)
        self._loaded_key = self.key

    @property
    def effective_stale_after_days(self):
        if self.stale_after_days is not None:
            return self.stale_after_days
        return self.pipeline.stale_after_days


# --- 1.3 Engagement -----------------------------------------------------------


class EngagementWriteRefused(RuntimeError):
    """``stage``, ``status`` or ``pipeline`` changed outside ``crm.engagements``."""


_engagement_service = threading.local()


@contextmanager
def engagement_service_writes():
    """Let ``Engagement.save()`` accept stage / status / pipeline changes.

    Only ``crm.engagements`` enters this: every move writes its history row.
    """
    depth = getattr(_engagement_service, "depth", 0)
    _engagement_service.depth = depth + 1
    try:
        yield
    finally:
        _engagement_service.depth = depth


def _service_writes_allowed():
    return getattr(_engagement_service, "depth", 0) > 0


_GUARDED_ENGAGEMENT_FIELDS = ("stage_id", "status", "pipeline_id")


class Engagement(OwnerTeamStamped, BaseModel):
    """A sales thread with one counterparty (spec 1.3).

    ``status`` is the outcome and is independent of ``stage`` (E3): closing
    leaves the stage where the deal was. Moves go through
    ``crm.engagements``; ``save()`` refuses a changed ``stage``, ``status`` or
    ``pipeline`` otherwise. ``next_task`` / ``next_action_due_at`` are
    denormalised from the earliest open Task (E4) by
    ``crm.tasks.recompute_next_action``. ``owner_team`` is stamped from the
    owner's team when the owner changes (``core.teams``, phase4-fields.md 2.1).
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    class Origin(models.TextChoices):
        ERP = "erp", "ERP"
        API = "api", "API"
        LEAD = "lead", "Lead"
        QUOTE = "quote", "Quote"
        IMPORT = "import", "Import"
        BACKFILL = "backfill", "Backfill"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="engagements")
    number = models.CharField(max_length=20, blank=True)
    title = models.CharField(max_length=255, blank=True)
    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT, related_name="engagements")
    stage = models.ForeignKey(
        Stage, on_delete=models.PROTECT, null=True, blank=True, related_name="engagements",
    )
    stage_entered_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=4, choices=Status.choices, default=Status.OPEN)
    closed_at = models.DateTimeField(null=True, blank=True)
    loss_reason = models.CharField(max_length=32, blank=True)
    loss_competitor = models.CharField(max_length=255, blank=True)
    loss_notes = models.TextField(blank=True)
    owner = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_engagements",
    )
    owner_team = models.ForeignKey(
        "core.Team", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    party = models.ForeignKey(
        "parties.Party", on_delete=models.PROTECT, null=True, blank=True,
        related_name="engagements",
    )
    expected_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    probability = models.PositiveSmallIntegerField(null=True, blank=True)
    expected_close_date = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=32, blank=True)
    first_touch = models.ForeignKey(
        "Touchpoint", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    last_touch = models.ForeignKey(
        "Touchpoint", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    next_task = models.ForeignKey(
        "Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    next_action_due_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    origin = models.CharField(max_length=12, choices=Origin.choices, default=Origin.ERP)
    metadata = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)

    objects = CompanyScopedManager()

    tenancy_parents = ("pipeline", "stage", "party", "next_task", "owner_team")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="uniq_engagement_number"),
            models.CheckConstraint(
                condition=Q(status="open", closed_at__isnull=True)
                | (~Q(status="open") & Q(closed_at__isnull=False)),
                name="engagement_closed_at_iff_closed",
            ),
            models.CheckConstraint(
                condition=~Q(status="open") | Q(stage__isnull=False),
                name="engagement_open_has_stage",
            ),
            models.CheckConstraint(
                condition=Q(status="lost") | Q(loss_reason=""),
                name="engagement_loss_reason_only_when_lost",
            ),
            models.CheckConstraint(
                condition=~Q(status="lost") | ~Q(loss_reason=""),
                name="engagement_lost_has_reason",
            ),
            models.CheckConstraint(
                condition=Q(probability__isnull=True) | Q(probability__lte=100),
                name="engagement_probability_range",
            ),
            models.CheckConstraint(
                condition=Q(expected_value__isnull=True) | Q(expected_value__gte=0),
                name="engagement_value_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status", "owner", "next_action_due_at"],
                         name="eng_co_status_owner_due_idx"),
            models.Index(fields=["company", "pipeline", "stage"], condition=Q(status="open"),
                         name="eng_co_open_stage_idx"),
            models.Index(fields=["company", "party"], name="eng_co_party_idx"),
            models.Index(fields=["company", "status", "closed_at"], name="eng_co_closed_idx"),
            models.Index(fields=["company", "status", "last_activity_at"], name="eng_co_activity_idx"),
            models.Index(fields=["company", "source", "created_at"], name="eng_co_source_created_idx"),
            models.Index(fields=["company", "status", "owner_team"], name="eng_co_status_team_idx"),
        ]
        permissions = [
            ("manage_pipelines", "Can create and edit pipelines and stages"),
            ("reopen_engagement", "Can reopen a won or lost engagement"),
            ("reassign_engagement", "Can change another user's engagement owner"),
        ]

    def __str__(self):
        return f"{self.number} — {self.title}" if self.number else self.title

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._snapshot_guarded()
        return instance

    def _snapshot_guarded(self, fields=None):
        loaded = getattr(self, "_loaded", {})
        for attname in _GUARDED_ENGAGEMENT_FIELDS:
            if fields is None or attname in fields or attname.removesuffix("_id") in fields:
                if attname in self.__dict__:
                    loaded[attname] = getattr(self, attname)
        self._loaded = loaded

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        self._snapshot_guarded(fields)

    def guarded_changes(self):
        """Names of stage / status / pipeline columns changed since load."""
        if self._state.adding:
            return []
        loaded = getattr(self, "_loaded", {})
        # A guarded column absent from the snapshot (deferred by .only())
        # but assigned since counts as changed.
        return [
            attname for attname in _GUARDED_ENGAGEMENT_FIELDS
            if attname in self.__dict__
            and (attname not in loaded or getattr(self, attname) != loaded[attname])
        ]

    @property
    def weighted_value(self):
        """Computed, never stored (spec 1.3): value x probability / 100."""
        if self.expected_value is None:
            return None
        probability = self.effective_probability
        if probability is None:
            return None
        return (self.expected_value * probability / 100).quantize(self.expected_value)

    @property
    def effective_probability(self):
        if self.probability is not None:
            return self.probability
        return self.stage.default_probability if self.stage_id else None

    def default_title(self):
        from .registries import ENGAGEMENT_SOURCES

        who = self.party.display_name if self.party_id else "No counterparty"
        source = ENGAGEMENT_SOURCES.get(self.source)
        return f"{who} — {source.label if source else 'New'}"[:255]

    def clean(self):
        from .registries import ENGAGEMENT_SOURCES, LOSS_REASONS

        errors = {}
        if self.stage_id and self.pipeline_id and self.stage.pipeline_id != self.pipeline_id:
            errors["stage"] = "The stage belongs to another pipeline."
        if self.source and self.source not in ENGAGEMENT_SOURCES:
            errors["source"] = f"Unknown source {self.source!r} (GOLDEN_ENGAGEMENT_SOURCES)."
        if self.loss_reason and self.loss_reason not in LOSS_REASONS:
            errors["loss_reason"] = f"Unknown loss reason {self.loss_reason!r} (GOLDEN_LOSS_REASONS)."
        for name in ("pipeline", "stage", "party", "owner", "owner_team"):
            related = getattr(self, name) if getattr(self, f"{name}_id") else None
            if related is not None and self.company_id and related.company_id != self.company_id:
                errors[name] = f"The {name.replace('_', ' ')} belongs to another company."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        changed = self.guarded_changes()
        if changed and not _service_writes_allowed():
            update_fields = kwargs.get("update_fields")
            touched = changed if update_fields is None else [
                c for c in changed if c in update_fields or c.removesuffix("_id") in update_fields
            ]
            if touched:
                raise EngagementWriteRefused(
                    f"Engagement {', '.join(c.removesuffix('_id') for c in touched)} changes go "
                    "through crm.engagements (move / win / lose / reopen / change_pipeline), "
                    "which writes the history row."
                )
        if not self.number:
            from core.models import DocumentSequence

            self.number = DocumentSequence.get_next_number(self.company, Engagement, "number", "EN")
        if not self.title:
            self.title = self.default_title()
        if self._state.adding and self.stage_id and self.stage_entered_at is None:
            self.stage_entered_at = timezone.now()
        adding = self._state.adding
        self._stamp_owner_team(kwargs)
        super().save(*args, **kwargs)
        # Re-snapshot only what was written: an unwritten in-memory change
        # must still be refused by the next save().
        self._snapshot_guarded(kwargs.get("update_fields"))
        self._after_owner_save(kwargs)
        if adding:
            from .lifecycle import engagement_created

            engagement_created(self)  # spec 5.1, after commit


class EngagementContact(_CompanyFromParent, BaseModel):
    """A person on the deal, with their role (spec 1.3)."""

    class Role(models.TextChoices):
        DECISION_MAKER = "decision_maker", "Decision maker"
        CHAMPION = "champion", "Champion"
        INFLUENCER = "influencer", "Influencer"
        BILLING = "billing", "Billing"
        TECHNICAL = "technical", "Technical"
        OTHER = "other", "Other"

    engagement = models.ForeignKey(Engagement, on_delete=models.CASCADE, related_name="contacts")
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    party = models.ForeignKey(
        "parties.Party", on_delete=models.PROTECT, related_name="engagement_contacts",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.OTHER)
    is_primary = models.BooleanField(default=False)

    objects = CompanyScopedManager()

    company_from = "engagement"
    tenancy_parents = ("engagement", "party")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["engagement", "party"], name="uniq_engcontact_party"),
            models.UniqueConstraint(
                fields=["engagement"], condition=Q(is_primary=True),
                name="uniq_engcontact_one_primary",
            ),
        ]

    def __str__(self):
        return f"{self.party} ({self.get_role_display()})"

    def clean(self):
        from parties.constants import PERSON

        errors = {}
        if self.party_id and self.party.kind != PERSON:
            errors["party"] = "An engagement contact is a person."
        elif self.party_id and self.engagement_id and self.party_id == self.engagement.party_id:
            errors["party"] = "The engagement's own party is implicitly a contact."
        if self.party_id and self.engagement_id and self.party.company_id != self.engagement.company_id:
            errors["party"] = "The contact belongs to another company."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._copy_company(kwargs)
        super().save(*args, **kwargs)


class EngagementStageChange(_AppendOnly, _CompanyFromParent, BaseModel):
    """Append-only engagement history (spec 1.4, E12), written by the move service."""

    class Source(models.TextChoices):
        ERP = "erp", "ERP"
        API = "api", "API"
        ASSIST = "assist", "Assistant"
        IMPORT = "import", "Import"
        BACKFILL = "backfill", "Backfill"
        SYSTEM = "system", "System"

    engagement = models.ForeignKey(Engagement, on_delete=models.CASCADE, related_name="stage_changes")
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    from_pipeline = models.ForeignKey(
        Pipeline, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    to_pipeline = models.ForeignKey(
        Pipeline, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    from_stage = models.ForeignKey(Stage, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    to_stage = models.ForeignKey(Stage, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    # Blank on the creation row: the engagement had no status before.
    from_status = models.CharField(max_length=4, blank=True, choices=Engagement.Status.choices)
    to_status = models.CharField(max_length=4, choices=Engagement.Status.choices)
    changed_at = models.DateTimeField(default=timezone.now)
    changed_by = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    note = models.CharField(max_length=255, blank=True)

    #: Written by a backfill or an import: the time and stage are
    #: reconstructed, so the timeline marks the row and the stage report
    #: never times a visit from it (reporting.crm_reports).
    RECONSTRUCTED_SOURCES = frozenset({"backfill", "import"})

    @property
    def is_reconstructed(self) -> bool:
        return self.source in self.RECONSTRUCTED_SOURCES

    objects = CompanyScopedManager()

    company_from = "engagement"
    tenancy_parents = ("engagement", "from_stage", "to_stage")

    class Meta:
        ordering = ["changed_at"]
        indexes = [
            models.Index(fields=["engagement", "changed_at"], name="engstage_eng_at_idx"),
            models.Index(fields=["company", "to_stage", "changed_at"], name="engstage_co_to_stage_idx"),
        ]

    def __str__(self):
        return f"{self.engagement_id}: {self.from_stage_id}->{self.to_stage_id} {self.from_status}->{self.to_status}"

    def save(self, *args, **kwargs):
        self._copy_company(kwargs)
        super().save(*args, **kwargs)  # append-only: always an insert
        from .lifecycle import stage_change_written

        # engagement_won / _lost / _reopened / _stage_changed (spec 5.1).
        stage_change_written(self)


# --- 1.8 Touchpoint -------------------------------------------------------------


class Touchpoint(BaseModel):
    """A passive marketing event (a visit, an ad click); never a Communication."""

    class ClickIdKind(models.TextChoices):
        GCLID = "gclid", "gclid"
        GBRAID = "gbraid", "gbraid"
        WBRAID = "wbraid", "wbraid"
        FBCLID = "fbclid", "fbclid"
        MSCLKID = "msclkid", "msclkid"
        OTHER = "other", "Other"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    party = models.ForeignKey(
        "parties.Party", on_delete=models.CASCADE, null=True, blank=True, related_name="touchpoints",
    )
    engagement = models.ForeignKey(
        Engagement, on_delete=models.SET_NULL, null=True, blank=True, related_name="touchpoints",
    )
    occurred_at = models.DateTimeField()
    channel = models.CharField(max_length=32, blank=True)
    utm_source = models.CharField(max_length=255, blank=True)
    utm_medium = models.CharField(max_length=255, blank=True)
    utm_campaign = models.CharField(max_length=255, blank=True)
    utm_term = models.CharField(max_length=255, blank=True)
    utm_content = models.CharField(max_length=255, blank=True)
    referrer_url = models.URLField(max_length=2048, blank=True)
    landing_url = models.URLField(max_length=2048, blank=True)
    click_id_kind = models.CharField(max_length=16, blank=True, choices=ClickIdKind.choices)
    click_id = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    tenancy_parents = ("party", "engagement")

    class Meta:
        ordering = ["occurred_at"]
        indexes = [
            models.Index(fields=["company", "party", "occurred_at"], name="touch_co_party_at_idx"),
        ]

    def __str__(self):
        return f"{self.channel or 'touch'} @ {self.occurred_at:%Y-%m-%d}"

    def clean(self):
        from .registries import ENGAGEMENT_SOURCES

        if self.channel and self.channel not in ENGAGEMENT_SOURCES:
            raise ValidationError({"channel": f"Unknown channel {self.channel!r} (GOLDEN_ENGAGEMENT_SOURCES)."})


# --- 1.5 Task -----------------------------------------------------------------

OPEN_TASK_STATUSES = ("open", "in_progress", "waiting")


class Task(BaseModel):
    """A unit of work; the one next-action model (E4).

    ``due_at`` is a UTC instant and ``due_tz`` the IANA zone it was set in;
    an all-day task is due at the last instant of that local day (E7). Every
    save or delete that touches an engagement recomputes its next action in
    the same transaction. Lifecycle changes with history go through
    ``crm.tasks``, which writes ``TaskEvent`` rows.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        WAITING = "waiting", "Waiting"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Canceled"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=32, default="todo")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=6, choices=Priority.choices, default=Priority.NORMAL)
    assignee = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="crm_tasks",
    )
    created_by = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    engagement = models.ForeignKey(
        Engagement, on_delete=models.CASCADE, null=True, blank=True, related_name="tasks",
    )
    party = models.ForeignKey(
        "parties.Party", on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks",
    )
    # A task raised from a call or meeting (Phase 6: meeting action items).
    communication = models.ForeignKey(
        "communications.Communication", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks",
    )
    due_at = models.DateTimeField(null=True, blank=True)
    all_day = models.BooleanField(default=True)
    due_tz = models.CharField(max_length=64, blank=True, validators=[validate_timezone])
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    due_notified_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    tenancy_parents = ("engagement", "party", "communication")

    class Meta:
        ordering = ["due_at", "created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status="done", completed_at__isnull=False)
                | (~Q(status="done") & Q(completed_at__isnull=True)),
                name="task_completed_at_iff_done",
            ),
            models.CheckConstraint(
                condition=Q(due_at__isnull=True) | ~Q(due_tz=""), name="task_due_has_tz",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "assignee", "due_at"],
                         condition=Q(status__in=["open", "in_progress", "waiting"]),
                         name="task_co_assignee_open_due_idx"),
            models.Index(fields=["engagement", "due_at"],
                         condition=Q(status__in=["open", "in_progress", "waiting"]),
                         name="task_eng_open_due_idx"),
            models.Index(fields=["company", "status", "completed_at"], name="task_co_completed_idx"),
        ]

    def __str__(self):
        return self.title

    @property
    def is_open(self):
        return self.status in OPEN_TASK_STATUSES

    @property
    def is_overdue(self):
        """``due_at < now()`` in UTC: all-day tasks already encode end of day."""
        return self.is_open and self.due_at is not None and self.due_at < timezone.now()

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_engagement_id = instance.__dict__.get("engagement_id")
        instance._snapshot_lifecycle()
        return instance

    #: Columns snapshotted as last read or written: ``status`` / ``assignee``
    #: for the lifecycle signals, and the rest so ``task_updated`` can say
    #: which written columns actually changed (a full ``save()`` writes all).
    SNAPSHOT_ATTNAMES = ("status", "assignee_id", "title", "description", "priority", "due_at", "all_day")

    def _snapshot_lifecycle(self, fields=None):
        """The ``SNAPSHOT_ATTNAMES`` values as last read or written, for the task signals."""
        from .lifecycle import UNKNOWN

        for attname in self.SNAPSHOT_ATTNAMES:
            if fields is None or attname in fields or attname.removesuffix("_id") in fields:
                setattr(self, f"_loaded_{attname}", self.__dict__.get(attname, UNKNOWN))

    def clean(self):
        from .registries import TASK_KINDS

        errors = {}
        if self.kind not in TASK_KINDS:
            errors["kind"] = f"Unknown task kind {self.kind!r} (GOLDEN_TASK_KINDS)."
        if self.engagement_id and self.company_id and self.engagement.company_id != self.company_id:
            errors["engagement"] = "The engagement belongs to another company."
        if self.party_id and self.company_id and self.party.company_id != self.company_id:
            errors["party"] = "The party belongs to another company."
        if self.communication_id and self.company_id and self.communication.company_id != self.company_id:
            errors["communication"] = "The communication belongs to another company."
        for name in ("assignee", "created_by"):
            user = getattr(self, name) if getattr(self, f"{name}_id") else None
            if user is not None and self.company_id and user.company_id != self.company_id:
                errors[name] = f"The {name.replace('_', ' ')} belongs to another company."
        if errors:
            raise ValidationError(errors)

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        if fields is None or "engagement" in fields or "engagement_id" in fields:
            self._loaded_engagement_id = self.engagement_id
        self._snapshot_lifecycle(fields)

    def save(self, *args, **kwargs):
        if self.engagement_id is not None:
            self.company_id = self.engagement.company_id
            if self.party_id is None and self._state.adding:
                self.party_id = self.engagement.party_id
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "company"}
        previous = getattr(self, "_loaded_engagement_id", None)
        from .lifecycle import UNKNOWN, task_saved

        adding = self._state.adding
        loaded_status = getattr(self, "_loaded_status", UNKNOWN)
        loaded_assignee_id = getattr(self, "_loaded_assignee_id", UNKNOWN)
        loaded = {name: getattr(self, f"_loaded_{name}", UNKNOWN) for name in self.SNAPSHOT_ATTNAMES}
        written = kwargs.get("update_fields")
        with transaction.atomic(using=kwargs.get("using")):
            super().save(*args, **kwargs)
            from .tasks import recompute_next_action

            for engagement_id in {previous, self.engagement_id} - {None}:
                recompute_next_action(engagement_id)
            # task_created / _assigned / _completed / _reopened (spec 5.1),
            # registered inside the save's transaction: a rollback drops them.
            task_saved(self, adding=adding, loaded_status=loaded_status,
                       loaded_assignee_id=loaded_assignee_id,
                       written=None if written is None else set(written), loaded=loaded)
        self._loaded_engagement_id = self.engagement_id
        self._snapshot_lifecycle(None if written is None else set(written))


@receiver(post_delete, sender=Task, dispatch_uid="crm.task_deleted.recompute_next_action")
def _task_deleted(sender, instance, **kwargs):
    """A deleted task (instance or queryset delete) leaves its engagement's
    next action stale; the collector sends this inside its transaction."""
    if instance.engagement_id is not None:
        from .tasks import recompute_next_action

        recompute_next_action(instance.engagement_id)


class TaskEvent(_AppendOnly, _CompanyFromParent, BaseModel):
    """Append-only task history: completion, reopen, reschedule (spec 1.5)."""

    class Event(models.TextChoices):
        CREATED = "created", "Created"
        ASSIGNED = "assigned", "Assigned"
        RESCHEDULED = "rescheduled", "Rescheduled"
        STARTED = "started", "Started"
        COMPLETED = "completed", "Completed"
        REOPENED = "reopened", "Reopened"
        CANCELLED = "cancelled", "Canceled"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="events")
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    event = models.CharField(max_length=12, choices=Event.choices)
    from_value = models.CharField(max_length=64, blank=True)
    to_value = models.CharField(max_length=64, blank=True)
    at = models.DateTimeField(default=timezone.now)
    by = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    objects = CompanyScopedManager()

    company_from = "task"
    tenancy_parents = ("task",)

    class Meta:
        ordering = ["at"]
        indexes = [models.Index(fields=["task", "at"], name="taskevent_task_at_idx")]

    def __str__(self):
        return f"{self.event} {self.from_value}->{self.to_value}"

    def save(self, *args, **kwargs):
        self._copy_company(kwargs)
        super().save(*args, **kwargs)
