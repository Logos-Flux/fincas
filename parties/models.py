"""Party schema (docs/crm/phase2-party.md section 1, implementation step 3).

Every model here has ``company`` NOT NULL and ``CompanyScopedManager``
(3.1). On child tables ``company`` is denormalised from the owning party:
``save()`` always copies it and ignores any value the caller set
(``CompanyFromParentMixin``). ``manage.py check_tenancy --app parties``
asserts the copies still agree, using each model's ``tenancy_parents``.

Constraints are named exactly as the spec names them; the parity baseline
and the downstream comparison key on those names, so never rename one.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from core import lookups  # noqa: F401  # registers `__ne` for uniq_partyphone_party_e164
from core.managers import CompanyScopedManager
from core.models import BaseModel
from core.teams import OwnerTeamStamped

from .constants import LINE_FAX, LINE_LANDLINE, LINE_MOBILE, LINE_UNKNOWN, ORGANISATION, PERSON
from .normalize import (
    normalize_domain,
    normalize_email,
    normalize_phone,
    person_display_name,
    phone_line_type,
)
from .relationship_registry import get_relationship_kind
from .role_registry import get_party_role


def _skip_save(kwargs) -> bool:
    """True for ``save(update_fields=[])``, which Django treats as "write nothing"."""
    update_fields = kwargs.get("update_fields")
    return update_fields is not None and not update_fields


def _add_update_fields(kwargs, *fields) -> None:
    """Extend an explicit ``update_fields`` with the columns ``save()`` derives."""
    update_fields = kwargs.get("update_fields")
    if update_fields is not None:
        kwargs["update_fields"] = {*update_fields, *fields}


class CompanyFromParentMixin:
    """Copy ``company`` from the owning party on every save (spec 1, 3.1).

    Do not rename or move this class: ``parties/migrations/0001_initial.py``
    names it in ``bases``. Historical models in data migrations inherit this
    ``save()`` (so they copy ``company``) but not the subclasses' normalising
    ``save()``; a data migration must normalise explicitly.

    ``company_from`` names the FK whose ``company`` is copied. When that FK is
    null (only ``ConsentRecord.party`` may be), the caller's ``company`` is
    kept. ``tenancy_parents`` lists every FK whose party must share the row's
    tenant; ``check_tenancy`` reads it.
    """

    company_from: str = "party"
    tenancy_parents: tuple[str, ...] = ("party",)

    def save(self, *args, **kwargs):
        if _skip_save(kwargs):
            return
        if getattr(self, f"{self.company_from}_id") is not None:
            self.company_id = getattr(self, self.company_from).company_id
            _add_update_fields(kwargs, "company")
        super().save(*args, **kwargs)


class Purpose(models.TextChoices):
    PERSONAL = "personal", "Personal"
    WORK = "work", "Work"
    BILLING = "billing", "Billing"
    OTHER = "other", "Other"


class Channel(models.TextChoices):
    EMAIL = "email", "Email"
    SMS = "sms", "SMS"
    PHONE = "phone", "Phone"
    POST = "post", "Post"


class ConsentSource(models.TextChoices):
    PORTAL = "portal", "Portal"
    ERP = "erp", "ERP"
    API = "api", "API"
    IMPORT = "import", "Import"
    BACKFILL = "backfill", "Backfill"
    DOUBLE_OPT_IN = "double_opt_in", "Double opt-in"


# --- 1.1 Party ---------------------------------------------------------------


class Party(OwnerTeamStamped, BaseModel):
    """A person or organisation the tenant deals with (spec 1.1).

    ``owner_team`` is stamped from the owner's team when the owner changes
    (``core.teams``); ``lifecycle_stage`` / ``contact_type`` are keys of
    ``GOLDEN_LIFECYCLE_STAGES`` / ``GOLDEN_CONTACT_TYPES`` (blank = unknown /
    unset), and ``lifecycle_stage_changed_at`` is set whenever the stage
    changes unless the writer names its own time (an import's source
    timestamp). Phase 4 fields spec 2.1, 2.2.
    """

    class Kind(models.TextChoices):
        PERSON = PERSON, "Person"
        ORGANISATION = ORGANISATION, "Organization"

    class Origin(models.TextChoices):
        BACKFILL = "backfill", "Backfill"
        ERP = "erp", "ERP"
        API = "api", "API"
        PORTAL = "portal", "Portal"
        LEAD_FORM = "lead_form", "Lead form"
        IMPORT = "import", "Import"
        SYSTEM = "system", "System"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="parties")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    display_name = models.CharField(max_length=255)
    display_name_locked = models.BooleanField(default=False)
    given_name = models.CharField(max_length=100, blank=True)
    family_name = models.CharField(max_length=100, blank=True)
    legal_name = models.CharField(max_length=255, blank=True)
    primary_domain = models.CharField(max_length=253, blank=True)
    registration_number = models.CharField(max_length=64, blank=True)
    tax_id = models.CharField(max_length=64, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="parties",
    )
    owner = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_parties",
    )
    owner_team = models.ForeignKey(
        "core.Team", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    lifecycle_stage = models.CharField(max_length=32, blank=True)
    lifecycle_stage_changed_at = models.DateTimeField(null=True, blank=True)
    contact_type = models.CharField(max_length=32, blank=True)
    is_self = models.BooleanField(default=False)
    marketable = models.BooleanField(default=False)
    marketable_changed_at = models.DateTimeField(null=True, blank=True)
    marketable_source = models.CharField(max_length=64, blank=True)
    primary_email_normalized = models.CharField(max_length=254, blank=True)
    primary_phone_e164 = models.CharField(max_length=16, blank=True)
    origin = models.CharField(max_length=16, choices=Origin.choices)
    merged_into = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="merged_from",
    )
    merged_at = models.DateTimeField(null=True, blank=True)
    erased_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)

    objects = CompanyScopedManager()

    # check_tenancy: a merge never crosses tenants; nor does the owner team.
    tenancy_parents = ("merged_into", "owner_team")

    class Meta:
        verbose_name_plural = "parties"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "company"], condition=Q(user__isnull=False),
                name="uniq_party_user_per_company",
            ),
            models.UniqueConstraint(
                fields=["company"], condition=Q(is_self=True), name="uniq_party_self_per_company",
            ),
            models.CheckConstraint(
                condition=Q(kind=PERSON) | Q(user__isnull=True), name="party_user_person_only",
            ),
            models.CheckConstraint(
                condition=Q(kind=PERSON) | (Q(given_name="") & Q(family_name="")),
                name="party_org_has_no_person_name",
            ),
            models.CheckConstraint(
                condition=Q(merged_into__isnull=True) | ~Q(merged_into=F("id")),
                name="party_not_merged_into_self",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "kind", "display_name"], name="party_co_kind_name_idx"),
            models.Index(fields=["company", "primary_email_normalized"], name="party_co_email_idx"),
            models.Index(fields=["company", "primary_phone_e164"], name="party_co_phone_idx"),
            models.Index(fields=["company", "-updated_at"], name="party_co_updated_idx"),
            models.Index(fields=["company", "primary_domain"], name="party_co_domain_idx"),
            models.Index(fields=["company", "owner_team"], name="party_co_team_idx"),
            models.Index(fields=["company", "lifecycle_stage"], name="party_co_lifecycle_idx"),
            models.Index(fields=["company", "contact_type"], name="party_co_contact_type_idx"),
        ]
        permissions = [
            ("merge_party", "Can merge and unmerge parties"),
            ("erase_party", "Can erase a party's personal data"),
            ("lift_suppression", "Can lift a contact suppression"),
            ("export_pii", "Can export personal data columns from CRM lists"),
        ]

    def __str__(self):
        return self.display_name

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        # The kind as stored, so clean() can refuse a change (1.1: immutable).
        instance._loaded_kind = instance.__dict__.get("kind")
        instance._snapshot_lifecycle()
        return instance

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        if fields is None or {"lifecycle_stage", "lifecycle_stage_changed_at"} & set(fields):
            self._snapshot_lifecycle()

    def _snapshot_lifecycle(self):
        self._loaded_lifecycle = (
            self.__dict__.get("lifecycle_stage"), self.__dict__.get("lifecycle_stage_changed_at"),
        )

    def clean(self):
        super().clean()
        loaded = getattr(self, "_loaded_kind", None)
        if loaded is not None and self.kind != loaded:
            raise ValidationError({"kind": "A party's kind cannot change after it is created."})
        if self.kind == ORGANISATION and self.user_id is not None:
            raise ValidationError({"user": "Only a person party can have a login."})
        if self.kind == ORGANISATION and (self.given_name or self.family_name):
            raise ValidationError("An organization has no given or family name.")
        from .classification_registry import CONTACT_TYPES, LIFECYCLE_STAGES

        errors = {}
        if self.lifecycle_stage and self.lifecycle_stage not in LIFECYCLE_STAGES:
            errors["lifecycle_stage"] = (
                f"Unknown lifecycle stage {self.lifecycle_stage!r} (GOLDEN_LIFECYCLE_STAGES)."
            )
        if self.contact_type and self.contact_type not in CONTACT_TYPES:
            errors["contact_type"] = f"Unknown contact type {self.contact_type!r} (GOLDEN_CONTACT_TYPES)."
        for name in ("owner", "owner_team"):
            related = getattr(self, name) if getattr(self, f"{name}_id") else None
            if related is not None and self.company_id and related.company_id != self.company_id:
                errors[name] = f"The {name.replace('_', ' ')} belongs to another company."
        if errors:
            raise ValidationError(errors)

    def _stamp_lifecycle_changed_at(self, kwargs) -> None:
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "lifecycle_stage" not in update_fields:
            return
        if "lifecycle_stage" not in self.__dict__:
            return
        adding = self._state.adding or bool(kwargs.get("force_insert"))
        loaded_stage, loaded_at = getattr(self, "_loaded_lifecycle", (None, None))
        changed = bool(self.lifecycle_stage) if adding else self.lifecycle_stage != (loaded_stage or "")
        if not changed:
            return
        named = self.lifecycle_stage_changed_at is not None if adding else (
            self.lifecycle_stage_changed_at != loaded_at
        )
        if not named:
            self.lifecycle_stage_changed_at = timezone.now()
        _add_update_fields(kwargs, "lifecycle_stage_changed_at")

    # Denormalised from the primary PartyEmail / PartyPhone. Their only writer
    # is ``refresh_primary_party_contact``; ``save()`` never writes them back
    # from a possibly stale instance.
    DENORMALISED_CONTACT_FIELDS = ("primary_email_normalized", "primary_phone_e164")

    def save(self, *args, **kwargs):
        if _skip_save(kwargs):
            return
        if self.kind == PERSON and not self.display_name_locked:
            derived = person_display_name(self.given_name, self.family_name)
            if derived:
                self.display_name = derived
        if self.primary_domain:
            self.primary_domain = normalize_domain(self.primary_domain)
        _add_update_fields(kwargs, "display_name", "primary_domain")
        self._stamp_owner_team(kwargs)
        self._stamp_lifecycle_changed_at(kwargs)
        if (
            kwargs.get("update_fields") is None
            and not self._state.adding
            and not kwargs.get("force_insert")
        ):
            kwargs["update_fields"] = [
                field.name for field in self._meta.concrete_fields
                if not field.primary_key and field.name not in self.DENORMALISED_CONTACT_FIELDS
            ]
        adding = self._state.adding or bool(kwargs.get("force_insert"))
        super().save(*args, **kwargs)
        self._loaded_kind = self.kind
        self._after_owner_save(kwargs)
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "lifecycle_stage" in update_fields:
            self._snapshot_lifecycle()
        # party_created / party_updated (phase2-party.md 5.1), after commit.
        # The backfill writes with bulk_create / update, so it never gets here.
        from core.plugins import signals
        from core.plugins.dispatch import emit

        if adding:
            emit(signals.party_created, Party, self, party=self)
        else:
            emit(signals.party_updated, Party, self, party=self,
                 update_fields=tuple(sorted(kwargs.get("update_fields") or ())))

    def refresh_primary_contact(self) -> None:
        """Re-derive the primary email and phone columns from the contact points."""
        email, phone = refresh_primary_party_contact(self.pk)
        self.primary_email_normalized, self.primary_phone_e164 = email, phone


def refresh_primary_party_contact(party_id) -> tuple[str, str]:
    """Write ``Party.primary_email_normalized`` / ``primary_phone_e164`` (spec 1.1).

    Runs from ``post_save`` / ``post_delete`` of PartyEmail and PartyPhone,
    so queryset deletes are covered too. ``QuerySet.update()`` and
    ``bulk_create`` still bypass it (``reconcile_parties``, step 5). Always
    writes: comparing against an in-memory copy could skip a needed update.
    """
    email = (
        PartyEmail.objects.filter(party_id=party_id, is_primary=True)
        .values_list("email_normalized", flat=True).first() or ""
    )
    phone = (
        PartyPhone.objects.filter(party_id=party_id, is_primary=True)
        .values_list("number_e164", flat=True).first() or ""
    )
    Party.objects.filter(pk=party_id).update(
        primary_email_normalized=email, primary_phone_e164=phone,
    )
    return email, phone


# --- 1.2 PartyRole ------------------------------------------------------------


class PartyRole(CompanyFromParentMixin, BaseModel):
    """A dated statement of what a party is to the tenant (spec 1.2)."""

    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="roles")
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    role = models.CharField(max_length=32)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["party", "role"], condition=Q(valid_to__isnull=True),
                name="uniq_partyrole_open",
            ),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="partyrole_dates_ordered",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "role", "valid_to"], name="partyrole_co_role_open_idx"),
        ]

    def __str__(self):
        return f"{self.party} as {self.role}"

    def clean(self):
        super().clean()
        try:
            config = get_party_role(self.role)
        except KeyError:
            raise ValidationError({"role": f"{self.role!r} is not a party role (GOLDEN_PARTY_ROLES)."})
        if self.party_id is not None and self.party.kind not in config.kinds:
            raise ValidationError(
                {"role": f"A {self.party.kind} party cannot hold the {self.role!r} role."}
            )


# --- 1.3 PartyRelationship ----------------------------------------------------


class PartyRelationship(CompanyFromParentMixin, BaseModel):
    """A dated link between two parties of one tenant (spec 1.3).

    ``is_symmetric`` is copied from ``GOLDEN_RELATIONSHIP_KINDS`` on save, so
    an unknown kind cannot be saved. A symmetric row must be stored with
    ``from_party < to_party``; ``parties.relationships.relate`` (a later step)
    swaps the ends for callers.
    """

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    from_party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="relationships_out")
    to_party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="relationships_in")
    kind = models.CharField(max_length=32)
    is_symmetric = models.BooleanField()
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    title = models.CharField(max_length=100, blank=True)
    is_primary = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    source = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    company_from = "from_party"
    tenancy_parents = ("from_party", "to_party")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(from_party=F("to_party")), name="partyrel_not_self"),
            # A symmetric relation is stored once, in canonical order.
            models.CheckConstraint(
                condition=Q(is_symmetric=False) | Q(from_party__lt=F("to_party")),
                name="partyrel_symmetric_canonical",
            ),
            models.UniqueConstraint(
                fields=["from_party", "to_party", "kind"], condition=Q(valid_to__isnull=True),
                name="uniq_partyrel_open",
            ),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_from__isnull=True)
                | Q(valid_to__gte=F("valid_from")),
                name="partyrel_dates_ordered",
            ),
        ]
        indexes = [models.Index(fields=["to_party", "kind"], name="partyrel_to_kind_idx")]

    def __str__(self):
        return f"{self.from_party} {self.kind} {self.to_party}"

    def _kind_config(self):
        try:
            return get_relationship_kind(self.kind)
        except KeyError:
            raise ValidationError(
                {"kind": f"{self.kind!r} is not a relationship kind (GOLDEN_RELATIONSHIP_KINDS)."}
            ) from None

    def clean(self):
        super().clean()
        config = self._kind_config()
        if self.from_party_id is None or self.to_party_id is None:
            return
        if self.from_party.company_id != self.to_party.company_id:
            raise ValidationError("Both parties of a relationship must belong to the same company.")
        if not config.allows(self.from_party.kind, self.to_party.kind):
            raise ValidationError(
                {"kind": f"{self.kind!r} cannot link a {self.from_party.kind} to a "
                         f"{self.to_party.kind}."}
            )

    def save(self, *args, **kwargs):
        if _skip_save(kwargs):
            return
        self.is_symmetric = self._kind_config().symmetric
        _add_update_fields(kwargs, "is_symmetric")
        super().save(*args, **kwargs)


# --- 1.4 contact points ------------------------------------------------------


class PartyEmail(CompanyFromParentMixin, BaseModel):
    """An email address of a party (spec 1.4). Never unique across parties."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        BOUNCED = "bounced", "Bounced"
        OBSOLETE = "obsolete", "Obsolete"

    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="emails")
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    email = models.EmailField(max_length=254)
    email_normalized = models.CharField(max_length=254)
    purpose = models.CharField(max_length=12, choices=Purpose.choices)
    is_primary = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    source = models.CharField(max_length=64, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["party", "email_normalized"], name="uniq_partyemail_party_addr",
            ),
            models.UniqueConstraint(
                fields=["party"], condition=Q(is_primary=True), name="uniq_partyemail_one_primary",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "email_normalized"], name="partyemail_co_addr_idx"),
        ]

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        if _skip_save(kwargs):
            return
        # 7.2: the only column matching reads. Raises ValidationError on an
        # address without exactly one "@".
        self.email_normalized = normalize_email(self.email)
        _add_update_fields(kwargs, "email_normalized")
        super().save(*args, **kwargs)


class PartyPhone(CompanyFromParentMixin, BaseModel):
    """A phone number of a party (spec 1.4). ``number_raw`` is never discarded."""

    class LineType(models.TextChoices):
        MOBILE = LINE_MOBILE, "Mobile"
        LANDLINE = LINE_LANDLINE, "Landline"
        FAX = LINE_FAX, "Fax"
        UNKNOWN = LINE_UNKNOWN, "Unknown"

    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="phones")
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    number_raw = models.CharField(max_length=50)
    number_e164 = models.CharField(max_length=16, blank=True)
    extension = models.CharField(max_length=10, blank=True)
    line_type = models.CharField(max_length=10, choices=LineType.choices, default=LineType.UNKNOWN)
    purpose = models.CharField(max_length=12, choices=Purpose.choices)
    is_primary = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=64, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        constraints = [
            # `__ne` (core.lookups) stores the predicate as `number_e164 <> ''`
            # (spec 1.4): unparseable numbers are blank and never collide.
            models.UniqueConstraint(
                fields=["party", "number_e164"], condition=Q(number_e164__ne=""),
                name="uniq_partyphone_party_e164",
            ),
            models.UniqueConstraint(
                fields=["party"], condition=Q(is_primary=True), name="uniq_partyphone_one_primary",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "number_e164"], name="partyphone_co_e164_idx"),
        ]

    def __str__(self):
        return self.number_e164 or self.number_raw

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_number_raw = instance.__dict__.get("number_raw")
        return instance

    def save(self, *args, **kwargs):
        if _skip_save(kwargs):
            return
        # 7.3: E.164 and extension from the raw text; blank when unparseable.
        # ``phone_region`` (not a column) is the address country when the
        # caller knows it (parties.sync passes the customer's); otherwise
        # national numbers parse against GOLDEN_DEFAULT_PHONE_REGION.
        e164, extension = normalize_phone(self.number_raw, getattr(self, "phone_region", None))
        raw_changed = not self._state.adding and self.number_raw != getattr(
            self, "_loaded_number_raw", self.number_raw
        )
        self.number_e164 = e164
        # A parsed extension wins. On a new row an extension typed separately
        # is kept; when the number changes, the old one is not carried over.
        if extension or raw_changed:
            self.extension = extension
        # `fax` is only ever set by staff; otherwise infer where the library
        # can, and re-infer when the number changes.
        if self.line_type != LINE_FAX and (raw_changed or self.line_type in ("", LINE_UNKNOWN)):
            self.line_type = phone_line_type(e164)
        _add_update_fields(kwargs, "number_e164", "extension", "line_type")
        super().save(*args, **kwargs)
        self._loaded_number_raw = self.number_raw


class PartyAddress(CompanyFromParentMixin, BaseModel):
    """A postal address of a party (spec 1.4). Logistics flags stay on CustomerAddress."""

    class AddressPurpose(models.TextChoices):
        BILLING = "billing", "Billing"
        SHIPPING = "shipping", "Shipping"
        MAILING = "mailing", "Mailing"
        REGISTERED = "registered", "Registered"
        OTHER = "other", "Other"

    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="addresses")
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    label = models.CharField(max_length=100, blank=True)
    purpose = models.CharField(max_length=12, choices=AddressPurpose.choices)
    # Same lengths as customers.CustomerAddress.
    address_line_1 = models.CharField(max_length=255)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state_province = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=20)
    country = models.CharField(max_length=2)
    is_primary = models.BooleanField(default=False)
    valid_to = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=64, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        verbose_name_plural = "party addresses"
        constraints = [
            models.UniqueConstraint(
                fields=["party"], condition=Q(is_primary=True),
                name="uniq_partyaddress_one_primary",
            ),
        ]

    def __str__(self):
        return f"{self.address_line_1}, {self.city}"

    def save(self, *args, **kwargs):
        if _skip_save(kwargs):
            return
        self.country = (self.country or "").strip().upper()
        _add_update_fields(kwargs, "country")
        super().save(*args, **kwargs)


# --- 1.6 consent -------------------------------------------------------------


class ConsentRecord(CompanyFromParentMixin, BaseModel):
    """Append-only consent ledger (spec 1.6). The latest row is the current state.

    ``save()`` refuses to update an existing row. ``party`` is nullable
    (SET_NULL), so ``company`` is copied from it only while it is set.
    """

    class Purpose(models.TextChoices):
        MARKETING = "marketing", "Marketing"
        TRANSACTIONAL = "transactional", "Transactional"

    class State(models.TextChoices):
        GRANTED = "granted", "Granted"
        WITHDRAWN = "withdrawn", "Withdrawn"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    party = models.ForeignKey(
        Party, on_delete=models.SET_NULL, null=True, blank=True, related_name="consents",
    )
    channel = models.CharField(max_length=8, choices=Channel.choices)
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    state = models.CharField(max_length=10, choices=State.choices)
    address_normalized = models.CharField(max_length=254, blank=True)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=16, choices=ConsentSource.choices)
    source_detail = models.CharField(max_length=255, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    objects = CompanyScopedManager()

    class Meta:
        indexes = [
            models.Index(
                fields=["party", "channel", "purpose", "-occurred_at"],
                name="consent_party_latest_idx",
            ),
        ]

    def __str__(self):
        return f"{self.channel}/{self.purpose}: {self.state}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Consent records are append-only: record a new row instead of editing one."
            )
        super().save(*args, **kwargs)


class Suppression(BaseModel):
    """Address-keyed, party-independent do-not-contact list (spec 1.6).

    Survives erasure: it stores only ``sha256(normalized address)``.
    """

    class Reason(models.TextChoices):
        OPT_OUT = "opt_out", "Opt-out"
        HARD_BOUNCE = "hard_bounce", "Hard bounce"
        COMPLAINT = "complaint", "Complaint"
        LEGAL = "legal", "Legal"
        ERASURE = "erasure", "Erasure"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    channel = models.CharField(max_length=8, choices=Channel.choices)
    address_hash = models.CharField(max_length=64)
    address_hint = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=16, choices=Reason.choices)
    source = models.CharField(max_length=16, choices=ConsentSource.choices)
    source_detail = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    lifted_at = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    lift_reason = models.CharField(max_length=255, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "channel", "address_hash"], condition=Q(lifted_at__isnull=True),
                name="uniq_suppression_active",
            ),
        ]

    def __str__(self):
        return f"{self.channel} {self.address_hint or self.address_hash[:12]} ({self.reason})"


# --- 3.4 merge log ------------------------------------------------------------


class PartyMergeLog(CompanyFromParentMixin, BaseModel):
    """One loser folded into one survivor (spec 3.4), with what it takes to undo it.

    Written only by ``parties.merge.merge``. A cluster of N parties writes
    N-1 rows sharing one ``merge_id``. ``before`` is the serialised
    before-state of every row the merge changed or deleted
    (``{"app.Model": {pk: {attname: value}}}``); ``moved`` and ``removed``
    list the ids it re-pointed and deleted per table. ``after_digest``
    fingerprints those rows just after the merge, so an undo can tell whether
    anything changed them since. ``parties.merge.unmerge`` restores
    ``before`` exactly and sets ``undone_at``.
    """

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    merge_id = models.UUIDField(db_index=True)
    survivor = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="merge_logs_won")
    loser = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="merge_logs_lost")
    moved = models.JSONField(default=dict, blank=True)
    removed = models.JSONField(default=dict, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after_digest = models.CharField(max_length=64)
    field_picks = models.JSONField(default=dict, blank=True)
    actor = models.CharField(max_length=255)
    approval_row = models.ForeignKey(
        "unify.ApprovalRow", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.CharField(max_length=255, blank=True)

    objects = CompanyScopedManager()

    company_from = "survivor"
    tenancy_parents = ("survivor", "loser")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(survivor=F("loser")), name="partymergelog_not_self"),
        ]
        indexes = [
            models.Index(fields=["company", "-created_at"], name="partymergelog_co_created_idx"),
            models.Index(fields=["survivor", "undone_at"], name="partymergelog_survivor_idx"),
        ]

    def __str__(self):
        return f"{self.loser_id} merged into {self.survivor_id}"


@receiver(post_save, sender=PartyEmail, dispatch_uid="parties.primary_contact.email_saved")
@receiver(post_delete, sender=PartyEmail, dispatch_uid="parties.primary_contact.email_deleted")
@receiver(post_save, sender=PartyPhone, dispatch_uid="parties.primary_contact.phone_saved")
@receiver(post_delete, sender=PartyPhone, dispatch_uid="parties.primary_contact.phone_deleted")
def _refresh_primary_contact(sender, instance, raw=False, **kwargs):
    if raw:  # loaddata: the fixture carries the party's columns as they were
        return
    email, phone = refresh_primary_party_contact(instance.party_id)
    party = instance._state.fields_cache.get("party")
    if party is not None:
        party.primary_email_normalized, party.primary_phone_e164 = email, phone
