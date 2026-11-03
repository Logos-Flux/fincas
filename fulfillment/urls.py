from rest_framework.routers import DefaultRouter

from .views import PickTicketLineViewSet, PickTicketViewSet

router = DefaultRouter()
router.register("pick-tickets", PickTicketViewSet)
router.register("pick-ticket-lines", PickTicketLineViewSet)

urlpatterns = router.urls
