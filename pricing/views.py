from rest_framework import viewsets

from core.managers import scope_to_company
from core.permissions import PrincipalPermission
from core.views import CompanyScopedViewSet

from .models import (
    PriceList,
    PriceListItem,
    TaxRate,
)
from .serializers import (
    PriceListItemSerializer,
    PriceListSerializer,
    TaxRateSerializer,
)


class PriceListViewSet(CompanyScopedViewSet):
    queryset = PriceList.objects.all()
    serializer_class = PriceListSerializer
    filterset_fields = ["price_list_type", "is_active", "currency", "company"]
    search_fields = ["name"]


class PriceListItemViewSet(CompanyScopedViewSet):
    queryset = PriceListItem.objects.select_related("price_list", "variant")
    serializer_class = PriceListItemSerializer
    filterset_fields = ["price_list", "variant", "is_active"]
    search_fields = ["variant__sku"]
    company_field = None
    parent_links = [("price_list", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "price_list__company"
        )


class TaxRateViewSet(viewsets.ModelViewSet):
    # Global tax table (no company FK) by design — shared reference data.
    # Explicit auth so prod/test/dev agree (F-003) + storefront gate.
    permission_classes = [PrincipalPermission]
    queryset = TaxRate.objects.all()
    serializer_class = TaxRateSerializer
    filterset_fields = ["tax_type", "is_active", "jurisdiction"]
    search_fields = ["name", "jurisdiction"]
