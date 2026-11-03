from rest_framework.routers import DefaultRouter

from .views import WarehouseStockViewSet, WarehouseViewSet

router = DefaultRouter()
router.register("warehouses", WarehouseViewSet)
router.register("stock", WarehouseStockViewSet)

urlpatterns = router.urls
