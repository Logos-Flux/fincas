"""URL patterns for Webhooks API."""

from rest_framework.routers import DefaultRouter

from .views import WebhookDeliveryLogViewSet, WebhookSubscriptionViewSet

router = DefaultRouter()
router.register(r"subscriptions", WebhookSubscriptionViewSet, basename="webhook-subscription")
router.register(r"deliveries", WebhookDeliveryLogViewSet, basename="webhook-delivery")

urlpatterns = router.urls
