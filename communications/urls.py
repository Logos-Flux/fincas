from rest_framework.routers import DefaultRouter

from .api import CommunicationViewSet

router = DefaultRouter()
router.register("communications", CommunicationViewSet, basename="communication")

urlpatterns = router.urls
