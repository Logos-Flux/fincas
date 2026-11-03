"""Staff REST API for communications (CRM Phase 3 step 7, docs/crm/phase3-engagement.md 5.2).

Mounted at ``/api/v1/communications/``. ``PrincipalPermission`` is pinned
explicitly: staff and superusers pass, the storefront identity and a
customer-only account get 403. Rows are company-scoped.

- POST logs a communication through ``communications.ingest.record``
  (``provider="manual"``, ``performed_by`` = the caller). Provider imports
  stay in-process (Phase 6 adapters), never through this endpoint.
- PATCH only where ``is_editable`` (manual logs): golden-sent emails and
  provider imports are records, a write to them is 400 ``not_editable``.
- ``{id}/raw/`` serves the provider payload sidecar and needs
  ``communications.view_raw_communication`` (403 without it, 404 when the
  communication has none). No other response carries the payload.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from core.permissions import PrincipalPermission
from core.views import CompanyScopedViewSet
from crm.api import caller_profile, parse_instant, write_company
from crm.api_serializers import django_errors

from . import ingest
from .models import Communication, CommunicationRaw
from .serializers import (
    CommunicationRawSerializer,
    CommunicationSerializer,
    CommunicationUpdateSerializer,
)

TAG = "communications"
RAW_PERMISSION = "communications.view_raw_communication"


class NotEditable(APIException):
    status_code = 400
    default_code = "not_editable"

    def __init__(self):
        super().__init__()
        self.detail = {
            "detail": "This communication is a record (a golden-sent email or a provider "
                      "import), not a note: it cannot be edited.",
            "code": self.default_code,
        }


@extend_schema(tags=[TAG])
@extend_schema_view(
    list=extend_schema(parameters=[
        OpenApiParameter("engagement", str), OpenApiParameter("party", str),
        OpenApiParameter("kind", str), OpenApiParameter("direction", str),
        OpenApiParameter("since", str, description="ISO 8601: occurred at or after."),
    ]),
    create=extend_schema(description="Logs through communications.ingest.record "
                                     "(provider=manual). `participant_input` lists "
                                     "{role, address, display_name, party}."),
    partial_update=extend_schema(description="Only on is_editable rows (400 not_editable)."),
)
class CommunicationViewSet(CompanyScopedViewSet):
    """``communications/``: GET, POST, PATCH; ``raw`` on one communication."""

    permission_classes = [PrincipalPermission]
    queryset = Communication.objects.select_related("engagement", "party").prefetch_related("participants")
    serializer_class = CommunicationSerializer
    filterset_fields = ["engagement", "party", "kind", "direction", "status", "thread_key"]
    ordering_fields = ["occurred_at", "created_at"]
    ordering = ["-occurred_at"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action in ("update", "partial_update"):
            return CommunicationUpdateSerializer
        return CommunicationSerializer

    def get_queryset(self):
        # has_raw without a query per row.
        qs = super().get_queryset().annotate(
            has_raw_payload=Exists(CommunicationRaw._base_manager.filter(communication=OuterRef("pk"))))
        since = self.request.query_params.get("since") if self.action == "list" else None
        if since:
            at = parse_instant(since)
            if at is None:
                raise ValidationError({"since": "An ISO 8601 instant."})
            qs = qs.filter(occurred_at__gte=at)
        return qs

    def perform_create(self, serializer):
        company = write_company(self.request)
        v = dict(serializer.validated_data)
        for name in ("engagement", "party"):
            target = v.get(name)
            if target is not None and target.company_id != company.pk:
                raise ValidationError({name: f"The {name} belongs to another company."})
        specs = [
            {"role": p["role"], "address": p["address"], "display_name": p["display_name"],
             "party": p["party"]}
            for p in v.pop("participant_specs", [])
        ]
        for spec in specs:
            if spec["party"] is not None and spec["party"].company_id != company.pk:
                raise ValidationError({"participant_input": "A participant's party belongs to another company."})
        try:
            result = ingest.record(
                company, v["kind"], direction=v["direction"], occurred_at=v["occurred_at"],
                participants=specs, provider=ingest.MANUAL, subject=v.get("subject", ""),
                body_text=v.get("body_text", ""), summary=v.get("summary", ""),
                status=v.get("status", Communication.Status.COMPLETED), ended_at=v.get("ended_at"),
                duration_seconds=v.get("duration_seconds"), thread_key=v.get("thread_key", ""),
                engagement=v.get("engagement"), party=v.get("party"),
                performed_by=caller_profile(self.request), metadata=v.get("metadata") or None,
            )
        except ingest.IngestError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        except DjangoValidationError as exc:
            raise django_errors(exc) from exc
        serializer.instance = result.communication

    def perform_update(self, serializer):
        comm = serializer.instance
        if not comm.is_editable:
            raise NotEditable()
        # ``metadata`` is validated as a merge patch (core.sidecar_api) and a
        # communication takes no client keys, so the patch is always empty:
        # never assign it (that would replace the sidecar).
        for name, value in serializer.validated_data.items():
            if name == "metadata":
                continue
            setattr(comm, name, value)
        try:
            with transaction.atomic():
                # The save writes the whole row: take the stored sidecar under a
                # lock so a concurrent golden write to it is not overwritten.
                comm.metadata = Communication._base_manager.select_for_update().only(
                    "pk", "metadata").get(pk=comm.pk).metadata
                comm.full_clean(validate_unique=False)
                comm.save()
        except DjangoValidationError as exc:
            raise django_errors(exc) from exc

    @extend_schema(responses=CommunicationRawSerializer,
                   description="Needs communications.view_raw_communication.")
    @action(detail=True, methods=["get"])
    def raw(self, request, pk=None):
        comm = self.get_object()
        if not request.user.has_perm(RAW_PERMISSION):
            raise PermissionDenied(f"Raw provider payloads need the {RAW_PERMISSION} permission.")
        raw = CommunicationRaw._base_manager.filter(communication=comm).first()
        if raw is None:
            raise NotFound("This communication has no raw payload.")
        return Response(CommunicationRawSerializer(raw).data)
