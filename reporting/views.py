from rest_framework import viewsets

from core.permissions import PrincipalPermission

from .models import ReportDefinition
from .serializers import ReportDefinitionSerializer


class ReportDefinitionViewSet(viewsets.ModelViewSet):
    # NOTE: saved report definitions carry no company FK, so this
    # endpoint cannot tenant-isolate — see M1 Track A findings.
    # Explicit auth so prod/test/dev agree (F-003) + storefront gate.
    permission_classes = [PrincipalPermission]
    queryset = ReportDefinition.objects.all()
    serializer_class = ReportDefinitionSerializer
    filterset_fields = ["is_active", "chart_type"]
    search_fields = ["name"]
