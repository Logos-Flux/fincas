import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from . import lookups  # noqa: F401  # registers `__ne` for outbox_coalesce_uniq
from .managers import CompanyScopedManager
from .tz import validate_timezone


class BaseModel(models.Model):
    """Abstract base for all ERP models — UUID pk + timestamps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Company(BaseModel):
    """Business entity (e.g. Acme Corp, Global Supplies Ltd)."""

    name = models.CharField(max_length=255, db_index=True)
    website_url = models.URLField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    # Migration provenance
    bc_company_name = models.CharField(max_length=255, blank=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)
    # IANA zone; blank = settings.TIME_ZONE. Second in core.tz.resolve's order.
    timezone = models.CharField(
        max_length=64, blank=True, validators=[validate_timezone],
        help_text="IANA time zone, e.g. Europe/Berlin. Blank uses the deployment default.",
    )

    class Meta:
        verbose_name_plural = "companies"
        ordering = ["name"]

    def __str__(self):
        return self.name


class LabelOverride(BaseModel):
    """One tenant's rewording of a staff-UI noun (docs/labels.md, tenant layer).

    ``key`` is a registered label key (``crm.engagement``); ``singular`` /
    ``plural`` / ``article`` replace the golden default (and the deployment's
    ``GOLDEN_LABEL_OVERRIDES``) for every staff screen of ``company``. The
    setting ``GOLDEN_LABEL_TENANT_OVERRIDES`` still works; a row here wins
    over it. Edited at ``/erp/settings/labels/`` (``core.change_labeloverride``);
    read through ``core.labels`` only, which caches per company and is
    invalidated by the receivers below. Never reaches machine-facing output
    (API, CSV, PDF, email: ``core/tests/test_labels_never_leak.py``).
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="label_overrides")
    key = models.CharField(max_length=64)
    singular = models.CharField(max_length=60)
    plural = models.CharField(max_length=60)
    # "a" | "an" | "" (no article); empty means derive from the singular.
    article = models.CharField(max_length=3, blank=True)

    class Meta:
        db_table = "core_label_override"
        ordering = ["company", "key"]
        constraints = [
            models.UniqueConstraint(fields=["company", "key"], name="uniq_label_override_co_key"),
        ]

    def __str__(self):
        return f"{self.key} = {self.singular} ({self.company_id})"


class Team(BaseModel):
    """An owner team: org structure, not permissions (phase4-fields.md 2.1).

    Tenant data edited by staff with ``core.manage_teams``. ``key`` is
    immutable (imports map by it); an archived team (``is_active=False``)
    stays referenceable on the records stamped with it. A user's one primary
    team is ``UserProfile.team``; ``owner_team`` on Party and Engagement is
    *stamped* from the owner's team when the owner changes (``core.teams``),
    never recomputed when the owner later moves team.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="teams")
    key = models.SlugField(max_length=32)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "key"], name="uniq_team_co_key"),
        ]
        permissions = [("manage_teams", "Can create and edit teams")]

    def __str__(self):
        return self.name

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_key = instance.__dict__.get("key")
        instance._loaded_company_id = instance.__dict__.get("company_id")
        return instance

    def clean(self):
        super().clean()
        loaded = getattr(self, "_loaded_key", None)
        if loaded is not None and self.key != loaded:
            raise ValidationError({"key": "A team's key cannot change: imports map by it."})
        moved = getattr(self, "_loaded_company_id", None)
        if moved is not None and self.company_id != moved:
            raise ValidationError({"company": "A team cannot move to another company."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)
        self._loaded_key, self._loaded_company_id = self.key, self.company_id


class UserProfile(BaseModel):
    """The staff profile: company, display name, FK target for history.

    Session (`/erp/`) users link via ``user`` (AD-12): without it,
    ``scope_to_company`` cannot resolve a company for Django-session logins and
    fails closed to no rows (F-026).

    External logins live in ``UserIdentity``. The legacy ``supabase_uid``
    column was dropped by identity step 8 (identity-unification §8): a value
    on a profile with a user is kept as an inert ``UserIdentity``
    (``legacy_supabase`` or ``local``), and a real uid on a profile with no
    user (a historical FK target) is archived in ``legacy_external_id``.
    """

    user = models.OneToOneField(
        "auth.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="userprofile",
    )
    # Archive only: the old Supabase uid of a profile with no user, copied by
    # customers.0008_identity_retire_precheck. Nothing reads it to log in.
    legacy_external_id = models.CharField(max_length=255, blank=True, default="", editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.SET_NULL, null=True, blank=True
    )
    display_name = models.CharField(max_length=255, blank=True)
    # The user's one primary team (phase4-fields.md 2.1); same company.
    team = models.ForeignKey(
        Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="members",
    )
    # IANA zone; blank = the company's. First in core.tz.resolve's order.
    timezone = models.CharField(
        max_length=64, blank=True, validators=[validate_timezone],
        help_text="IANA time zone, e.g. Europe/Berlin. Blank uses the company's.",
    )

    # check_tenancy: a profile's team is its own company's.
    tenancy_parents = ("team",)

    class Meta:
        ordering = ["display_name"]

    def clean(self):
        super().clean()
        if self.team_id is not None and self.team.company_id != self.company_id:
            raise ValidationError({"team": "The team belongs to another company."})

    def save(self, *args, **kwargs):
        if self.team_id is not None and self.team.company_id != self.company_id:
            raise ValidationError({"team": "The team belongs to another company."})
        super().save(*args, **kwargs)

    def __str__(self):
        if self.display_name:
            return self.display_name
        if self.user_id is not None:
            return self.user.get_username()
        return self.legacy_external_id or str(self.pk)


class ERPUserSettings(models.Model):
    """Per-user ERP settings: forced password change, email verification,
    list preferences."""

    user = models.OneToOneField(
        "auth.User", on_delete=models.CASCADE, related_name="erp_settings"
    )
    must_change_password = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    # {list_id: {"columns": [column ids], "page_size": n}}
    # (docs/crm/phase4-fields.md 5.3). Written only by core.list_prefs.
    list_preferences = models.JSONField(default=dict, blank=True)
    # Dashboard quick actions the user chose to hide (keys from
    # core.quick_actions.QUICK_ACTIONS). Everything shows by default; an
    # unknown key is ignored, so a retired action never breaks the page.
    hidden_quick_actions = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name_plural = "ERP user settings"

    def __str__(self):
        return f"Settings for {self.user.username}"


class SavedFilter(BaseModel):
    """A named set of list parameters (docs/crm/phase4-fields.md 5.4).

    ``params`` hold filter, search and sort parameters only, never page or
    export, and are re-validated against the list's allow-lists on save and
    on every load (``core.saved_filters``): a parameter the list no longer
    accepts is dropped, never applied. ``private`` filters are the owner's;
    ``tenant`` filters are seen by every staff user of ``company``. Editing
    or deleting a tenant filter needs ownership or ``core.change_savedfilter``.
    """

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Only me"
        TENANT = "tenant", "Everyone in my company"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="saved_filters")
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="saved_filters")
    list_id = models.CharField(max_length=64)
    name = models.CharField(max_length=80)
    params = models.JSONField(default=dict, blank=True)
    visibility = models.CharField(max_length=8, choices=Visibility.choices, default=Visibility.PRIVATE)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "list_id", "name"],
                                    name="uniq_savedfilter_owner_list_name"),
        ]
        indexes = [
            models.Index(fields=["company", "list_id", "visibility"], name="savedfilter_co_list_vis_idx"),
        ]

    def __str__(self):
        return f"{self.list_id}: {self.name}"


class AuditLog(models.Model):
    """Change tracking for any model."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=20)  # create / update / delete
    model_name = models.CharField(max_length=100)
    object_id = models.UUIDField()
    changes = models.JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["model_name", "object_id"]),
        ]

    def __str__(self):
        return f"{self.action} {self.model_name} {self.object_id}"


class DocumentSequence(models.Model):
    """Shared auto-incrementing sequence for QT/SO/INV numbering."""

    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="document_sequence")
    next_number = models.IntegerField(default=1)

    class Meta:
        db_table = "core_document_sequence"

    def __str__(self):
        return f"Seq({self.company.name}: next={self.next_number})"

    @classmethod
    def get_next(cls, company):
        """Atomically get and increment the next document number."""
        from django.db import transaction
        from django.db.models import F

        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(company=company)
            num = seq.next_number
            cls.objects.filter(pk=seq.pk).update(next_number=F("next_number") + 1)
        return num

    @classmethod
    def get_next_number(cls, company, model, field, prefix, width=5, max_tries=1000):
        """Mint a globally-unique document number from a per-company sequence.

        Sequences restart at 1 per company while document numbers are
        globally unique, so a new tenant's first numbers collide with
        existing rows (second tenant's first order 500s on SO-00001).
        Skip past taken numbers — burning sequence values, never reusing.
        """
        for _ in range(max_tries):
            num = cls.get_next(company)
            candidate = f"{prefix}-{num:0{width}d}"
            if not model.objects.filter(**{field: candidate}).exists():
                return candidate
        raise RuntimeError(
            f"Could not mint a free {prefix}- number for {company} "
            f"after {max_tries} tries"
        )


class IntegrationCheck(BaseModel):
    """One staff-run connection test from the Integrations page.

    Append-only: each row is both the persisted "last test result" and the
    audit record of who ran which test when. Only nonsecret fields exist:
    target, outcome, error class, an operator sentence, HTTP status and
    latency. The sentence is redacted against every configured credential
    value before it is stored (``core.integration_checks.redact``); no
    request/response body, header or URL with a query string is kept.
    """

    # A registered test name, or ``<provider>:<account uuid>`` for a
    # Connected account (docs/crm/phase6-adapters.md 2.7, problem 15).
    target = models.CharField(max_length=128, db_index=True)
    # Set for a per-account check: only that tenant's staff see it. Null for
    # the server-level tests of ``ERP_INTEGRATIONS``.
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    ok = models.BooleanField()
    error_class = models.CharField(max_length=64, blank=True)
    what = models.CharField(max_length=500)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    # Nonsecret facts the test observed (model id, provider-declared methods,
    # destination host). Written through an allowlist, never a raw response.
    observed = models.JSONField(default=dict, blank=True)
    tested_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["target", "-created_at"])]

    def __str__(self):
        return f"{self.target} {'ok' if self.ok else self.error_class} @ {self.created_at:%Y-%m-%d %H:%M}"


class ExternalRef(BaseModel):
    """A source system's id for a golden row (docs/crm/phase2-party.md 1.7).

    The target is generic (``content_type`` + UUID ``object_id``, decision
    P4) so plugins can attach provenance to their own models without a core
    migration. That leaves no DB integrity on the target: a model registered
    with ``core.provenance.track`` has its refs deleted when a row is
    hard-deleted, and ``manage.py check_external_refs`` reports any that
    dangle. ``(company, source, object_type, source_id)`` is the identity of
    the source record and does not involve the target.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="external_refs")
    source = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64)
    source_id = models.CharField(max_length=255)
    content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.PROTECT, related_name="+",
    )
    object_id = models.UUIDField()
    target = GenericForeignKey("content_type", "object_id")
    source_url = models.URLField(blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "source", "object_type", "source_id"],
                name="uniq_extref_source_key",
            ),
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"], name="extref_target_idx"),
        ]

    def __str__(self):
        return f"{self.source}:{self.object_type}:{self.source_id}"


class ApiKey(BaseModel):
    """A per-integration API key (identity-unification D7, spec §2.6).

    Each key belongs to a service account ``svc-<name>`` (unusable password,
    not staff, not superuser, member of ``kind="service"`` roles), so every
    integration has its own principal, audit trail and revocation. The secret
    is shown once at creation (``manage.py create_api_key``) and only its
    SHA-256 is stored; ``prefix`` is the public lookup handle. Wire format:
    ``Authorization: Api-Key gk_<prefix>_<secret>``.

    ``scope`` is a registered bearer-token scope (``core.authentication``):
    ``"api"`` (the default) leaves the account to its role contract; a plugin
    scope also binds the key, and every bearer minted from it, to that scope's
    paths. Logic lives in ``core/service_accounts.py``.
    """

    user = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, related_name="api_keys",
    )
    name = models.CharField(max_length=100, unique=True)
    prefix = models.CharField(max_length=8, unique=True)
    key_hash = models.CharField(max_length=64)
    scope = models.CharField(max_length=64, default="api")
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.prefix})"

    def is_usable(self, now=None) -> bool:
        """Not revoked and not expired (``expires_at`` is exclusive)."""
        now = now or timezone.now()
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now


# Defined in core/identity/models.py (spec §2.2), registered under the core app.
class OutboxEvent(models.Model):
    """Work that must not be lost (docs/crm/phase6-adapters.md 1.2).

    A row is written inside the transaction that made the fact
    (``core.outbox``), so a rollback drops it and a crash after commit loses
    nothing. ``manage.py process_outbox`` claims due rows with a conditional
    UPDATE and a lease and runs the consumer's handler with no transaction
    open. Delivery is at-least-once; every consumer is idempotent.
    """

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        DEAD = "dead", "Dead"
        CANCELLED = "cancelled", "Canceled"

    # Also the delivery id sent to webhook subscribers (X-Golden-Delivery-Id).
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Null only for a company-less webhook event (global subscriptions only).
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    consumer = models.CharField(max_length=64)
    event = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=255)
    coalesce_key = models.CharField(max_length=255, blank=True, default="")
    object_type = models.CharField(max_length=64, blank=True, default="")
    object_id = models.UUIDField(null=True, blank=True)
    # The person the payload concerns, for erasure. No FK on purpose: a
    # deleted party must never cascade a delivery away.
    party_id = models.UUIDField(null=True, blank=True)
    # DjangoJSONEncoder: a spec carrying a UUID, Decimal or datetime is stored,
    # not lost (the encoder of the old in-request path was default=str).
    payload = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=10)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=128, blank=True, default="")
    # An error code and status, never a provider response body.
    last_error = models.CharField(max_length=500, blank=True, default="")
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["next_attempt_at"]
        constraints = [
            models.UniqueConstraint(fields=["consumer", "idempotency_key"], name="outbox_idem_uniq"),
            models.UniqueConstraint(
                fields=["consumer", "coalesce_key"], name="outbox_coalesce_uniq",
                condition=models.Q(state="pending") & models.Q(coalesce_key__ne=""),
            ),
            models.CheckConstraint(
                condition=~models.Q(state__in=["done", "dead"]) | models.Q(completed_at__isnull=False),
                name="outbox_completed_at",
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__lte=models.F("max_attempts")), name="outbox_attempts_cap",
            ),
        ]
        indexes = [
            models.Index(fields=["next_attempt_at"], name="outbox_due_idx",
                         condition=models.Q(state="pending")),
            models.Index(fields=["party_id"], name="outbox_party_idx",
                         condition=models.Q(party_id__isnull=False)),
            models.Index(fields=["consumer", "state"], name="outbox_consumer_state_idx"),
        ]

    def __str__(self):
        return f"{self.consumer}:{self.event} {self.state} ({self.attempts}/{self.max_attempts})"


from .identity.models import UserIdentity  # noqa: E402,F401

# Defined in core/integrations/models.py (docs/crm/phase6-adapters.md 2.6, 2.7).
from .integrations.models import IntegrationAccount, IntegrationCredential  # noqa: E402,F401


# The label cache (core.label_registry) must not outlive an edit.
def _invalidate_label_overrides(sender, instance, **kwargs):
    from .label_registry import invalidate_tenant

    invalidate_tenant(instance.company_id)


models.signals.post_save.connect(_invalidate_label_overrides, sender=LabelOverride,
                                 dispatch_uid="core.labels.invalidate.save")
models.signals.post_delete.connect(_invalidate_label_overrides, sender=LabelOverride,
                                   dispatch_uid="core.labels.invalidate.delete")
