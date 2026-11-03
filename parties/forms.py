"""Back-office forms for parties (docs/crm/phase2-party.md 6, step 9).

Every form is bound to one tenant: the party it hangs off, or the company the
new party is created in. Lookups for "the other party" of a relationship and
the owner list never leave that tenant (3.1).

One writer per fact (P2): facts mirrored from a Customer, CustomerContact,
CustomerAddress or Lead are shown read-only. ``mirrored_party_fields`` lists
the Party columns the sync owns; the views refuse to edit mirrored contact
points, system roles and sync-written relationships outright.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, When
from django.utils import timezone

from core.sidecar_forms import DefinitionFieldsMixin
from core.staff import picker_profiles, profile_display
from core.teams import TeamChoiceField, team_choices, team_of

from .classification_registry import CONTACT_TYPES
from .constants import ORGANISATION, PERSON
from .display import contact_type_choices, lifecycle_choices
from .models import (
    Channel,
    ConsentRecord,
    ConsentSource,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
)
from .normalize import normalize_email, normalize_phone, person_display_name
from .relationship_registry import FROM_SIDE, RELATIONSHIP_KINDS, TO_SIDE
from .role_registry import PARTY_ROLES, get_party_role
from .widgets import PartySearchInput

# PartyEmail/PartyPhone.source prefixes written by parties.sync: the source
# row owns the value (P2), so the Party page shows it read-only.
MIRROR_SOURCE_PREFIXES = ("customer:", "contact:", "lead:")
# PartyRelationship.source values written by parties.sync / the backfill.
SYSTEM_RELATIONSHIP_SOURCES = frozenset({"customer-sync", "contact-sync", "staff-sync"})

DATE_WIDGET = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
DATETIME_WIDGET = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


def is_mirrored_point(row) -> bool:
    """A PartyEmail / PartyPhone written by the sync from a commerce or lead row."""
    return (row.source or "").startswith(MIRROR_SOURCE_PREFIXES)


def is_mirrored_address(address) -> bool:
    """A PartyAddress with linked CustomerAddresses (2.5) is read-only."""
    related = getattr(address, "customer_addresses", None)
    return bool(related is not None and related.exists())


def is_system_relationship(rel) -> bool:
    return rel.source in SYSTEM_RELATIONSHIP_SOURCES


def mirrored_party_fields(party) -> frozenset[str]:
    """Party columns a Customer or CustomerContact owns while it exists (2.1, 2.5)."""
    if party._state.adding:
        return frozenset()
    if party.kind == ORGANISATION:
        return frozenset({"display_name"}) if party.customer_accounts.exists() else frozenset()
    linked = (
        party.customer_accounts.exists()
        or party.held_accounts.exists()
        or party.customer_contacts.exists()
        or party.relationships_out.filter(
            source__in=("customer-sync", "contact-sync"), valid_to__isnull=True,
        ).exists()
    )
    return frozenset({"given_name", "family_name"}) if linked else frozenset()


def lock_party(party) -> None:
    """Serialise primary-flag changes on one party (``uniq_*_one_primary``).

    Two concurrent "make primary" saves would otherwise both pass the demote
    step and the second insert would hit the constraint. Call inside atomic().
    """
    list(Party.objects.select_for_update().filter(pk=party.pk).values_list("pk", flat=True))


def promote_primary(model, party, exclude_pk=None) -> None:
    """Give the party a primary row again when none is left (1.4).

    Emails prefer an active address; phones skip unparseable numbers (they
    are never matched, so never primary). Addresses are never auto-promoted.
    """
    if model is PartyAddress or model.objects.filter(party=party, is_primary=True).exists():
        return
    rows = model.objects.filter(party=party).exclude(pk=exclude_pk)
    if model is PartyPhone:
        rows = rows.exclude(number_e164="")
        order = ["created_at"]
    else:
        order = [Case(When(status=PartyEmail.Status.ACTIVE, then=0), default=1), "created_at"]
    first = rows.order_by(*order, "pk").first()
    if first is not None:
        first.is_primary = True
        first.save(update_fields=["is_primary"])


def save_with_primary(row, party, *, eligible=True, auto=True) -> None:
    """Save a contact point keeping exactly zero or one primary per party.

    ``eligible=False`` (an unparseable phone) forces the row non-primary.
    ``auto`` keeps the party with a primary row: when this row is not (or
    stops being) primary, another eligible row is promoted, and this row is
    made primary only when no other row can be.
    """
    model = type(row)
    with transaction.atomic():
        lock_party(party)
        others = model.objects.filter(party=party).exclude(pk=row.pk)
        if not eligible:
            row.is_primary = False
        if row.is_primary:
            # The queryset update skips post_save; row.save() below re-derives
            # the Party's denormalised primary columns.
            others.filter(is_primary=True).update(is_primary=False)
        elif auto and eligible and not others.filter(is_primary=True).exists():
            candidates = others.exclude(number_e164="") if model is PartyPhone else others
            row.is_primary = not candidates.exists()
        row.save()
        if auto and not row.is_primary:
            promote_primary(model, party, exclude_pk=row.pk)


class PartyForm(DefinitionFieldsMixin, forms.ModelForm):
    """Create or edit a party's identity, classification and record fields,
    then the deployment's party definitions as fieldsets (phase4-fields.md 5.2).

    Owners offered are the tenant's active people (plus the current owner,
    even when archived: 2.5). ``owner_team`` is offered only with
    ``crm.reassign_engagement`` (2.7); otherwise ``save()`` stamps it from the
    owner."""

    definition_object = "party"

    PERSON_ONLY = ("given_name", "family_name")
    ORGANISATION_ONLY = ("legal_name", "primary_domain", "registration_number", "tax_id")

    tags_text = forms.CharField(
        label="Tags", required=False, max_length=500,
        help_text="Comma-separated.",
    )

    class Meta:
        model = Party
        fields = [
            "kind", "given_name", "family_name", "display_name", "display_name_locked",
            "legal_name", "primary_domain", "registration_number", "tax_id",
            "lifecycle_stage", "contact_type", "owner", "owner_team", "marketable",
        ]
        labels = {
            "lifecycle_stage": "Lifecycle stage",
            "contact_type": "Contact type",
            "owner_team": "Owner team",
            "display_name": "Display name",
            "display_name_locked": "Keep this display name (do not derive it from the name parts)",
            "primary_domain": "Primary domain",
            "registration_number": "Registration number",
            "tax_id": "Tax id",
            "marketable": "Counts as a marketing contact (not consent)",
        }

    def __init__(self, *args, company_id=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company_id = company_id if company_id is not None else self.instance.company_id
        self.fields["display_name"].required = False
        self.fields["owner"].queryset = picker_profiles(self.company_id, keep=[self.instance.owner_id])
        self.fields["owner"].label_from_instance = profile_display
        self.fields["lifecycle_stage"].widget = forms.Select(choices=lifecycle_choices(self.company_id))
        self.fields["lifecycle_stage"].help_text = "Where the party is in the marketing funnel."
        if CONTACT_TYPES or self.instance.contact_type:
            self.fields["contact_type"].widget = forms.Select(choices=contact_type_choices(self.company_id))
        else:
            del self.fields["contact_type"]  # the deployment defines no contact types
        if user is not None and user.has_perm("crm.reassign_engagement"):
            self.fields["owner_team"] = TeamChoiceField(
                label="Owner team", required=False, empty_label="From the owner",
                queryset=team_choices(self.company_id, keep=self.instance.owner_team_id),
                help_text="Blank: taken from the owner's team when the owner changes.")
        else:
            del self.fields["owner_team"]
        self.fields["tags_text"].initial = ", ".join(str(t) for t in (self.instance.tags or []))
        self.mirrored = mirrored_party_fields(self.instance)
        if not self.instance._state.adding:
            # 1.1: kind is immutable after create; fields of the other kind go.
            del self.fields["kind"]
            other = self.ORGANISATION_ONLY if self.instance.kind == PERSON else self.PERSON_ONLY
            for name in other:
                del self.fields[name]
            for name in self.mirrored:
                if name in self.fields:
                    field = self.fields[name]
                    field.disabled = True
                    field.help_text = "Mirrored from the linked customer account; edit it there."
        self.add_definition_fields(self.instance, user=user, company_id=self.company_id)

    def clean_primary_domain(self):
        return (self.cleaned_data.get("primary_domain") or "").strip()

    def clean_tags_text(self):
        raw = self.cleaned_data.get("tags_text") or ""
        seen, tags = set(), []
        for tag in (part.strip() for part in raw.split(",")):
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                tags.append(tag[:50])
        return tags

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind") or self.instance.kind
        if kind == ORGANISATION:
            # The model's check constraint blanks person names on organisations.
            if cleaned.get("given_name") or cleaned.get("family_name"):
                self.add_error(None, "An organization has no given or family name.")
            cleaned["given_name"] = cleaned["family_name"] = ""
            if not (cleaned.get("display_name") or "").strip():
                self.add_error("display_name", "An organization needs a name.")
        elif kind == PERSON:
            for name in self.ORGANISATION_ONLY:
                cleaned[name] = ""
            derived = person_display_name(cleaned.get("given_name"), cleaned.get("family_name"))
            if not derived and not (cleaned.get("display_name") or "").strip():
                self.add_error("display_name", "Enter a name: given, family or display name.")
            elif not (cleaned.get("display_name") or "").strip():
                cleaned["display_name"] = derived
        self.clean_definition_fields()
        return cleaned

    def save(self, commit=True):
        party = super().save(commit=False)
        if "owner_team" in self.fields and self.add_prefix("owner_team") not in self.data:
            # Not posted (a scripted post): not a direct edit, so the owner stamp applies.
            party.owner_team_id = self.initial.get("owner_team")
        elif "owner_team" in self.fields and "owner_team" in self.changed_data \
                and self.cleaned_data.get("owner_team") is None:
            # Blank is "From the owner" (2.1): the owner's team, not none.
            party.owner_team_id = team_of(party.owner, self.company_id) if party.owner_id else None
        party.tags = self.cleaned_data.get("tags_text", [])
        if party._state.adding:
            party.company_id = self.company_id
            party.origin = Party.Origin.ERP
        if "marketable" in self.changed_data:
            party.marketable_changed_at = timezone.now()
            party.marketable_source = "erp"
        if commit:
            party.save()
        return party


class _PartyChildForm(forms.ModelForm):
    """A form for a row hanging off ``party``; sets it before model validation."""

    sets_party = True  # PartyRelationship has two ends instead of a ``party``

    def __init__(self, *args, party, **kwargs):
        super().__init__(*args, **kwargs)
        self.party = party
        if self.sets_party:
            self.instance.party = party
        self.instance.company_id = party.company_id


class PartyEmailForm(_PartyChildForm):
    class Meta:
        model = PartyEmail
        fields = ["email", "purpose", "is_primary", "status"]
        labels = {"is_primary": "Primary email"}

    def clean_email(self):
        value = self.cleaned_data.get("email", "")
        normalized = normalize_email(value)  # ValidationError -> field error
        dupes = PartyEmail.objects.filter(party=self.party, email_normalized=normalized)
        if not self.instance._state.adding:
            dupes = dupes.exclude(pk=self.instance.pk)
        if dupes.exists():
            raise ValidationError("This party already has that email address.")
        self.normalized = normalized
        return value.strip()

    def save(self, commit=True):
        row = super().save(commit=False)
        if not commit:
            return row
        save_with_primary(row, self.party)
        return row


class PartyPhoneForm(_PartyChildForm):
    class Meta:
        model = PartyPhone
        fields = ["number_raw", "extension", "line_type", "purpose", "is_primary"]
        labels = {"number_raw": "Number", "line_type": "Line type", "is_primary": "Primary phone"}

    def clean(self):
        cleaned = super().clean()
        raw = (cleaned.get("number_raw") or "").strip()
        if not raw:
            return cleaned
        e164, _ext = normalize_phone(raw)
        self.e164 = e164
        if not e164 and cleaned.get("is_primary"):
            self.add_error(
                "is_primary",
                "This number cannot be read as a full international number, so it cannot be primary.",
            )
        if e164:
            dupes = PartyPhone.objects.filter(party=self.party, number_e164=e164)
            if not self.instance._state.adding:
                dupes = dupes.exclude(pk=self.instance.pk)
            if dupes.exists():
                self.add_error("number_raw", "This party already has that number.")
        return cleaned

    def save(self, commit=True):
        row = super().save(commit=False)
        if not commit:
            return row
        save_with_primary(row, self.party, eligible=bool(getattr(self, "e164", "")))
        return row


class PartyAddressForm(_PartyChildForm):
    class Meta:
        model = PartyAddress
        fields = [
            "label", "purpose", "address_line_1", "address_line_2", "city",
            "state_province", "postal_code", "country", "is_primary", "valid_to",
        ]
        labels = {
            "address_line_1": "Address line 1",
            "address_line_2": "Address line 2",
            "state_province": "State / province",
            "country": "Country (2-letter code)",
            "is_primary": "Primary address",
            "valid_to": "Moved away on",
        }
        widgets = {"valid_to": DATE_WIDGET}

    def clean_country(self):
        value = (self.cleaned_data.get("country") or "").strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValidationError("Use the two-letter ISO country code, e.g. US or DE.")
        return value

    def save(self, commit=True):
        row = super().save(commit=False)
        if not commit:
            return row
        save_with_primary(row, self.party, auto=False)
        return row


class PartyRoleForm(_PartyChildForm):
    """Open or close a staff-writable role. System roles are never offered (1.2)."""

    role = forms.ChoiceField(label="Role")

    class Meta:
        model = PartyRole
        fields = ["role", "valid_from", "valid_to"]
        labels = {"valid_from": "From", "valid_to": "Until"}
        widgets = {"valid_from": DATE_WIDGET, "valid_to": DATE_WIDGET}

    def __init__(self, *args, role_labels=None, **kwargs):
        super().__init__(*args, **kwargs)
        labels = role_labels or {}
        choices = [
            (name, labels.get(name, config.label))
            for name, config in PARTY_ROLES.items()
            if config.staff_writable and self.party.kind in config.kinds
        ]
        self.fields["role"].choices = choices
        if not self.instance._state.adding:
            self.fields["role"].disabled = True
            self.fields["role"].choices = [
                (self.instance.role, labels.get(self.instance.role, self.instance.role)),
            ]
        else:
            self.fields["valid_from"].initial = timezone.localdate()

    def clean_role(self):
        role = self.cleaned_data.get("role")
        try:
            config = get_party_role(role)
        except KeyError:
            raise ValidationError("Unknown role.") from None
        if not config.staff_writable:
            raise ValidationError("This role is managed by the system and cannot be set here.")
        return role

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        if role and cleaned.get("valid_to") is None:
            clash = PartyRole.objects.filter(party=self.party, role=role, valid_to__isnull=True)
            if not self.instance._state.adding:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error("role", "This party already holds that role.")
        start, end = cleaned.get("valid_from"), cleaned.get("valid_to")
        if start and end and end < start:
            self.add_error("valid_to", "The end date is before the start date.")
        return cleaned


def relationship_side_choices(party_kind: str) -> list[tuple[str, str]]:
    """``(kind:side, label)`` choices read from this party's side.

    ``employment:from`` reads "works at" (this party is the employee);
    ``employment:to`` reads "employs". A symmetric kind is offered once.
    Only directions this party's kind may take are offered.
    """
    choices = []
    for name, config in RELATIONSHIP_KINDS.items():
        if party_kind in config.from_kinds:
            choices.append((f"{name}:{FROM_SIDE}", config.label_for(FROM_SIDE)))
        if not config.symmetric and party_kind in config.to_kinds:
            choices.append((f"{name}:{TO_SIDE}", config.label_for(TO_SIDE)))
    return choices


class PartyRelationshipForm(_PartyChildForm):
    """Link this party to another one of the same tenant.

    The other party is picked with the party search (``PartySearchInput``:
    email or name, this tenant only, this party left out), which submits its
    id; without JavaScript an id or an exact email address also works. The
    id is re-checked against the tenant on POST (``_resolve_other``). An email
    matching several parties is refused rather than guessed (P9).
    """

    sets_party = False

    kind_side = forms.ChoiceField(label="Relationship")
    other_party = forms.CharField(
        label="Other party", max_length=254,
        help_text="Search by email or name and pick the party from the list.",
    )

    class Meta:
        model = PartyRelationship
        fields = ["valid_from", "valid_to", "title", "is_primary", "notes"]
        labels = {
            "valid_from": "From", "valid_to": "Until", "title": "Job title",
            "is_primary": "Primary (employer / contact)",
        }
        widgets = {
            "valid_from": DATE_WIDGET, "valid_to": DATE_WIDGET,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance._state.adding:
            # The ends and the kind of a stored relationship are fixed; end it
            # (Until) and record a new one instead.
            del self.fields["kind_side"]
            del self.fields["other_party"]
        else:
            self.fields["kind_side"].choices = relationship_side_choices(self.party.kind)
            self.fields["other_party"].widget = PartySearchInput(
                company_id=self.party.company_id, exclude_id=self.party.pk,
                empty_text="No match. Add the party first, then search again.",
            )

    def _post_clean(self):
        # Model validation needs both ends and the kind, which clean() sets
        # only once the form's own checks pass; an invalid form stops here.
        if self._errors or (self.instance._state.adding and not self.instance.kind):
            return
        super()._post_clean()

    def _resolve_other(self, text):
        text = text.strip()
        live = Party.objects.filter(
            company_id=self.party.company_id, merged_into__isnull=True, erased_at__isnull=True,
        )
        try:
            pk = uuid.UUID(text)
        except ValueError:
            pk = None
        if pk is not None:
            other = live.filter(pk=pk).first()
            if other is None:
                raise ValidationError("No party with that id.")
            return other
        try:
            email = normalize_email(text)
        except ValidationError:
            raise ValidationError("Enter a party id or an email address.") from None
        matches = list(live.filter(emails__email_normalized=email).distinct()[:3])
        if not matches:
            raise ValidationError("No party holds that email address.")
        if len(matches) > 1:
            raise ValidationError(
                "Several parties hold that email address; enter the id of the one you mean."
            )
        return matches[0]

    def clean(self):
        cleaned = super().clean()
        if not self.instance._state.adding:
            start, end = cleaned.get("valid_from"), cleaned.get("valid_to")
            if start and end and end < start:
                self.add_error("valid_to", "The end date is before the start date.")
            return cleaned
        kind_side, other_text = cleaned.get("kind_side"), cleaned.get("other_party")
        if not kind_side or not other_text:
            return cleaned
        try:
            other = self._resolve_other(other_text)
        except ValidationError as exc:
            self.add_error("other_party", exc)
            return cleaned
        if other.pk == self.party.pk:
            self.add_error("other_party", "A party cannot be related to itself.")
            return cleaned
        kind, _, side = kind_side.partition(":")
        config = RELATIONSHIP_KINDS.get(kind)
        if config is None:
            self.add_error("kind_side", "Unknown relationship.")
            return cleaned
        from_party, to_party = (self.party, other) if side == FROM_SIDE else (other, self.party)
        if config.symmetric and from_party.pk > to_party.pk:
            # Stored once, in canonical order (partyrel_symmetric_canonical).
            from_party, to_party = to_party, from_party
        if not config.allows(from_party.kind, to_party.kind):
            self.add_error(
                "kind_side",
                f"'{config.label_for(side)}' cannot link a {self.party.kind} to a {other.kind}.",
            )
            return cleaned
        if cleaned.get("valid_to") is None and PartyRelationship.objects.filter(
            from_party=from_party, to_party=to_party, kind=kind, valid_to__isnull=True,
        ).exists():
            self.add_error("other_party", "This relationship is already recorded.")
            return cleaned
        self.instance.kind = kind
        self.instance.from_party = from_party
        self.instance.to_party = to_party
        self.instance.source = "erp"
        return cleaned


    def save(self, commit=True):
        rel = super().save(commit=False)
        if not commit:
            return rel
        with transaction.atomic():
            lock_party(rel.from_party)
            rel.save()
            if rel.is_primary and rel.valid_to is None:
                # One primary employer / contact per person and kind. Rows the
                # sync wrote are left alone: their writer owns them (P2).
                PartyRelationship.objects.filter(
                    from_party_id=rel.from_party_id, kind=rel.kind, is_primary=True, valid_to__isnull=True,
                ).exclude(pk=rel.pk).exclude(source__in=SYSTEM_RELATIONSHIP_SOURCES).update(is_primary=False)
        return rel


class ConsentRecordForm(_PartyChildForm):
    """Append one consent record (1.6). Records are never edited."""

    address = forms.CharField(
        label="Address", required=False, max_length=254,
        help_text="The email address or phone number this applies to. "
                  "Leave blank for every address of this party on the channel.",
    )

    class Meta:
        model = ConsentRecord
        fields = ["channel", "purpose", "state", "occurred_at", "source_detail"]
        labels = {
            "occurred_at": "When the person decided",
            "source_detail": "Source detail",
        }
        help_texts = {"source_detail": "Where this came from, e.g. a phone call or a signed form."}
        widgets = {"occurred_at": DATETIME_WIDGET}

    def __init__(self, *args, recorded_by=None, evidence=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.recorded_by = recorded_by
        self.evidence = evidence or {}
        self.fields["occurred_at"].initial = timezone.localtime().replace(second=0, microsecond=0)
        self.fields["occurred_at"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]

    def clean(self):
        cleaned = super().clean()
        address = (cleaned.get("address") or "").strip()
        channel = cleaned.get("channel")
        if address and channel:
            if channel == Channel.EMAIL:
                try:
                    address = normalize_email(address)
                except ValidationError as exc:
                    self.add_error("address", exc)
            elif channel in (Channel.SMS, Channel.PHONE):
                e164, _ext = normalize_phone(address)
                if not e164:
                    self.add_error("address", "Enter a full phone number, including the area code.")
                address = e164
            else:
                address = " ".join(address.split()).casefold()
        occurred = cleaned.get("occurred_at")
        if occurred and occurred > timezone.now() + timedelta(minutes=5):
            self.add_error("occurred_at", "A consent decision cannot be in the future.")
        cleaned["address"] = address
        return cleaned

    def save(self, commit=True):
        record = super().save(commit=False)
        record.address_normalized = self.cleaned_data.get("address", "")
        record.source = ConsentSource.ERP
        record.recorded_by = self.recorded_by
        record.evidence = self.evidence
        if commit:
            record.save()
        return record
