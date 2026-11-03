import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from core.managers import CompanyScopedManager
from core.models import BaseModel


class EmailLog(models.Model):
    """Records every email sent from the ERP. Works across all document types."""

    class DocumentType(models.TextChoices):
        SALES_ORDER = "sales_order", "Sales Order"
        SHIPPING_NOTIFICATION = "shipping_notification", "Shipping Notification"
        QUOTE = "quote", "Quote"
        PURCHASE_ORDER = "purchase_order", "Purchase Order"
        INVOICE = "invoice", "Invoice"

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        DELIVERED = "delivered", "Delivered"
        BOUNCED = "bounced", "Bounced"

    class TriggerType(models.TextChoices):
        AUTO = "auto", "Automatic"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document_type = models.CharField(max_length=25, choices=DocumentType.choices)
    document_id = models.UUIDField()
    document_number = models.CharField(max_length=50)
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=255)
    template_name = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices)
    error_message = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=10, choices=TriggerType.choices)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    anymail_message_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["document_type", "document_id"]),
            models.Index(fields=["recipient_email"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.document_number} → {self.recipient_email} ({self.status})"


# --- CRM Phase 3: Communication (docs/crm/phase3-engagement.md section 2) -----
#
# A Communication is an interaction with someone outside the tenant (email,
# SMS, call, meeting, LinkedIn message) or an internal note about one. It is
# not a Touchpoint (passive marketing, ``crm.Touchpoint``) and not a
# marketing send (Phase 6). ``company`` is NOT NULL on every table; the two
# child tables copy it from the communication on save (the Phase 2 rule) and
# list their parents in ``tenancy_parents`` for ``manage.py check_tenancy``.
# Write through ``communications.ingest.record`` (or the EmailLog link in
# ``communications.email_link``), which validates kind, direction, tenant and
# resolves participants.


class Communication(BaseModel):
    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"
        INTERNAL = "internal", "Internal"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        SCHEDULED = "scheduled", "Scheduled"
        CANCELLED = "cancelled", "Canceled"
        NO_SHOW = "no_show", "No show"
        FAILED = "failed", "Failed"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=32)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED)
    subject = models.CharField(max_length=255, blank=True)
    body_text = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    occurred_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    provider = models.CharField(max_length=32, blank=True)
    thread_key = models.CharField(max_length=255, blank=True)
    engagement = models.ForeignKey(
        "crm.Engagement", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="communications",
    )
    party = models.ForeignKey(
        "parties.Party", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="communications",
    )
    performed_by = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    email_log = models.OneToOneField(
        EmailLog, on_delete=models.SET_NULL, null=True, blank=True, related_name="communication",
    )
    is_editable = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    tenancy_parents = ("engagement", "party")

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["engagement", "-occurred_at"], name="comm_eng_at_idx"),
            models.Index(fields=["company", "party", "-occurred_at"], name="comm_co_party_at_idx"),
            models.Index(fields=["company", "thread_key"], condition=~Q(thread_key=""),
                         name="comm_co_thread_idx"),
            models.Index(fields=["company", "kind", "-occurred_at"], name="comm_co_kind_at_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F("occurred_at")),
                name="comm_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.direction} @ {self.occurred_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        adding = self._state.adding
        super().save(*args, **kwargs)
        if adding:
            from crm.lifecycle import communication_logged

            communication_logged(self)  # spec 5.1, after commit

    @property
    def kind_entry(self):
        """The registry entry, or a generic one if the kind's plugin is gone (2.6)."""
        from .kinds import kind_or_unknown

        return kind_or_unknown(self.kind)

    def clean(self):
        from .kinds import get_kind

        errors = {}
        kind = get_kind(self.kind)
        if kind is None:
            errors["kind"] = f"Unknown communication kind {self.kind!r} (GOLDEN_COMMUNICATION_KINDS)."
        else:
            if self.direction and self.direction not in kind.directions:
                errors["direction"] = (
                    f"A {kind.name!r} communication is {', '.join(kind.directions)}, "
                    f"not {self.direction!r}."
                )
            if not kind.has_duration and (self.duration_seconds is not None or self.ended_at is not None):
                errors["duration_seconds"] = f"A {kind.name!r} communication has no duration."
        for name in ("engagement", "party", "performed_by"):
            target = getattr(self, name) if getattr(self, f"{name}_id") else None
            if target is not None and self.company_id and target.company_id != self.company_id:
                errors[name] = f"The {name.replace('_', ' ')} belongs to another company."
        if errors:
            raise ValidationError(errors)


class _CopyCompanyFromCommunication(models.Model):
    """Child rows copy ``company`` from their communication on every save."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.communication_id is not None:
            self.company_id = self.communication.company_id
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "company"}
        super().save(*args, **kwargs)


class CommunicationParticipant(_CopyCompanyFromCommunication, BaseModel):
    """Who took part, and through which address (spec 2.2).

    Participants never create parties: an unknown address keeps ``party``
    NULL with its normalised address, and ``manage.py resolve_participants``
    (or the PartyEmail / PartyPhone save hook) links it once exactly one
    party holds that address.
    """

    class Role(models.TextChoices):
        FROM = "from", "From"
        TO = "to", "To"
        CC = "cc", "Cc"
        BCC = "bcc", "Bcc"
        ORGANIZER = "organizer", "Organizer"
        ATTENDEE = "attendee", "Attendee"
        CALLER = "caller", "Caller"
        CALLEE = "callee", "Callee"

    communication = models.ForeignKey(
        Communication, on_delete=models.CASCADE, related_name="participants",
    )
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    role = models.CharField(max_length=10, choices=Role.choices)
    party = models.ForeignKey(
        "parties.Party", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    party_email = models.ForeignKey(
        "parties.PartyEmail", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    party_phone = models.ForeignKey(
        "parties.PartyPhone", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    address_raw = models.CharField(max_length=254, blank=True)
    address_normalized = models.CharField(max_length=254, blank=True)
    display_name = models.CharField(max_length=255, blank=True)

    objects = CompanyScopedManager()

    tenancy_parents = ("communication", "party", "party_email", "party_phone")

    class Meta:
        indexes = [
            models.Index(fields=["company", "address_normalized"], name="commpart_co_addr_idx"),
            models.Index(fields=["party", "communication"], name="commpart_party_idx"),
        ]

    def __str__(self):
        return f"{self.role}: {self.display_name or self.address_raw or self.party_id}"


class CommunicationRaw(_CopyCompanyFromCommunication, models.Model):
    """The verbatim provider payload, kept off the Communication row (E10).

    Reading it needs ``communications.view_raw_communication``, which the
    built-in ``staff`` role does not carry: transcripts and full headers are
    more sensitive than the summary.
    """

    communication = models.OneToOneField(
        Communication, on_delete=models.CASCADE, primary_key=True, related_name="raw",
    )
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    provider = models.CharField(max_length=32)
    schema_version = models.PositiveSmallIntegerField()
    payload = models.JSONField()
    content_hash = models.CharField(max_length=64)
    fetched_at = models.DateTimeField()

    objects = CompanyScopedManager()

    tenancy_parents = ("communication",)

    class Meta:
        permissions = [
            ("view_raw_communication", "Can view raw provider payloads of communications"),
        ]

    def __str__(self):
        return f"{self.provider} v{self.schema_version} ({self.content_hash[:12]})"
