"""Staff REST API serializers for parties (docs/crm/phase2-party.md 5.2).

Rules every serializer here follows:

- ``company`` is read-only. A tenant user's rows take the requester's
  company (``CompanyScopedViewSet``); a child row's company is copied from
  its party on save (``CompanyFromParentMixin``). The only way to pick a
  tenant is a superuser creating a Party (``PartyViewSet.perform_create``).
- Every party reference is scoped to the requester's tenant and to live
  parties (not merged away, not erased), so another tenant's id is a 400
  "does not exist", never a cross-tenant link.
- Facts mirrored from a Customer or CustomerContact (decision P2,
  ``parties.ownership``) cannot be edited here: a write gets a 400 that
  names the owning record. ``customer`` and ``staff`` roles are system
  managed and refused.
- Contact points are normalised on write (7.2, 7.3) and an invalid email is
  a 400, not a model error.
- ``metadata`` writes are merged, never replaced, and only ``custom.*``
  keys in it are written; ``custom_fields`` is the write surface for defined
  sidecar fields (``core.sidecar_api``, phase4-fields.md 3.3).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.managers import scope_to_company
from core.models import UserProfile
from core.serializers import ScopedTeamField
from core.sidecar_api import CustomFieldsMixin, MetadataCompatMixin

from .classification_registry import CONTACT_TYPES, LIFECYCLE_STAGES
from .constants import ORGANISATION, PERSON
from .envelope import TARGETS
from .mirror import phone_parts
from .models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
)
from .normalize import normalize_email
from .ownership import (
    contact_point_owner,
    mirrored_party_fields,
    owner_message,
    party_owner,
    relationship_owner,
)
from .relationship_registry import get_relationship_kind
from .role_registry import get_party_role

API_SOURCE = "api"


def _request(serializer):
    return serializer.context.get("request")


def _django_errors(exc: DjangoValidationError):
    """A model ``ValidationError`` as DRF's 400 payload."""
    if hasattr(exc, "message_dict"):
        return serializers.ValidationError(exc.message_dict)
    return serializers.ValidationError({"non_field_errors": exc.messages})


class SafeSaveMixin:
    """Own the uniqueness checks; turn a constraint race into a 400.

    DRF < 3.18 derives a ``UniqueTogetherValidator`` from every
    ``UniqueConstraint`` and ignores its ``condition``: the conditional
    one-primary (``uniq_party*_one_primary``) and open-row
    (``uniq_partyrole_open``, ``uniq_partyrel_open``) constraints then
    refuse a second, non-primary or closed row. Every serializer here checks
    those constraints itself in ``validate`` (and demotes the old primary
    under a row lock), so the derived validators are dropped.

    Validation checks the one-primary and open-row unique constraints first;
    this covers the window between that check and the insert. The savepoint
    keeps the surrounding transaction usable.
    """

    def get_unique_together_validators(self):
        return []

    def save(self, **kwargs):
        try:
            with transaction.atomic():
                return super().save(**kwargs)
        except IntegrityError as exc:
            raise serializers.ValidationError(
                {"non_field_errors": [f"Conflicts with an existing row; retry. ({exc.__class__.__name__})"]}
            ) from exc
        except DjangoValidationError as exc:
            raise _django_errors(exc) from exc


class ScopedPartyField(serializers.PrimaryKeyRelatedField):
    """A live party of the requester's tenant (5.2, 3.1)."""

    def get_queryset(self):
        qs = Party.objects.filter(merged_into__isnull=True, erased_at__isnull=True)
        request = self.context.get("request")
        if request is None:
            return qs.none()
        return scope_to_company(qs, request)


class ScopedOwnerField(serializers.PrimaryKeyRelatedField):
    """A staff profile of the requester's tenant."""

    def get_queryset(self):
        request = self.context.get("request")
        if request is None:
            return UserProfile.objects.none()
        return scope_to_company(UserProfile.objects.all(), request)


class OwnerRefSerializer(serializers.Serializer):
    """The record a mirrored fact belongs to (decision P2)."""

    model = serializers.CharField(help_text="app_label.Model, e.g. customers.Customer")
    id = serializers.CharField()


class _OwnedMixin(serializers.Serializer):
    """Adds read-only ``mirrored_from``: the record that owns this row, if any."""

    mirrored_from = serializers.SerializerMethodField()

    @extend_schema_field(OwnerRefSerializer(allow_null=True))
    def get_mirrored_from(self, obj):
        return self.owner_of(obj)

    def owner_of(self, obj):  # pragma: no cover - overridden
        return None


def _demote_other_primaries(model, party, exclude_pk=None) -> None:
    """Keep the one-primary constraint (1.4) when a row is made primary.

    Locks the party row first so two concurrent "make primary" writes queue.
    """
    Party.objects.select_for_update().filter(pk=party.pk).first()
    qs = model.objects.filter(party=party, is_primary=True)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    qs.update(is_primary=False)


class _ContactPointSerializer(SafeSaveMixin, _OwnedMixin, serializers.ModelSerializer):
    """Shared rules of PartyEmail / PartyPhone / PartyAddress."""

    party = ScopedPartyField()

    def owner_of(self, obj):
        return contact_point_owner(obj)

    def validate_party(self, party):
        if self.instance is not None and party.pk != self.instance.party_id:
            raise serializers.ValidationError("A contact point cannot move to another party.")
        return party

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is not None:
            owner = contact_point_owner(self.instance)
            if owner is not None:
                raise serializers.ValidationError({"mirrored_from": owner_message(owner)})
        party = attrs.get("party") or getattr(self.instance, "party", None)
        if attrs.get("is_primary") and party is not None and not getattr(self.instance, "is_primary", False):
            # Demoting a mirrored primary would change the owning record's
            # primary contact behind its back (decision P2).
            for row in self.Meta.model.objects.filter(party=party, is_primary=True):
                owner = contact_point_owner(row)
                if owner is not None:
                    raise serializers.ValidationError({"is_primary": (
                        "The current primary is mirrored. " + owner_message(owner)
                    )})
        return attrs

    def create(self, validated_data):
        validated_data.setdefault("source", API_SOURCE)
        with transaction.atomic():
            if validated_data.get("is_primary"):
                _demote_other_primaries(self.Meta.model, validated_data["party"])
            try:
                return super().create(validated_data)
            except DjangoValidationError as exc:
                raise _django_errors(exc) from exc

    def update(self, instance, validated_data):
        with transaction.atomic():
            if validated_data.get("is_primary") and not instance.is_primary:
                _demote_other_primaries(self.Meta.model, instance.party, exclude_pk=instance.pk)
            try:
                return super().update(instance, validated_data)
            except DjangoValidationError as exc:
                raise _django_errors(exc) from exc


class PartyEmailSerializer(_ContactPointSerializer):
    class Meta:
        model = PartyEmail
        fields = [
            "id", "party", "company", "email", "email_normalized", "purpose", "is_primary",
            "verified_at", "status", "source", "mirrored_from", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "company", "email_normalized", "verified_at", "source", "created_at", "updated_at",
        ]

    def validate_email(self, value):
        try:
            normalize_email(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value.strip()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        party = attrs.get("party") or getattr(self.instance, "party", None)
        email = attrs.get("email")
        if party is not None and email:
            dupes = PartyEmail.objects.filter(party=party, email_normalized=normalize_email(email))
            if self.instance is not None:
                dupes = dupes.exclude(pk=self.instance.pk)
            if dupes.exists():
                raise serializers.ValidationError({"email": "This party already has that address."})
        return attrs


class PartyPhoneSerializer(_ContactPointSerializer):
    region = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=2,
        help_text="ISO 3166-1 country for a national-format number; default GOLDEN_DEFAULT_PHONE_REGION.",
    )

    class Meta:
        model = PartyPhone
        fields = [
            "id", "party", "company", "number_raw", "number_e164", "extension", "line_type",
            "purpose", "is_primary", "verified_at", "source", "region", "mirrored_from",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "company", "number_e164", "verified_at", "source", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        party = attrs.get("party") or getattr(self.instance, "party", None)
        raw = attrs.get("number_raw")
        if party is not None and raw:
            _raw, e164, _ext = phone_parts(raw, attrs.get("region"))
            if e164:
                dupes = PartyPhone.objects.filter(party=party, number_e164=e164)
                if self.instance is not None:
                    dupes = dupes.exclude(pk=self.instance.pk)
                if dupes.exists():
                    raise serializers.ValidationError({"number_raw": "This party already has that number."})
        return attrs

    def _apply_region(self, instance, region):
        if region:
            instance.phone_region = region.upper()

    def create(self, validated_data):
        region = validated_data.pop("region", None)
        validated_data.setdefault("source", API_SOURCE)
        with transaction.atomic():
            if validated_data.get("is_primary"):
                _demote_other_primaries(PartyPhone, validated_data["party"])
            instance = PartyPhone(**validated_data)
            self._apply_region(instance, region)
            instance.save()
            return instance

    def update(self, instance, validated_data):
        self._apply_region(instance, validated_data.pop("region", None))
        return super().update(instance, validated_data)


class PartyAddressSerializer(_ContactPointSerializer):
    class Meta:
        model = PartyAddress
        fields = [
            "id", "party", "company", "label", "purpose", "address_line_1", "address_line_2",
            "city", "state_province", "postal_code", "country", "is_primary", "valid_to",
            "source", "mirrored_from", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "source", "created_at", "updated_at"]

    def validate_country(self, value):
        value = (value or "").strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise serializers.ValidationError("Use an ISO 3166-1 alpha-2 code, e.g. US.")
        return value


class PartyRoleSerializer(MetadataCompatMixin, SafeSaveMixin, serializers.ModelSerializer):
    party = ScopedPartyField()
    valid_from = serializers.DateField(required=False)

    class Meta:
        model = PartyRole
        fields = [
            "id", "party", "company", "role", "valid_from", "valid_to", "source", "metadata",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "source", "created_at", "updated_at"]

    def validate_role(self, value):
        try:
            config = get_party_role(value)
        except KeyError:
            raise serializers.ValidationError(f"{value!r} is not a party role (GOLDEN_PARTY_ROLES).") from None
        if not config.staff_writable:
            raise serializers.ValidationError(
                f"The {value!r} role is system-managed (managed_by=system) and cannot be written here."
            )
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is not None:
            changed = sorted(k for k in attrs if k != "valid_to")
            if changed:
                raise serializers.ValidationError(
                    {k: "Only valid_to can change on a role; open a new role instead." for k in changed}
                )
            if not get_party_role(self.instance.role).staff_writable:
                raise serializers.ValidationError(
                    {"role": f"The {self.instance.role!r} role is system-managed and cannot be closed here."}
                )
            valid_from = self.instance.valid_from
            if "valid_to" in attrs and attrs["valid_to"] is None and self.instance.valid_to is not None and (
                PartyRole.objects.filter(party=self.instance.party, role=self.instance.role,
                                         valid_to__isnull=True).exclude(pk=self.instance.pk).exists()
            ):
                raise serializers.ValidationError({"valid_to": "That role is already open on this party."})
        else:
            attrs.setdefault("valid_from", timezone.localdate())
            valid_from = attrs["valid_from"]
            party, role = attrs["party"], attrs["role"]
            if party.kind not in get_party_role(role).kinds:
                raise serializers.ValidationError({"role": f"A {party.kind} party cannot hold the {role!r} role."})
            if attrs.get("valid_to") is None and PartyRole.objects.filter(
                party=party, role=role, valid_to__isnull=True,
            ).exists():
                raise serializers.ValidationError({"role": "This party already holds that role (open)."})
        valid_to = attrs.get("valid_to")
        if valid_to is not None and valid_to < valid_from:
            raise serializers.ValidationError({"valid_to": "valid_to cannot be before valid_from."})
        return attrs

    def create(self, validated_data):
        validated_data.setdefault("source", API_SOURCE)
        return super().create(validated_data)


class PartyRelationshipSerializer(MetadataCompatMixin, SafeSaveMixin, _OwnedMixin, serializers.ModelSerializer):
    from_party = ScopedPartyField()
    to_party = ScopedPartyField()

    IMMUTABLE = ("from_party", "to_party", "kind")

    class Meta:
        model = PartyRelationship
        fields = [
            "id", "company", "from_party", "to_party", "kind", "is_symmetric", "valid_from",
            "valid_to", "title", "is_primary", "notes", "source", "metadata", "mirrored_from",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "is_symmetric", "source", "created_at", "updated_at"]

    def owner_of(self, obj):
        return relationship_owner(obj)

    def validate_kind(self, value):
        try:
            get_relationship_kind(value)
        except KeyError:
            raise serializers.ValidationError(
                f"{value!r} is not a relationship kind (GOLDEN_RELATIONSHIP_KINDS)."
            ) from None
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is not None:
            owner = relationship_owner(self.instance)
            if owner is not None:
                raise serializers.ValidationError({"mirrored_from": owner_message(owner)})
            changed = [
                k for k in self.IMMUTABLE
                if k in attrs and getattr(attrs[k], "pk", attrs[k]) != getattr(
                    getattr(self.instance, k), "pk", getattr(self.instance, k))
            ]
            if changed:
                raise serializers.ValidationError(
                    {k: "Close this relationship and open a new one instead." for k in changed}
                )
            inst = self.instance
            if "valid_to" in attrs and attrs["valid_to"] is None and inst.valid_to is not None and (
                PartyRelationship.objects.filter(
                    from_party=inst.from_party, to_party=inst.to_party, kind=inst.kind, valid_to__isnull=True,
                ).exclude(pk=inst.pk).exists()
            ):
                raise serializers.ValidationError({"valid_to": "That relationship is already open."})
            return self._check_dates(attrs, inst)
        a, b, kind = attrs["from_party"], attrs["to_party"], attrs["kind"]
        config = get_relationship_kind(kind)
        if config.symmetric and b.pk < a.pk:
            # Stored once, in canonical order (1.3); callers never reason about it.
            attrs["from_party"], attrs["to_party"] = b, a
        probe = PartyRelationship(**{k: v for k, v in attrs.items() if k != "metadata"})
        try:
            probe.clean()
        except DjangoValidationError as exc:
            raise _django_errors(exc) from exc
        if attrs.get("valid_to") is None and PartyRelationship.objects.filter(
            from_party=attrs["from_party"], to_party=attrs["to_party"], kind=kind, valid_to__isnull=True,
        ).exists():
            raise serializers.ValidationError({"kind": "These parties already have that relationship (open)."})
        return self._check_dates(attrs, None)

    @staticmethod
    def _check_dates(attrs, instance):
        valid_from = attrs.get("valid_from", getattr(instance, "valid_from", None))
        valid_to = attrs.get("valid_to", getattr(instance, "valid_to", None))
        if valid_from and valid_to and valid_to < valid_from:
            raise serializers.ValidationError({"valid_to": "valid_to cannot be before valid_from."})
        return attrs

    def create(self, validated_data):
        validated_data.setdefault("source", API_SOURCE)
        return super().create(validated_data)


class ConsentRecordSerializer(SafeSaveMixin, serializers.ModelSerializer):
    """Append-only (1.6): create and read only; the view has no PATCH/DELETE."""

    party = ScopedPartyField()
    occurred_at = serializers.DateTimeField(required=False)
    source = serializers.ChoiceField(
        choices=[("api", "API"), ("erp", "ERP"), ("import", "Import")], default=API_SOURCE,
        help_text="portal, backfill and double_opt_in are written by golden itself, never through this API.",
    )

    class Meta:
        model = ConsentRecord
        fields = [
            "id", "party", "company", "channel", "purpose", "state", "address_normalized",
            "occurred_at", "recorded_at", "source", "source_detail", "evidence", "recorded_by",
        ]
        read_only_fields = ["id", "company", "recorded_at", "recorded_by"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        address = (attrs.get("address_normalized") or "").strip()
        channel = attrs.get("channel")
        if address and channel == "email":
            try:
                address = normalize_email(address)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"address_normalized": exc.messages}) from exc
        elif address and channel in ("sms", "phone"):
            _raw, e164, _ext = phone_parts(address, None)
            if not e164:
                raise serializers.ValidationError(
                    {"address_normalized": "Not a phone number that can be written in E.164."}
                )
            address = e164
        attrs["address_normalized"] = address
        attrs.setdefault("occurred_at", timezone.now())
        return attrs

    def create(self, validated_data):
        request = _request(self)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            validated_data["recorded_by"] = user
        return super().create(validated_data)


# --- Party ----------------------------------------------------------------------


class PartyListSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()

    class Meta:
        model = Party
        fields = [
            "id", "company", "kind", "display_name", "primary_email_normalized",
            "primary_phone_e164", "roles", "marketable", "is_active", "owner", "owner_team",
            "lifecycle_stage", "contact_type", "merged_into", "erased_at", "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_roles(self, obj):
        # ``open_roles`` is prefetched by the view; fall back for other callers.
        rows = getattr(obj, "open_roles", None)
        if rows is None:
            rows = obj.roles.filter(valid_to__isnull=True)
        return sorted(r.role for r in rows)


class PartySerializer(CustomFieldsMixin, SafeSaveMixin, serializers.ModelSerializer):
    """Detail, create and PATCH (5.2 ``parties/``)."""

    owner = ScopedOwnerField(required=False, allow_null=True)
    # Stamped from the owner's team when the owner changes (phase4-fields.md
    # 2.1); naming it directly needs crm.reassign_engagement (PartyViewSet).
    owner_team = ScopedTeamField(required=False, allow_null=True)
    # Create-only conveniences: the first email / phone, in the same transaction.
    email = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=254)
    phone = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=50)
    region = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=2)
    emails = PartyEmailSerializer(many=True, read_only=True)
    phones = PartyPhoneSerializer(many=True, read_only=True)
    addresses = PartyAddressSerializer(many=True, read_only=True)
    roles = PartyRoleSerializer(many=True, read_only=True)
    owned_by = serializers.SerializerMethodField()
    mirrored_fields = serializers.SerializerMethodField()

    class Meta:
        model = Party
        fields = [
            "id", "company", "kind", "display_name", "display_name_locked", "given_name",
            "family_name", "legal_name", "primary_domain", "registration_number", "tax_id",
            "user", "owner", "owner_team", "lifecycle_stage", "lifecycle_stage_changed_at",
            "contact_type", "is_self", "marketable", "marketable_changed_at",
            "marketable_source", "primary_email_normalized", "primary_phone_e164", "origin",
            "merged_into", "merged_at", "erased_at", "is_active", "metadata", "custom_fields", "tags",
            "email", "phone", "region", "emails", "phones", "addresses", "roles",
            "owned_by", "mirrored_fields", "created_at", "updated_at",
        ]
        # user: linked only by the sync, the portal resolver or a merge (2.3,
        # 2.7: same principle as identity spec 4). is_self: one per tenant,
        # set by golden. The rest are derived or belong to merge/erasure.
        read_only_fields = [
            "id", "company", "user", "is_self", "marketable_changed_at", "marketable_source",
            "primary_email_normalized", "primary_phone_e164", "origin", "merged_into",
            "merged_at", "erased_at", "created_at", "updated_at",
        ]
        extra_kwargs = {"display_name": {"required": False}}

    @extend_schema_field(OwnerRefSerializer(allow_null=True))
    def get_owned_by(self, obj):
        return party_owner(obj)

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_mirrored_fields(self, obj):
        return list(mirrored_party_fields(obj))

    def validate_email(self, value):
        if value:
            try:
                normalize_email(value)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(exc.messages) from exc
        return value.strip()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        if instance is None:
            kind = attrs.get("kind")
            if kind == PERSON and not (
                attrs.get("display_name") or attrs.get("given_name") or attrs.get("family_name")
            ):
                raise serializers.ValidationError({"display_name": "A person needs a name."})
            if kind == ORGANISATION and not attrs.get("display_name"):
                raise serializers.ValidationError({"display_name": "An organization needs a display name."})
        else:
            for field in ("email", "phone", "region"):
                if field in attrs:
                    raise serializers.ValidationError(
                        {field: "Only on create; use the emails/ and phones/ endpoints."}
                    )
            if "kind" in attrs and attrs["kind"] != instance.kind:
                raise serializers.ValidationError({"kind": "A party's kind cannot change after it is created."})
            mirrored = mirrored_party_fields(instance)
            refused = [f for f in mirrored if f in attrs and attrs[f] != getattr(instance, f)]
            if refused:
                message = owner_message(party_owner(instance))
                raise serializers.ValidationError({f: message for f in refused})
        owner = attrs.get("owner")
        company = getattr(instance, "company", None) or self.context.get("party_company")
        if owner is not None:
            if company is not None and owner.company_id != company.pk:
                raise serializers.ValidationError({"owner": "The owner must belong to the party's company."})
        team = attrs.get("owner_team")
        if team is not None and company is not None and team.company_id != company.pk:
            raise serializers.ValidationError({"owner_team": "The team must belong to the party's company."})
        kind = attrs.get("kind", getattr(instance, "kind", None))
        if kind == ORGANISATION and (
            attrs.get("given_name", getattr(instance, "given_name", ""))
            or attrs.get("family_name", getattr(instance, "family_name", ""))
        ):
            raise serializers.ValidationError("An organization has no given or family name.")
        return attrs

    def validate_lifecycle_stage(self, value):
        if value and value not in LIFECYCLE_STAGES:
            raise serializers.ValidationError(f"Unknown lifecycle stage {value!r} (GOLDEN_LIFECYCLE_STAGES).")
        return value

    def validate_contact_type(self, value):
        if value and value not in CONTACT_TYPES:
            raise serializers.ValidationError(f"Unknown contact type {value!r} (GOLDEN_CONTACT_TYPES).")
        return value

    def _marketable(self, validated_data, instance=None):
        if "marketable" in validated_data and (
            instance is None or validated_data["marketable"] != instance.marketable
        ):
            validated_data["marketable_changed_at"] = timezone.now()
            validated_data["marketable_source"] = API_SOURCE

    def create(self, validated_data):
        email = validated_data.pop("email", "")
        phone = validated_data.pop("phone", "")
        region = validated_data.pop("region", "")
        validated_data.setdefault("origin", Party.Origin.API)
        if validated_data.get("kind") == PERSON and not validated_data.get("display_name"):
            validated_data["display_name"] = ""
        self._marketable(validated_data)
        purpose = "work" if validated_data.get("kind") == ORGANISATION else "personal"
        with transaction.atomic():
            party = Party.objects.create(**validated_data)
            if email:
                PartyEmail.objects.create(
                    party=party, email=email, purpose=purpose, is_primary=True, source=API_SOURCE,
                )
            if phone:
                row = PartyPhone(party=party, number_raw=phone.strip(), purpose=purpose, source=API_SOURCE)
                row.phone_region = region.upper() if region else None
                row.is_primary = bool(phone_parts(phone, row.phone_region)[1])
                row.save()
            party.refresh_from_db()
        return party

    def update(self, instance, validated_data):
        self._marketable(validated_data, instance)
        return super().update(instance, validated_data)


# --- match envelope ---------------------------------------------------------------


class MatchQuerySerializer(serializers.Serializer):
    """``match/`` input: one exact key (``email`` or ``phone``) or free text ``q``."""

    q = serializers.CharField(required=False, allow_blank=True, max_length=255)
    email = serializers.CharField(required=False, allow_blank=True, max_length=254)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=50)
    region = serializers.CharField(required=False, allow_blank=True, max_length=2)
    target = serializers.ChoiceField(choices=TARGETS, default="party")
    company = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="Superusers and service keys only: narrow the lookup to one tenant.",
    )


class MatchCandidateSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    type = serializers.ChoiceField(choices=TARGETS)
    display_name = serializers.CharField()
    kind = serializers.CharField()
    company = serializers.UUIDField(allow_null=True)
    company_name = serializers.CharField()
    primary_email = serializers.CharField()
    primary_phone_last4 = serializers.CharField()
    roles = serializers.ListField(child=serializers.CharField())
    organisation = serializers.CharField()
    last_order_at = serializers.DateTimeField(allow_null=True)
    rule = serializers.CharField(help_text="R1 (email) / R3 (phone) from parties.dedupe, or 'search'.")
    confidence = serializers.FloatField(allow_null=True)


class MatchEnvelopeSerializer(serializers.Serializer):
    match_quality = serializers.ChoiceField(choices=["exact", "ambiguous", "fuzzy", "none"])
    item = MatchCandidateSerializer(allow_null=True)
    candidates = MatchCandidateSerializer(many=True)
    message = serializers.CharField()
