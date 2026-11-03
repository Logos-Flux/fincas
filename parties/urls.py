"""``/api/v1/parties/`` (docs/crm/phase2-party.md 5.2)."""

from rest_framework.routers import DefaultRouter

from .views import (
    ConsentRecordViewSet,
    PartyAddressViewSet,
    PartyEmailViewSet,
    PartyMatchViewSet,
    PartyPhoneViewSet,
    PartyRelationshipViewSet,
    PartyRoleViewSet,
    PartyViewSet,
)

router = DefaultRouter()
router.register("parties", PartyViewSet)
router.register("emails", PartyEmailViewSet)
router.register("phones", PartyPhoneViewSet)
router.register("addresses", PartyAddressViewSet)
router.register("roles", PartyRoleViewSet)
router.register("relationships", PartyRelationshipViewSet)
router.register("consents", ConsentRecordViewSet)
router.register("match", PartyMatchViewSet, basename="party-match")

urlpatterns = router.urls
