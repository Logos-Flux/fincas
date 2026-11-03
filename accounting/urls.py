from rest_framework.routers import DefaultRouter

from .api import FiscalPeriodViewSet

router = DefaultRouter()
router.register("periods", FiscalPeriodViewSet)

urlpatterns = router.urls
