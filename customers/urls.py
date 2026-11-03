from rest_framework.routers import DefaultRouter

from .views import CustomerAddressViewSet, CustomerViewSet

router = DefaultRouter()
router.register("customers", CustomerViewSet)
router.register("addresses", CustomerAddressViewSet)

urlpatterns = router.urls
