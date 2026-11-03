from rest_framework.routers import DefaultRouter

from .views import (
    POLineItemViewSet,
    POReceiptViewSet,
    PurchaseOrderViewSet,
    SupplierViewSet,
)

router = DefaultRouter()
router.register("suppliers", SupplierViewSet)
router.register("purchase-orders", PurchaseOrderViewSet)
router.register("po-lines", POLineItemViewSet)
router.register("receipts", POReceiptViewSet)

urlpatterns = router.urls
