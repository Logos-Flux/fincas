"""Server-rendered back office for parties (docs/crm/phase2-party.md 6.1, 6.2).

``/erp/`` is staff-only (``core.middleware.StaffOnlyERPMiddleware``). Every
queryset here starts from ``for_request``, so a staff user only ever reaches
their own tenant's parties; another tenant's id is a 404 (3.1). Superusers
are cross-tenant, the same rule as the staff API.

The merge review screen (6.3) is step 6's (``parties.merge_views``). The
erasure danger zone (step 12) posts to ``PartyEraseView``.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from collections import OrderedDict

import phonenumbers
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Prefetch, Q, Subquery, Value, When
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, UpdateView

from core import sidecar_forms
from core.http import safe_next as _safe_next
from core.labels import get_label, labels_for_request
from core.list_columns import Column
from core.models import Company, ExternalRef, Team
from core.staff import filter_entry, owner_filter_choices
from core.teams import team_display
from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin

from .classification_registry import CONTACT_TYPES, LIFECYCLE_STAGES
from .constants import ORGANISATION, PERSON
from .display import contact_type_choices, contact_type_label, lifecycle_choices, lifecycle_label
from .erasure import ErasureError, erase_party, erasure_summary
from .forms import (
    ConsentRecordForm,
    PartyAddressForm,
    PartyEmailForm,
    PartyForm,
    PartyPhoneForm,
    PartyRelationshipForm,
    PartyRoleForm,
    is_mirrored_point,
    is_system_relationship,
    lock_party,
    mirrored_party_fields,
    promote_primary,
)
from .models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
    Suppression,
)
from .normalize import default_phone_region, normalize_email, normalize_phone
from .relationship_registry import EMPLOYMENT, FROM_SIDE, RELATIONSHIP_KINDS, TO_SIDE
from .role_registry import PARTY_ROLES
from .widgets import party_choice_label

SECTION = "parties"
ROLE_BADGE_LIMIT = 3
OPEN_QUOTE_STATUSES = ("draft", "pending_approval", "approved", "sent", "viewed", "negotiating")
CONSENT_STATES = (("granted", "Granted"), ("withdrawn", "Withdrawn"), ("unknown", "Unknown"))
YES_NO = (("yes", "Yes"), ("no", "No"))
CONSENT_HISTORY_LIMIT = 100
PARTY_TASK_LIMIT = 10
PARTY_ENGAGEMENT_LIMIT = 20
PARTY_TIMELINE_LIMIT = 20
PARTY_SEARCH_LIMIT = 15
PARTY_SEARCH_MIN = 2
PARTY_SEARCH_WORDS = 5  # name words matched; the rest of a long query is ignored


# --- helpers -----------------------------------------------------------------------


def scoped_parties(request):
    """The requester's tenant's parties (superusers: every tenant)."""
    return Party.objects.for_request(request)


def request_company_id(request):
    """The tenant a new party is created in.

    The user's own company; for a superuser without one, the only Company if
    exactly one exists. ``None`` means "cannot tell" and the create view
    refuses rather than guessing a tenant.
    """
    profile = getattr(request.user, "userprofile", None)
    if profile is not None and profile.company_id:
        return profile.company_id
    if request.user.is_superuser:
        ids = list(Company.objects.values_list("pk", flat=True)[:2])
        if len(ids) == 1:
            return ids[0]
    return None


def role_label(name, company_id=None) -> str:
    """A party role's staff-UI noun (label layer, ``party_role.<name>``)."""
    try:
        return get_label(f"party_role.{name}", company=company_id).singular
    except (KeyError, ValueError):
        config = PARTY_ROLES.get(name)
        return config.label if config else name


def format_phone(e164: str) -> str:
    """National format for numbers in the default region, international otherwise."""
    if not e164:
        return ""
    try:
        number = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return e164
    region = phonenumbers.region_code_for_number(number)
    fmt = (
        phonenumbers.PhoneNumberFormat.NATIONAL
        if region == default_phone_region()
        else phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )
    return phonenumbers.format_number(number, fmt)


def mask_address(value: str) -> str:
    """``j***@example.com`` / ``***0143``: never the full address on screen."""
    if "@" in value:
        local, _, domain = value.rpartition("@")
        return f"{local[:1]}***@{domain}"
    return f"***{value[-4:]}" if len(value) > 4 else "***"


def address_hash(value: str) -> str:
    """``Suppression.address_hash``: sha256 of the normalised address (1.6)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _looks_like_phone(text: str) -> bool:
    compact = [c for c in text if not c.isspace()]
    digits = sum(c.isdigit() for c in compact)
    return digits >= 4 and digits >= 0.6 * len(compact)


# --- 6.1 list ----------------------------------------------------------------------


def lifecycle_rank():
    """``GOLDEN_LIFECYCLE_STAGES`` order as a sortable expression; unknown last."""
    whens = [When(lifecycle_stage=name, then=Value(i)) for i, name in enumerate(LIFECYCLE_STAGES)]
    return Case(*whens, default=Value(len(whens)), output_field=IntegerField())


def _latest_email_consent():
    return ConsentRecord.objects.filter(
        party=OuterRef("pk"), channel="email", purpose="marketing",
    ).order_by("-occurred_at", "-recorded_at").values("state")[:1]



class PartyListView(ERPListView):
    """``/erp/parties/``: one query for the rows, one each for roles and employers.

    The reference list for the column registry (docs/crm/phase4-fields.md
    5.1): its columns, the party definition columns and plugin columns render
    through ``erp/parties/party_list.html``; the export is the canonical CRM
    export (5.5). Step 6 changes the defaults (lifecycle, contact type)."""

    model = Party
    template_name = "erp/parties/party_list.html"
    current_section = SECTION
    page_title = "Parties"
    entity_name = "Party"
    entity_label_key = "parties.party"
    page_title_format = "{plural}"
    create_url_name = "erp-party-create"
    paginate_by = 50
    ordering = ["display_name", "pk"]
    list_id = "parties.party"
    definition_object = "party"
    csv_canonical = True
    sortable_fields = {
        "display_name": ("display_name",),
        "email": ("primary_email_normalized",),
        "primary_email_normalized": ("primary_email_normalized",),  # step-0 bookmarks
        "kind": ("kind",),
        "updated_at": ("updated_at",),
        "created_at": ("created_at",),
        # Step 6 (phase4-fields.md 5.1): the funnel order, not the key's spelling.
        "lifecycle_stage": ("lifecycle_rank",),
        "contact_type": ("contact_type",),
        "owner": ("owner__display_name",),
        "owner_team": ("owner_team__name",),
    }
    # 5.1 defaults (7): Name, Roles, Lifecycle, Email, Phone, Owner, Updated.
    list_columns = [
        Column("display_name", "Name", path="display_name", sortable=True, priority=1),
        Column("kind", "Kind", path="kind", sortable=True, priority=3, default=False),
        Column("roles", "Roles", priority=2),
        Column("lifecycle_stage", "Lifecycle", path="lifecycle_stage", sortable=True, priority=2),
        Column("email", "Email", path="primary_email_normalized", sortable=True, priority=1, pii=True),
        Column("phone", "Phone", path="primary_phone_e164", priority=3, pii=True),
        Column("owner", "Owner", sortable=True, priority=2),
        Column("updated_at", "Updated", path="updated_at", sortable=True, priority=3),
        Column("contact_type", "Contact type", path="contact_type", sortable=True, priority=2, default=False),
        Column("organisation", "Organization", priority=3, default=False),
        Column("owner_team", "Team", sortable=True, priority=3, default=False),
        Column("created_at", "Created", path="created_at", sortable=True, priority=3, default=False),
        Column("origin", "Origin", path="origin", priority=3, default=False),
        Column("marketable", "Marketable", path="marketable", priority=3, default=False, format="boolean"),
        Column("email_consent", "Email consent", priority=3, default=False),
        Column("id", "ID", path="id", export_only=True),
    ]
    filter_params = {
        "q": None, "kind": (PERSON, ORGANISATION), "role": None, "owner": None,
        "lifecycle_stage": None, "contact_type": None, "owner_team": None,
        "customer": ("yes", "no"), "marketable": ("yes", "no"),
        "consent": ("granted", "withdrawn", "unknown"), "show": ("all",),
    }

    def export_value(self, obj, col):
        if col.id == "roles":
            return [r.role for r in obj.open_roles]
        if col.id == "organisation":
            employer = obj.current_employment[0] if obj.current_employment else None
            return employer.to_party.display_name if employer and obj.kind == PERSON else ""
        if col.id == "owner":
            owner = obj.owner
            if owner is None:
                return ""
            return owner.display_name or (owner.user.get_username() if owner.user_id else "")
        if col.id == "owner_team":
            return obj.owner_team.key if obj.owner_team_id else ""
        if col.id == "email_consent":
            return getattr(obj, "email_marketing", None) or ""
        return super().export_value(obj, col)

    def with_columns(self, qs, columns):
        """The Email consent column reads the latest email-marketing record
        in SQL, only when it is visible (or the consent filter already did)."""
        qs = super().with_columns(qs, columns)
        if any(c.id == "email_consent" for c in columns) and "email_marketing" not in qs.query.annotations:
            qs = qs.annotate(email_marketing=Subquery(_latest_email_consent()))
        return qs

    def get_queryset(self):
        params = self.request.GET
        qs = scoped_parties(self.request).select_related("owner__user")
        qs = qs.select_related("owner_team").annotate(lifecycle_rank=lifecycle_rank())
        if params.get("show") != "all":
            qs = qs.filter(merged_into__isnull=True, erased_at__isnull=True)

        kind = params.get("kind")
        if kind in (PERSON, ORGANISATION):
            qs = qs.filter(kind=kind)
        role = params.get("role")
        if role:
            qs = qs.filter(Exists(PartyRole.objects.filter(
                party=OuterRef("pk"), role=role, valid_to__isnull=True,
            )))
        owner = params.get("owner")
        if owner:
            try:
                qs = qs.filter(owner_id=owner)
            except ValidationError:
                qs = qs.none()
        for name in ("lifecycle_stage", "contact_type"):
            value = params.get(name)
            if value == "none":
                qs = qs.filter(**{name: ""})
            elif value:
                qs = qs.filter(**{name: value})
        team = params.get("owner_team")
        if team == "none":
            qs = qs.filter(owner_team__isnull=True)
        elif team:
            try:
                qs = qs.filter(owner_team_id=team)
            except ValidationError:
                qs = qs.none()
        customer = params.get("customer")
        if customer in ("yes", "no"):
            from customers.models import Customer

            has = Exists(Customer.objects.filter(
                Q(party=OuterRef("pk")) | Q(account_holder=OuterRef("pk")),
            ))
            qs = qs.filter(has if customer == "yes" else ~has)
        marketable = params.get("marketable")
        if marketable in ("yes", "no"):
            qs = qs.filter(marketable=marketable == "yes")
        consent = params.get("consent")
        if consent in ("granted", "withdrawn", "unknown"):
            qs = qs.annotate(email_marketing=Subquery(_latest_email_consent()))
            qs = qs.filter(email_marketing__isnull=True) if consent == "unknown" else qs.filter(
                email_marketing=consent,
            )

        qs = self._search(qs, params.get("q", "").strip())

        qs = self.apply_sort(qs.order_by(*self.ordering))

        return qs.prefetch_related(
            Prefetch(
                "roles",
                queryset=PartyRole.objects.filter(valid_to__isnull=True).defer("metadata").order_by("role"),
                to_attr="open_roles",
            ),
            Prefetch(
                "relationships_out",
                queryset=PartyRelationship.objects.filter(kind=EMPLOYMENT, valid_to__isnull=True)
                .select_related("to_party").defer("metadata", "to_party__metadata")
                .order_by("-is_primary", "created_at"),
                to_attr="current_employment",
            ),
        )

    @staticmethod
    def _search(qs, text):
        """6.1: name prefix, or an exact normalised email / phone."""
        if not text:
            return qs
        if "@" in text:
            try:
                email = normalize_email(text)
            except ValidationError:
                return qs.none()
            return qs.filter(Q(primary_email_normalized=email) | Exists(
                PartyEmail.objects.filter(party=OuterRef("pk"), email_normalized=email),
            ))
        if _looks_like_phone(text):
            e164, _ext = normalize_phone(text)
            if not e164:
                return qs.none()
            return qs.filter(Q(primary_phone_e164=e164) | Exists(
                PartyPhone.objects.filter(party=OuterRef("pk"), number_e164=e164),
            ))
        return qs.filter(
            Q(display_name__istartswith=text) | Q(family_name__istartswith=text)
            | Q(legal_name__istartswith=text),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company_id = labels_for_request(self.request).company_id
        for party in context["object_list"]:
            party.lifecycle_text = lifecycle_label(party.lifecycle_stage, party.company_id)
            party.contact_type_text = contact_type_label(party.contact_type, party.company_id)
            roles = [(r.role, role_label(r.role, party.company_id)) for r in party.open_roles]
            party.role_badges = roles[:ROLE_BADGE_LIMIT]
            party.more_roles = max(0, len(roles) - ROLE_BADGE_LIMIT)
            employer = party.current_employment[0] if party.current_employment else None
            party.organisation = employer.to_party if employer and party.kind == PERSON else None
            party.phone_display = format_phone(party.primary_phone_e164)

        params = self.request.GET
        role_choices = [(name, role_label(name, company_id)) for name in PARTY_ROLES]
        tenant = request_company_id(self.request)
        owner_choices = owner_filter_choices(tenant)
        team_choices = [("none", "No team")] + [
            (str(t.pk), team_display(t)) for t in Team.objects.filter(company_id=tenant).order_by("sort_order", "name")
        ] if tenant else []
        context["party_filters"] = [
            {"field": "kind", "label": "Kind", "choices": list(Party.Kind.choices)},
            {"field": "role", "label": "Role", "choices": role_choices},
            filter_entry("owner", "Owner", owner_choices),
            {"field": "lifecycle_stage", "label": "Lifecycle",
             "choices": [("none", "Unknown")] + lifecycle_choices(company_id)[1:]},
            *([{"field": "contact_type", "label": "Contact type",
                "choices": [("none", "Not set")] + contact_type_choices(company_id)[1:]}] if CONTACT_TYPES else []),
            {"field": "owner_team", "label": "Team", "choices": team_choices},
            {"field": "customer", "label": "Customer account", "choices": list(YES_NO)},
            {"field": "marketable", "label": "Marketable", "choices": list(YES_NO)},
            {"field": "consent", "label": "Email marketing", "choices": list(CONSENT_STATES)},
        ]
        keys = [f["field"] for f in context["party_filters"]] + ["show"]
        context["active_filters"] = {k: params.get(k, "") for k in keys}
        context["active_filter_count"] = sum(1 for k in keys if k != "show" and params.get(k))
        context.update(self.get_list_status(
            context, context["party_filters"], context["active_filters"],
            context["current_sort"], context["current_sort_dir"],
        ))
        context["show_all"] = params.get("show") == "all"
        kept = params.copy()
        kept.pop("page", None)
        context["page_query"] = kept.urlencode()
        for name in ("sort", "dir"):
            kept.pop(name, None)
        context["filter_query"] = kept.urlencode()
        toggled = params.copy()
        toggled.pop("page", None)
        if context["show_all"]:
            toggled.pop("show", None)
        else:
            toggled["show"] = "all"
        context["show_toggle_query"] = toggled.urlencode()
        return context


def party_relationships(party):
    """``(current_groups, past)``: the party's relationships read from its side.

    ``current_groups`` is ``[(side label, [rel, ...]), ...]`` for the open
    ones; each row carries ``side_label``, ``other`` (the far end) and
    ``system`` (written by the sync, so not editable here). Shared by the
    party page and the customer profile's Relationships card.
    """
    rels = list(
        PartyRelationship.objects.filter(Q(from_party=party) | Q(to_party=party))
        .select_related("from_party", "to_party")
        .order_by("kind", "-is_primary", "-valid_from")
    )
    current, past = OrderedDict(), []
    for rel in rels:
        side = FROM_SIDE if rel.from_party_id == party.pk else TO_SIDE
        config = RELATIONSHIP_KINDS.get(rel.kind)
        rel.side_label = config.label_for(side) if config else rel.kind
        rel.other = rel.to_party if side == FROM_SIDE else rel.from_party
        rel.system = is_system_relationship(rel)
        if rel.valid_to is None:
            current.setdefault(rel.side_label, []).append(rel)
        else:
            past.append(rel)
    return list(current.items()), past


# --- picker search (the party fields of the CRM forms) ------------------------------


class PartySearchView(ERPViewMixin, View):
    """``GET /erp/parties/search/?q=``: live parties for ``parties.widgets.PartySearchInput``.

    Matches any email the party holds, primary or not (``email_normalized``:
    exact, then prefix), or its name: every word of the query must appear in
    the display, given, family or legal name, so "lovelace ada" finds Ada
    Lovelace. Inside one tenant: ``company`` when given and the viewer may
    read it (``for_request``: staff only ever reach their own), else the
    tenant the viewer creates rows in. Merged and erased parties never match.
    ``kind`` narrows to people or organisations; ``exclude`` leaves one party
    out (the party a relationship is being added to). Exact email matches
    come first; each result carries the id the form submits.
    """

    def get(self, request):
        text = (request.GET.get("q") or "").strip()[:254]
        if len(text) < PARTY_SEARCH_MIN:
            return JsonResponse({"results": []})
        company_id = request.GET.get("company") or request_company_id(request)
        try:
            company_id = uuid.UUID(str(company_id)) if company_id else None
        except ValueError:
            company_id = None
        if company_id is None:
            return JsonResponse({"results": []})
        qs = scoped_parties(request).filter(company_id=company_id, merged_into__isnull=True,
                                            erased_at__isnull=True)
        kind = request.GET.get("kind")
        if kind in (PERSON, ORGANISATION):
            qs = qs.filter(kind=kind)
        try:
            qs = qs.exclude(pk=uuid.UUID(request.GET.get("exclude") or ""))
        except ValueError:
            pass
        # The prefix in email_normalized's form (NFKC + casefold). A partial
        # domain is not IDNA-encoded, which only matters for non-ASCII domains.
        prefix = unicodedata.normalize("NFKC", text).casefold()
        try:
            exact = normalize_email(text)
        except ValidationError:
            exact = None
        by_prefix = PartyEmail.objects.filter(party=OuterRef("pk"), email_normalized__startswith=prefix)
        by_name = Q()
        for word in text.split()[:PARTY_SEARCH_WORDS]:
            by_name &= (Q(display_name__icontains=word) | Q(given_name__icontains=word)
                        | Q(family_name__icontains=word) | Q(legal_name__icontains=word))
        qs = qs.filter(Q(Exists(by_prefix)) | Q(display_name__icontains=text) | by_name).annotate(
            exact=Exists(PartyEmail.objects.filter(party=OuterRef("pk"), email_normalized=exact))
            if exact else Value(False),
            matched_email=Subquery(by_prefix.order_by("-is_primary", "email_normalized")
                                   .values("email_normalized")[:1]),
        ).only("display_name", "kind", "primary_email_normalized")
        results = []
        for party in qs.order_by("-exact", "display_name", "pk")[:PARTY_SEARCH_LIMIT]:
            results.append({
                "id": str(party.pk),
                "label": party_choice_label(party),
                "name": party.display_name,
                "kind": party.kind,
                "kind_label": party.get_kind_display(),
                "email": party.matched_email or party.primary_email_normalized,
            })
        return JsonResponse({"results": results})


# --- 6.2 detail --------------------------------------------------------------------


class PartyDetailView(ERPViewMixin, DetailView):
    model = Party
    template_name = "erp/parties/party_detail.html"
    context_object_name = "party"
    current_section = SECTION
    entity_label_key = "parties.party"

    def get_queryset(self):
        return scoped_parties(self.request).select_related(
            "owner__user", "owner_team", "user", "merged_into",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        party = self.object
        company_id = party.company_id  # the party's tenant's wording, also for superusers
        context["page_title"] = party.display_name
        context["editable"] = not party.merged_into_id and not party.erased_at

        # Header: roles, primary contact, consent chips.
        roles = list(party.roles.order_by("-valid_from", "role"))
        for role in roles:
            role.label = role_label(role.role, company_id)
            config = PARTY_ROLES.get(role.role)
            role.editable = bool(config and config.staff_writable)
        context["open_roles"] = [r for r in roles if r.valid_to is None]
        context["past_roles"] = [r for r in roles if r.valid_to is not None]
        context["can_add_role"] = any(
            c.staff_writable and party.kind in c.kinds for c in PARTY_ROLES.values()
        )

        # 1. Contact points: primary first, then by purpose.
        emails = list(party.emails.order_by("-is_primary", "purpose", "email_normalized"))
        phones = list(party.phones.order_by("-is_primary", "purpose", "number_raw"))
        addresses = list(
            party.addresses.prefetch_related("customer_addresses__customer")
            .order_by("-is_primary", "purpose", "label")
        )
        mirror_customers = self._mirror_sources(party, emails + phones)
        for row in emails + phones:
            row.mirrored = is_mirrored_point(row)
            row.mirror_customer = mirror_customers.get(row.source)
        for row in phones:
            row.display = format_phone(row.number_e164) or row.number_raw
        for address in addresses:
            linked = list(address.customer_addresses.all())
            address.mirrored = bool(linked)
            address.mirror_customer = linked[0].customer if linked else None
        context.update(emails=emails, phones=phones, addresses=addresses)
        primary_email = next((e for e in emails if e.is_primary), None)
        primary_phone = next((p for p in phones if p.is_primary), None)
        context["primary_email"] = primary_email
        context["primary_phone"] = primary_phone

        # 2. Next actions and engagements (phase4-fields.md 5.2).
        context.update(self._crm(party))

        # 3. Profile: lifecycle, contact type, then the definition groups.
        context["lifecycle_label"] = lifecycle_label(party.lifecycle_stage, company_id)
        context["contact_type_label"] = contact_type_label(party.contact_type, company_id)
        context["definition_groups"] = sidecar_forms.definition_groups(party, self.request.user)
        context["other_data"] = sidecar_forms.other_data(party, self.request.user)

        # 4. Relationships, read from this party's side.
        context["relationship_groups"], context["past_relationships"] = party_relationships(party)

        # 5. Commerce, and the leads that came in from this party.
        context.update(self._commerce(party))
        context["leads"] = list(party.leads.order_by("-created_at")[:20])

        # 6. Timeline (party scope): the latest communications with the party.
        context["party_timeline"] = self._timeline(party)

        # 7. Consent: latest per channel and purpose, history, suppressions.
        consents = list(
            party.consents.select_related("recorded_by").order_by("-occurred_at", "-recorded_at")[:CONSENT_HISTORY_LIMIT]
        )
        current_consent = OrderedDict()
        for record in consents:
            current_consent.setdefault((record.channel, record.purpose), record)
        context["consent_current"] = list(current_consent.values())
        context["consent_history"] = consents
        email_marketing = current_consent.get(("email", "marketing"))
        context["email_marketing_state"] = email_marketing.state if email_marketing else "unknown"
        context["suppressions"] = self._suppressions(party, emails, phones)

        # Side column.
        context["access"] = self._access(party)
        context["identifiers"] = self._identifiers(party, context["commerce_accounts"])
        context["mirrored_fields"] = mirrored_party_fields(party)
        context["can_erase"] = can_erase(self.request.user, party)
        return context

    def _crm(self, party):
        """Open tasks on the party and on its engagements, soonest first; the
        engagements it is the counterparty or a contact of, open first."""
        from crm.erp_views import task_kind_labels
        from crm.models import OPEN_TASK_STATUSES, Engagement, Task

        now = timezone.now()
        tasks = list(
            Task.objects.filter(company_id=party.company_id, status__in=OPEN_TASK_STATUSES)
            .filter(Q(party=party) | Q(engagement__party=party))
            .select_related("engagement", "assignee__user")
            .order_by(F("due_at").asc(nulls_last=True), "created_at", "pk")[:PARTY_TASK_LIMIT]
        )
        labels = task_kind_labels(party.company_id)
        for task in tasks:
            task.kind_label = labels.get(task.kind, task.kind)
            task.overdue = task.due_at is not None and task.due_at < now
        engagements = list(
            Engagement.objects.filter(company_id=party.company_id)
            .filter(Q(party=party) | Q(contacts__party=party)).distinct()
            .select_related("stage", "owner__user", "next_task")
            .order_by(Case(When(status=Engagement.Status.OPEN, then=0), default=1),
                      F("next_action_due_at").asc(nulls_first=True), "-created_at", "pk")[:PARTY_ENGAGEMENT_LIMIT]
        )
        for eng in engagements:
            eng.as_contact = eng.party_id != party.pk
        return {"next_actions": tasks, "party_engagements": engagements, "now": now}

    @staticmethod
    def _timeline(party):
        from communications.kinds import communication_kinds
        from communications.models import Communication

        kinds = communication_kinds()
        rows = list(
            Communication.objects.filter(company_id=party.company_id)
            .filter(Q(party=party) | Q(engagement__party=party))
            .exclude(status=Communication.Status.SCHEDULED, occurred_at__gt=timezone.now())
            .select_related("engagement", "performed_by__user")
            .order_by("-occurred_at", "pk")[:PARTY_TIMELINE_LIMIT]
        )
        for row in rows:
            entry = kinds.get(row.kind)
            row.kind_label = entry.label if entry else row.kind
        return rows

    @staticmethod
    def _mirror_sources(party, rows):
        """``customer:<pk>`` sources -> the Customer, for "from customer account" links."""
        from customers.models import Customer

        pks = {row.source.split(":", 1)[1] for row in rows if row.source.startswith("customer:")}
        if not pks:
            return {}
        try:
            customers = Customer.objects.filter(company_id=party.company_id).in_bulk(list(pks))
        except ValidationError:
            return {}
        return {f"customer:{pk}": c for pk, c in ((str(k), v) for k, v in customers.items())}

    @staticmethod
    def _commerce(party):
        from customers.models import Customer
        from orders.models import Order

        accounts = list(
            Customer.objects.filter(Q(party=party) | Q(account_holder=party))
            .annotate(open_quotes=Count(
                "quotes", filter=Q(quotes__status__in=OPEN_QUOTE_STATUSES), distinct=True,
            ))
            .order_by("last_name", "first_name")
        )
        orders = (
            list(Order.objects.filter(customer__in=accounts).order_by("-created_at")[:5])
            if accounts else []
        )
        return {"commerce_accounts": accounts, "recent_orders": orders}

    @staticmethod
    def _suppressions(party, emails, phones):
        keys = {}
        for row in emails:
            keys[address_hash(row.email_normalized)] = row.email_normalized
        for row in phones:
            if row.number_e164:
                keys[address_hash(row.number_e164)] = row.number_e164
        if not keys:
            return []
        rows = list(Suppression.objects.filter(
            company_id=party.company_id, address_hash__in=keys, lifted_at__isnull=True,
        ).order_by("channel", "-created_at"))
        for row in rows:
            row.masked = row.address_hint or mask_address(keys[row.address_hash])
        return rows

    @staticmethod
    def _access(party):
        """Login, auth roles and provider *names* only; never subjects or tokens."""
        user = party.user
        if user is None:
            return None
        return {
            "username": user.get_username(),
            "is_active": user.is_active,
            "roles": list(user.groups.order_by("name").values_list("name", flat=True)),
            "providers": sorted(set(user.identities.values_list("provider", flat=True))),
        }

    @staticmethod
    def _identifiers(party, accounts):
        targets = Q(content_type=ContentType.objects.get_for_model(Party), object_id=party.pk)
        if accounts:
            from customers.models import Customer

            targets |= Q(
                content_type=ContentType.objects.get_for_model(Customer),
                object_id__in=[a.pk for a in accounts],
            )
        return list(ExternalRef.objects.filter(targets, company_id=party.company_id).order_by("source", "object_type", "source_id")[:50])


# --- erasure (7.4, step 12) ---------------------------------------------------------


def can_erase(user, party) -> bool:
    """Whether the danger zone shows: a person not yet erased, and the permission."""
    return (
        user.has_perm("parties.erase_party")
        and party.erased_at is None and party.kind == PERSON and not party.is_self
    )


class PartyEraseView(ERPViewMixin, View):
    """POST-only: erase a person after a typed confirmation (``parties.erasure``)."""

    def post(self, request, pk):
        party = get_object_or_404(scoped_parties(request), pk=pk)
        from core.storefront import is_storefront_user

        if is_storefront_user(request.user) or not request.user.has_perm("parties.erase_party"):
            raise PermissionDenied("Erasing a party needs the parties.erase_party permission.")
        detail = reverse("erp-party-detail", args=[party.pk])
        if (request.POST.get("confirm") or "").strip() != party.display_name.strip():
            messages.error(request, "Not erased: type the party's name exactly to confirm.")
            return redirect(detail + "#danger-zone")
        try:
            result = erase_party(party, request.user, request.POST.get("reason") or "", source="erp")
        except ErasureError as exc:
            messages.error(request, f"Not erased: {exc}")
            return redirect(detail + "#danger-zone")
        messages.success(request, "Erased. " + erasure_summary(result))
        return redirect(detail)


# --- forms -------------------------------------------------------------------------


class _PartyFormContext:
    """Shared form-page context: the generic ``erp/form.html`` plus a back link.

    ``erp/form.html`` renders ``form.definition_sections`` (the deployment's
    field definitions, phase4-fields.md 5.2) after ``form_sections``."""

    template_name = "erp/form.html"
    current_section = SECTION
    form_sections: list = []

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_sections"] = self.form_sections
        party = getattr(self, "party", None)
        if party is not None and party.pk:
            context["party"] = party
        return context


class PartyCreateView(_PartyFormContext, ERPViewMixin, CreateView):
    model = Party
    form_class = PartyForm
    entity_name = "Party"
    entity_label_key = "parties.party"
    page_title = "New Party"
    page_title_format = "New {singular}"
    form_sections = [
        {"title": "Identity", "fields": ["kind", "given_name", "family_name", "display_name",
                                         "display_name_locked"]},
        {"title": "Organization details", "fields": ["legal_name", "primary_domain",
                                                     "registration_number", "tax_id"]},
        {"title": "Profile", "fields": ["lifecycle_stage", "contact_type"]},
        {"title": "Record", "fields": ["owner", "owner_team", "marketable", "tags_text"]},
    ]

    def dispatch(self, request, *args, **kwargs):
        self.company_id = request_company_id(request) if request.user.is_authenticated else None
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        if self.company_id is None:
            raise PermissionDenied("Your account has no company, so a party cannot be created.")
        kwargs = super().get_form_kwargs()
        kwargs["company_id"] = self.company_id
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        # ``?kind=person``: the CRM contact picker's "New person" link.
        initial = super().get_initial()
        if self.request.GET.get("kind") in (PERSON, ORGANISATION):
            initial["kind"] = self.request.GET["kind"]
        return initial

    def form_valid(self, form):
        try:
            with transaction.atomic():
                response = super().form_valid(form)
                form.save_definition_fields(self.object, by=self.request.user)
        except ValidationError as exc:  # a sidecar limit hit on the locked row
            if isinstance(self, CreateView):
                form.instance._state.adding = True  # the insert was rolled back
            for message in exc.messages:
                form.add_error(None, message)
            return self.form_invalid(form)
        return response

    def get_success_url(self):
        return reverse("erp-party-detail", args=[self.object.pk])


class PartyUpdateView(_PartyFormContext, ERPViewMixin, UpdateView):
    model = Party
    form_class = PartyForm
    entity_name = "Party"
    entity_label_key = "parties.party"
    page_title = "Edit Party"
    page_title_format = "Edit {singular}"
    form_sections = PartyCreateView.form_sections

    def get_queryset(self):
        return scoped_parties(self.request)

    def get_object(self, queryset=None):
        party = super().get_object(queryset)
        if party.erased_at or party.merged_into_id:
            raise PermissionDenied("A merged or erased party cannot be edited.")
        self.party = party
        return party

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        try:
            with transaction.atomic():
                response = super().form_valid(form)
                form.save_definition_fields(self.object, by=self.request.user)
        except ValidationError as exc:  # a sidecar limit hit on the locked row
            if isinstance(self, CreateView):
                form.instance._state.adding = True  # the insert was rolled back
            for message in exc.messages:
                form.add_error(None, message)
            return self.form_invalid(form)
        return response

    def get_success_url(self):
        return reverse("erp-party-detail", args=[self.object.pk])


class _ChildMixin(_PartyFormContext, ERPViewMixin):
    """Create or edit a row hanging off a party of the requester's tenant."""

    anchor = ""
    noun = ""

    def get_party(self):
        party = get_object_or_404(scoped_parties(self.request), pk=self.kwargs["party_pk"])
        if party.erased_at or party.merged_into_id:
            raise PermissionDenied("A merged or erased party cannot be edited.")
        return party

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["party"] = self.party
        return kwargs

    def get_success_url(self):
        return reverse("erp-party-detail", args=[self.party.pk]) + self.anchor

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None)
        verb = "Edit" if obj is not None and not obj._state.adding else "New"
        context["page_title"] = f"{verb} {self.noun} · {self.party.display_name}"
        return context


class _ChildCreate(_ChildMixin, CreateView):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.party = self.get_party()
        return super().dispatch(request, *args, **kwargs)


class _ChildUpdate(_ChildMixin, UpdateView):
    """Edit view; ``read_only(obj)`` refuses rows the sync or the system owns."""

    def get_queryset(self):
        return self.model.objects.for_request(self.request)

    def read_only(self, obj) -> bool:
        return False

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        self.party = self.owning_party(obj)
        if self.party is None:
            raise Http404
        if self.party.erased_at or self.party.merged_into_id:
            raise PermissionDenied("A merged or erased party cannot be edited.")
        if self.read_only(obj):
            raise PermissionDenied("This row is maintained by the system; edit it at its source.")
        return obj

    def owning_party(self, obj):
        return obj.party


class _ChildDelete(ERPViewMixin, View):
    """POST-only delete of a staff-entered contact point."""

    model = None
    anchor = "#contact-points"

    def post(self, request, pk):
        obj = get_object_or_404(self.model.objects.for_request(request), pk=pk)
        party = obj.party
        if party.erased_at or party.merged_into_id or self.read_only(obj):
            raise PermissionDenied("This row is maintained by the system; remove it at its source.")
        with transaction.atomic():
            lock_party(party)
            obj.delete()
            promote_primary(self.model, party)
        messages.success(request, "Removed.")
        return redirect(reverse("erp-party-detail", args=[party.pk]) + self.anchor)

    def read_only(self, obj) -> bool:
        return is_mirrored_point(obj)


EMAIL_SECTIONS = [{"title": "Email", "fields": ["email", "purpose", "status", "is_primary"]}]
PHONE_SECTIONS = [{"title": "Phone", "fields": ["number_raw", "extension", "line_type", "purpose",
                                                "is_primary"]}]
ADDRESS_SECTIONS = [
    {"title": "Address", "fields": ["label", "purpose", "address_line_1", "address_line_2", "city",
                                    "state_province", "postal_code", "country"]},
    {"title": "Use", "fields": ["is_primary", "valid_to"]},
]
ROLE_SECTIONS = [{"title": "Role", "fields": ["role", "valid_from", "valid_to"]}]
RELATIONSHIP_SECTIONS = [
    {"title": "Relationship", "fields": ["kind_side", "other_party", "title", "is_primary"]},
    {"title": "Dates and notes", "fields": ["valid_from", "valid_to", "notes"]},
]
CONSENT_SECTIONS = [
    {"title": "Consent", "fields": ["channel", "purpose", "state", "address"]},
    {"title": "Evidence", "fields": ["occurred_at", "source_detail"]},
]


class PartyEmailCreateView(_ChildCreate):
    model, form_class, noun, anchor = PartyEmail, PartyEmailForm, "email", "#contact-points"
    form_sections = EMAIL_SECTIONS


class PartyEmailUpdateView(_ChildUpdate):
    model, form_class, noun, anchor = PartyEmail, PartyEmailForm, "email", "#contact-points"
    form_sections = EMAIL_SECTIONS

    def read_only(self, obj):
        return is_mirrored_point(obj)


class PartyEmailDeleteView(_ChildDelete):
    model = PartyEmail


class PartyPhoneCreateView(_ChildCreate):
    model, form_class, noun, anchor = PartyPhone, PartyPhoneForm, "phone", "#contact-points"
    form_sections = PHONE_SECTIONS


class PartyPhoneUpdateView(_ChildUpdate):
    model, form_class, noun, anchor = PartyPhone, PartyPhoneForm, "phone", "#contact-points"
    form_sections = PHONE_SECTIONS

    def read_only(self, obj):
        return is_mirrored_point(obj)


class PartyPhoneDeleteView(_ChildDelete):
    model = PartyPhone


class PartyAddressCreateView(_ChildCreate):
    model, form_class, noun, anchor = PartyAddress, PartyAddressForm, "address", "#contact-points"
    form_sections = ADDRESS_SECTIONS


class PartyAddressUpdateView(_ChildUpdate):
    model, form_class, noun, anchor = PartyAddress, PartyAddressForm, "address", "#contact-points"
    form_sections = ADDRESS_SECTIONS

    def read_only(self, obj):
        return obj.customer_addresses.exists()


class PartyAddressDeleteView(_ChildDelete):
    model = PartyAddress

    def read_only(self, obj):
        return obj.customer_addresses.exists()


class _RoleLabels:
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        company_id = self.party.company_id
        kwargs["role_labels"] = {name: role_label(name, company_id) for name in PARTY_ROLES}
        return kwargs


class PartyRoleCreateView(_RoleLabels, _ChildCreate):
    model, form_class, noun, anchor = PartyRole, PartyRoleForm, "role", "#roles"
    form_sections = ROLE_SECTIONS


class PartyRoleUpdateView(_RoleLabels, _ChildUpdate):
    model, form_class, noun, anchor = PartyRole, PartyRoleForm, "role", "#roles"
    form_sections = ROLE_SECTIONS

    def read_only(self, obj):
        config = PARTY_ROLES.get(obj.role)
        return not (config and config.staff_writable)


class _ReturnsToNext:
    """``?next=`` (same host only, ``crm.erp_views.safe_next``) wins over the
    party page, so the customer profile's Relationships card gets its visitor
    back; ``erp/form.html`` carries it through the POST."""

    def get_success_url(self):
        return _safe_next(self.request, super().get_success_url())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["next"] = _safe_next(self.request, "")
        context["party_search"] = True  # erp/form.html loads the picker script
        return context


class PartyRelationshipCreateView(_ReturnsToNext, _ChildCreate):
    model, form_class = PartyRelationship, PartyRelationshipForm
    noun, anchor = "relationship", "#relationships"
    form_sections = RELATIONSHIP_SECTIONS


class PartyRelationshipUpdateView(_ReturnsToNext, _ChildUpdate):
    """Edit dates, title, primary flag and notes; reached from either end."""

    model, form_class = PartyRelationship, PartyRelationshipForm
    noun, anchor = "relationship", "#relationships"
    form_sections = RELATIONSHIP_SECTIONS

    def owning_party(self, obj):
        # Return to the page the edit started from, when it is one of the ends.
        side = self.request.GET.get("party") or self.request.POST.get("party")
        if side and side == str(obj.to_party_id):
            return obj.to_party
        return obj.from_party

    def read_only(self, obj):
        ends_live = not any(p.erased_at or p.merged_into_id for p in (obj.from_party, obj.to_party))
        return is_system_relationship(obj) or not ends_live

    def get_success_url(self):
        party = self.request.GET.get("party")
        target = party if party in (str(self.object.from_party_id), str(self.object.to_party_id)) else \
            self.object.from_party_id
        return _safe_next(self.request, reverse("erp-party-detail", args=[target]) + self.anchor)


class ConsentRecordCreateView(_ChildCreate):
    model, form_class, noun, anchor = ConsentRecord, ConsentRecordForm, "consent record", "#consent"
    form_sections = CONSENT_SECTIONS

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["recorded_by"] = self.request.user
        kwargs["evidence"] = {
            "form": "erp",
            "form_url": self.request.path,
            "user_agent": self.request.META.get("HTTP_USER_AGENT", "")[:255],
        }
        return kwargs
