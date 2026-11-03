from rest_framework.routers import DefaultRouter

from .views import (
    PriceListItemViewSet,
    PriceListViewSet,
    TaxRateViewSet,
)

router = DefaultRouter()
router.register("price-lists", PriceListViewSet)
router.register("price-list-items", PriceListItemViewSet)
router.register("tax-rates", TaxRateViewSet)

urlpatterns = router.urls
