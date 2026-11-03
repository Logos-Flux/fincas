from django.db.models import F

from core.managers import scope_to_company
from core.views import CompanyScopedViewSet

from .models import Warehouse, WarehouseStock
from .serializers import WarehouseSerializer, WarehouseStockSerializer


class WarehouseViewSet(CompanyScopedViewSet):
    # Step 2 (F-027): owned via Warehouse.company; company-less rows fail
    # closed (invisible), never shared.
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    filterset_fields = ["warehouse_type", "is_active", "can_fulfill"]
    search_fields = ["warehouse_code", "name"]


class WarehouseStockViewSet(CompanyScopedViewSet):
    # Step 2 (F-027): company inherited through warehouse__company — there
    # is deliberately no second FK on the stock row to drift.
    queryset = WarehouseStock.objects.select_related("variant", "warehouse")
    serializer_class = WarehouseStockSerializer
    filterset_fields = ["variant", "warehouse"]
    search_fields = ["variant__sku", "bin_location"]
    company_field = None
    parent_links = [("warehouse", "company")]

    def get_queryset(self):
        """Company scope first, then `?below_reorder=true` support.

        The reorder filter (used by erp-assist `list_low_stock`) applies
        on top of the tenant scope — never instead of it. Only rows with
        a positive `reorder_level` are considered, so rows with reorder
        tracking disabled (0) are excluded.
        """
        qs = scope_to_company(
            super().get_queryset(), self.request, "warehouse__company"
        )
        below = (self.request.query_params.get("below_reorder") or "").lower()
        if below in ("true", "1", "yes"):
            qs = qs.filter(reorder_level__gt=0, quantity__lte=F("reorder_level"))
        return qs
