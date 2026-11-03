from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.settings import api_settings

from core.managers import scope_to_company
from core.permissions import PrincipalPermission
from core.views import CompanyScopedViewSet, request_company

from . import activity_compat
from .intake import lead_status_q
from .models import Lead
from .serializers import ENGAGEMENT_DOCS, LeadDetailSerializer, LeadListSerializer


class LeadViewSet(CompanyScopedViewSet):
    queryset = Lead.objects.select_related("engagement__stage")
    # ``?status=`` is served by get_queryset: it matches the derived status.
    filterset_fields = ["source", "assigned_to", "company", "engagement"]
    search_fields = ["lead_number", "first_name", "last_name", "email", "company_name"]
    ordering_fields = ["created_at", "estimated_value"]
    # A lead with no company gets no party and never reaches the CRM
    # (phase4-fields.md 6, item 3): a company-less create is refused.
    require_company = True

    def get_queryset(self):
        qs = super().get_queryset()
        status = self.request.query_params.get("status") if self.request else None
        if status:
            if status not in Lead.Status.values:
                raise ValidationError({"status": [f"Select a valid choice. {status} is not one of "
                                                  "the available choices."]})
            qs = qs.filter(lead_status_q(status))
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return LeadListSerializer
        return LeadDetailSerializer


class ActivityLogViewSet(viewsets.ViewSet):
    """The ActivityLog API, served from Communication + Task (``crm.activity_compat``).

    Same URL, ids and JSON as before CRM Phase 3 step 6, for one release.
    Scoped to the requester's company (superusers see every tenant); a write
    may not hang an activity on another tenant's lead, customer or staff
    profile, or name another company.
    """

    permission_classes = [PrincipalPermission]
    pagination_class = api_settings.DEFAULT_PAGINATION_CLASS
    # The old viewset's lookups, for the schema and for readers of this file.
    filterset_fields = ["lead", "customer", "activity_type", "performed_by"]
    search_fields = ["subject"]
    ordering_fields = ["activity_date"]
    _PARENTS = ("lead", "customer", "performed_by")

    def _scope(self, qs):
        return scope_to_company(qs, self.request)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Deprecation"] = "true"
        response["Link"] = f'<{ENGAGEMENT_DOCS}>; rel="deprecation"'
        return response

    def _row(self, pk):
        row = activity_compat.find(pk, scope=self._scope)
        if row is None:
            raise NotFound()
        return row

    def list(self, request):
        rows = activity_compat.filtered_rows(request.query_params, scope=self._scope)
        serializer = activity_compat.ActivityCompatSerializer
        if self.pagination_class is None:
            return Response(serializer(rows, many=True).data)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(serializer(page, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(activity_compat.ActivityCompatSerializer(self._row(pk)).data)

    def _company_for(self, data, row=None):
        """The tenant the activity belongs to, with the old viewset's guards."""
        company = request_company(self.request)
        requested = data.get("company")
        if company is not None:
            if requested is not None and requested.pk != company.pk:
                raise ValidationError({"company": "Cannot create records for another company."
                                       if row is None else "Cannot move records to another company."})
            if row is not None and "company" in data and requested is None:
                raise ValidationError({"company": "Cannot remove the company from a record."})
        elif row is not None:
            company = row.obj.company
        else:
            company = requested
            for name in self._PARENTS:  # superuser: derive like ActivityLog.save() did
                parent = data.get(name)
                if company is None and parent is not None and parent.company_id is not None:
                    company = parent.company
            if company is None:
                raise ValidationError({"company": "An activity needs a company."})
        for name in self._PARENTS:
            parent = data.get(name)
            if parent is not None and parent.company_id is not None and parent.company_id != company.pk:
                raise ValidationError({name: "Linked record belongs to another company."})
        return company

    def create(self, request):
        serializer = activity_compat.ActivityCompatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = self._company_for(serializer.validated_data)
        row = activity_compat.create(company, serializer.validated_data)
        return Response(activity_compat.ActivityCompatSerializer(row).data,
                        status=http_status.HTTP_201_CREATED)

    def _update(self, request, pk, partial):
        row = self._row(pk)
        serializer = activity_compat.ActivityCompatSerializer(data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        company = self._company_for(serializer.validated_data, row=row)
        row = activity_compat.update(row, company, serializer.validated_data)
        return Response(activity_compat.ActivityCompatSerializer(row).data)

    def update(self, request, pk=None):
        return self._update(request, pk, partial=False)

    def partial_update(self, request, pk=None):
        return self._update(request, pk, partial=True)

    def destroy(self, request, pk=None):
        self._row(pk).obj.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)
