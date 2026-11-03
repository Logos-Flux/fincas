"""Staff REST API for parties (docs/crm/phase2-party.md 5.2, step 8).

Mounted at ``/api/v1/parties/`` (house style: flat resources filtered by
``party=``). Every viewset pins ``PrincipalPermission`` explicitly: staff
roles and superusers pass, the storefront identity gets 403 (its group holds
no ``parties`` permission), a customer-only account gets 403. Rows are
company-scoped through ``CompanyScopedViewSet``; contact points, roles,
relationships and consents also check their parties' tenant
(``parent_links``) on write.

Merge, unmerge, merge candidates, erasure, suppressions and external refs
belong to steps 6 and 12; this module serves the rest of the 5.2 table.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from core.models import Company
from core.permissions import PrincipalPermission
from core.views import CompanyScopedViewSet, request_company

from .envelope import build_envelope
from .mirror import default_company_id
from .models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
)
from .ownership import contact_point_owner, owner_message
from .serializers import (
    ConsentRecordSerializer,
    MatchEnvelopeSerializer,
    MatchQuerySerializer,
    PartyAddressSerializer,
    PartyEmailSerializer,
    PartyListSerializer,
    PartyPhoneSerializer,
    PartyRelationshipSerializer,
    PartyRoleSerializer,
    PartySerializer,
)

TAG = "parties"
TRUE = ("1", "true", "yes")


def _flag(request, name) -> bool | None:
    value = request.query_params.get(name)
    if value is None or value == "":
        return None
    return value.lower() in TRUE


@extend_schema(tags=[TAG])
@extend_schema_view(
    list=extend_schema(parameters=[
        OpenApiParameter("role", str, description="Holds this role now (an open PartyRole)."),
        OpenApiParameter("has_customer", bool, description="Countersigns or holds a Customer."),
        OpenApiParameter("include_merged", bool, description="Also list merged-away and erased parties."),
        OpenApiParameter("owner_team", str, description="A team id, or `none`."),
        OpenApiParameter("owner_archived", bool, description="The owner is an archived profile."),
        OpenApiParameter("lifecycle_stage", str, description="A GOLDEN_LIFECYCLE_STAGES key, or `none` (blank)."),
        OpenApiParameter("contact_type", str, description="A GOLDEN_CONTACT_TYPES key, or `none` (blank)."),
        OpenApiParameter("search", str, description="Name prefix, exact normalized email or exact E.164."),
    ]),
    retrieve=extend_schema(description="A merged-away id answers 301 with the survivor's URL."),
)
class PartyViewSet(CompanyScopedViewSet):
    """``parties/``: GET, POST, PATCH. No PUT and no DELETE (merge and erasure keep the row)."""

    permission_classes = [PrincipalPermission]
    queryset = Party.objects.select_related("company").prefetch_related(
        "addresses__customer_addresses",
        Prefetch("roles", queryset=PartyRole.objects.filter(valid_to__isnull=True), to_attr="open_roles"),
    )
    filterset_fields = ["kind", "owner", "marketable", "is_active"]
    ordering_fields = ["display_name", "created_at", "updated_at"]
    ordering = ["display_name"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        return PartyListSerializer if self.action == "list" else PartySerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action != "list":
            return qs
        params = self.request.query_params
        if not _flag(self.request, "include_merged"):
            qs = qs.filter(merged_into__isnull=True, erased_at__isnull=True)
        role = params.get("role")
        if role:
            qs = qs.filter(Exists(PartyRole.objects.filter(
                party=OuterRef("pk"), role=role, valid_to__isnull=True)))
        has_customer = _flag(self.request, "has_customer")
        if has_customer is not None:
            from customers.models import Customer

            linked = Exists(Customer.objects.filter(Q(party=OuterRef("pk")) | Q(account_holder=OuterRef("pk"))))
            qs = qs.filter(linked) if has_customer else qs.exclude(linked)
        team = params.get("owner_team")
        if team == "none":
            qs = qs.filter(owner_team__isnull=True)
        elif team:
            if not _is_uuid(team):
                return qs.none()
            qs = qs.filter(owner_team_id=team)
        owner_archived = _flag(self.request, "owner_archived")
        if owner_archived is not None:
            from core.staff import archived_q

            qs = qs.filter(archived_q("owner")) if owner_archived else qs.exclude(archived_q("owner"))
        for name in ("lifecycle_stage", "contact_type"):
            value = params.get(name)
            if value:
                qs = qs.filter(**{name: "" if value == "none" else value})
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(self._search(search))
        return qs

    def _require_team_edit(self, serializer, instance=None):
        """Naming ``owner_team`` directly (not via the owner stamp) needs
        ``crm.reassign_engagement`` (phase4-fields.md 2.1)."""
        if "owner_team" not in serializer.validated_data:
            return
        team = serializer.validated_data["owner_team"]
        current = getattr(instance, "owner_team", None) if instance is not None else None
        if team != current and not self.request.user.has_perm("crm.reassign_engagement"):
            raise PermissionDenied("Setting owner_team directly needs crm.reassign_engagement.")

    def perform_update(self, serializer):
        self._require_team_edit(serializer, serializer.instance)
        super().perform_update(serializer)

    @staticmethod
    def _search(text):
        from .mirror import phone_parts, safe_email

        cond = (
            Q(display_name__istartswith=text) | Q(given_name__istartswith=text)
            | Q(family_name__istartswith=text) | Q(legal_name__istartswith=text)
        )
        email = safe_email(text) if "@" in text else ""
        if email:
            cond |= Q(Exists(PartyEmail.objects.filter(party=OuterRef("pk"), email_normalized=email)))
        e164 = phone_parts(text, None)[1] if any(ch.isdigit() for ch in text) else ""
        if e164:
            cond |= Q(Exists(PartyPhone.objects.filter(party=OuterRef("pk"), number_e164=e164)))
        return cond

    def retrieve(self, request, *args, **kwargs):
        party = self.get_object()
        if party.merged_into_id is not None:
            survivor, seen = party, {party.pk}
            while survivor.merged_into_id is not None and survivor.merged_into_id not in seen:
                seen.add(survivor.merged_into_id)
                # Merges never cross tenants (3.1); scope the hop anyway.
                survivor = Party.objects.get(pk=survivor.merged_into_id, company_id=party.company_id)
            # Same mount the caller used (/api/v1/ or the unversioned alias).
            prefix = request.path.rsplit(f"{party.pk}/", 1)[0]
            location = request.build_absolute_uri(f"{prefix}{survivor.pk}/")
            return Response(
                {"detail": "Merged into another party.", "survivor": str(survivor.pk)},
                status=status.HTTP_301_MOVED_PERMANENTLY, headers={"Location": location},
            )
        return Response(self.get_serializer(party).data)

    def update(self, request, *args, **kwargs):
        # An erased party is a tombstone (phase2-party.md 7.5): writing a name,
        # metadata or tags onto it would put personal data back.
        if self.get_object().erased_at is not None:
            raise ValidationError({"detail": "This party has been erased; it cannot be edited."})
        return super().update(request, *args, **kwargs)

    def perform_create(self, serializer):
        if self.request.user.is_superuser:
            # A superuser or service key: cross-tenant by definition, so the
            # tenant must be named (or be unambiguous).
            company = self._service_company()
            owner = serializer.validated_data.get("owner")
            if owner is not None and owner.company_id != company.pk:
                raise ValidationError({"owner": "The owner must belong to the party's company."})
            team = serializer.validated_data.get("owner_team")
            if team is not None and team.company_id != company.pk:
                raise ValidationError({"owner_team": "The team must belong to the party's company."})
        else:
            company = request_company(self.request)
            if company is None:
                # Staff without a resolvable tenant: reads already fail closed; so do writes.
                raise PermissionDenied("Your account has no company: it cannot create parties.")
        self._require_team_edit(serializer)
        serializer.save(company=company)

    def _service_company(self):
        raw = self.request.data.get("company") if hasattr(self.request.data, "get") else None
        if raw:
            company = Company.objects.filter(pk=raw).first() if _is_uuid(raw) else None
            if company is None:
                raise ValidationError({"company": "No such company."})
            return company
        company_id = default_company_id(Company)
        if company_id is None:
            raise ValidationError(
                {"company": "Several companies exist: name the tenant with company=<uuid>."}
            )
        return Company.objects.get(pk=company_id)


def _is_uuid(value) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


class _PartyChildViewSet(CompanyScopedViewSet):
    """Contact points: ``party=`` filter; the party must be the requester's tenant."""

    permission_classes = [PrincipalPermission]
    parent_links = [("party", "company")]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    ordering_fields = ["created_at"]
    ordering = ["-is_primary", "created_at"]

    def perform_destroy(self, instance):
        owner = contact_point_owner(instance)
        if owner is not None:
            raise ValidationError({"mirrored_from": owner_message(owner)})
        from .sync import _promote_primary

        model, party = type(instance), instance.party
        with transaction.atomic():
            instance.delete()
            if instance.is_primary:
                _promote_primary(model, party)


@extend_schema(tags=[TAG])
class PartyEmailViewSet(_PartyChildViewSet):
    permission_classes = [PrincipalPermission]
    queryset = PartyEmail.objects.select_related("party")
    serializer_class = PartyEmailSerializer
    filterset_fields = ["party", "purpose", "status", "is_primary"]


@extend_schema(tags=[TAG])
class PartyPhoneViewSet(_PartyChildViewSet):
    permission_classes = [PrincipalPermission]
    queryset = PartyPhone.objects.select_related("party")
    serializer_class = PartyPhoneSerializer
    filterset_fields = ["party", "purpose", "line_type", "is_primary"]


@extend_schema(tags=[TAG])
class PartyAddressViewSet(_PartyChildViewSet):
    permission_classes = [PrincipalPermission]
    queryset = PartyAddress.objects.select_related("party").prefetch_related("customer_addresses")
    serializer_class = PartyAddressSerializer
    filterset_fields = ["party", "purpose", "country", "is_primary"]


@extend_schema(tags=[TAG])
@extend_schema_view(
    list=extend_schema(parameters=[OpenApiParameter("current", bool, description="Only open roles.")]),
    partial_update=extend_schema(description="Only valid_to can change. customer/staff are refused."),
)
class PartyRoleViewSet(CompanyScopedViewSet):
    """``roles/``: GET, POST, PATCH (``valid_to`` only). System roles are refused."""

    permission_classes = [PrincipalPermission]
    queryset = PartyRole.objects.select_related("party")
    serializer_class = PartyRoleSerializer
    parent_links = [("party", "company")]
    filterset_fields = ["party", "role"]
    ordering_fields = ["valid_from", "created_at"]
    ordering = ["-valid_from"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "list" and _flag(self.request, "current"):
            qs = qs.filter(valid_to__isnull=True)
        return qs


@extend_schema(tags=[TAG])
@extend_schema_view(
    list=extend_schema(parameters=[
        OpenApiParameter("party", str, description="Either side of the relationship."),
        OpenApiParameter("kind", str),
        OpenApiParameter("current", bool, description="Only open relationships."),
    ]),
)
class PartyRelationshipViewSet(CompanyScopedViewSet):
    """``relationships/``: GET, POST, PATCH. A symmetric kind is stored in canonical order."""

    permission_classes = [PrincipalPermission]
    queryset = PartyRelationship.objects.select_related("from_party", "to_party")
    serializer_class = PartyRelationshipSerializer
    parent_links = [("from_party", "company"), ("to_party", "company")]
    ordering_fields = ["valid_from", "created_at"]
    ordering = ["created_at"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action != "list":
            return qs
        params = self.request.query_params
        party = params.get("party")
        if party:
            if not _is_uuid(party):
                return qs.none()
            qs = qs.filter(Q(from_party_id=party) | Q(to_party_id=party))
        if params.get("kind"):
            qs = qs.filter(kind=params["kind"])
        if _flag(self.request, "current"):
            qs = qs.filter(valid_to__isnull=True)
        return qs


@extend_schema(tags=[TAG])
class ConsentRecordViewSet(CompanyScopedViewSet):
    """``consents/``: GET and POST only. The ledger is append-only (1.6)."""

    permission_classes = [PrincipalPermission]
    queryset = ConsentRecord.objects.select_related("party")
    serializer_class = ConsentRecordSerializer
    parent_links = [("party", "company")]
    filterset_fields = ["party", "channel", "purpose", "state", "source"]
    ordering_fields = ["occurred_at", "recorded_at"]
    ordering = ["-occurred_at", "-recorded_at"]
    http_method_names = ["get", "post", "head", "options"]


@extend_schema(tags=[TAG])
class PartyMatchViewSet(viewsets.ViewSet):
    """``match/``: the 2.7 envelope. Never creates, never picks a candidate.

    GET takes query parameters, POST the same fields as a JSON body. A tenant
    user matches inside its own company; a superuser or service key matches
    across tenants (so a shared email is ``ambiguous``) unless it names one
    with ``company``.
    """

    permission_classes = [PrincipalPermission]

    @extend_schema(parameters=[MatchQuerySerializer], responses=MatchEnvelopeSerializer)
    def list(self, request):
        return self._match(request.query_params)

    @extend_schema(request=MatchQuerySerializer, responses=MatchEnvelopeSerializer)
    def create(self, request):
        return self._match(request.data)

    def _match(self, data):
        query = MatchQuerySerializer(data=data)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        user = self.request.user
        if user.is_superuser:
            company_ids = [params["company"]] if params.get("company") else None
        else:
            company = request_company(self.request)
            if params.get("company") and (company is None or params["company"] != company.pk):
                raise ValidationError({"company": "Cannot match in another company."})
            # No resolvable tenant: fail closed to an empty tenant set.
            company_ids = [company.pk] if company is not None else []
        envelope = build_envelope(
            company_ids, target=params["target"], query=params.get("q"),
            email=params.get("email"), phone=params.get("phone"), region=params.get("region") or None,
        )
        if not (params.get("q") or params.get("email") or params.get("phone")):
            envelope["message"] = "Pass q, email or phone."
        return Response(envelope)
