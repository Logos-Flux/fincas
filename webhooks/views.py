"""REST API ViewSets for Webhook Subscriptions and Delivery Logs."""

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.managers import scope_to_company
from core.permissions import PrincipalPermission
from core.views import CompanyScopedViewSet

from .models import WebhookDeliveryLog, WebhookSubscription
from .serializers import WebhookDeliveryLogSerializer, WebhookSubscriptionSerializer
from .services import ping


class WebhookSubscriptionViewSet(CompanyScopedViewSet):
    """Manage outbound webhook endpoints and dispatch test pings."""

    queryset = WebhookSubscription.objects.all()
    serializer_class = WebhookSubscriptionSerializer
    permission_classes = [PrincipalPermission]
    filterset_fields = ["is_active", "company"]
    search_fields = ["target_url", "description"]

    def get_queryset(self):
        return scope_to_company(super().get_queryset(), self.request)

    @action(detail=True, methods=["post"])
    def ping(self, request, pk=None):
        """Dispatch a test ping payload to verify webhook endpoint connectivity."""
        subscription = self.get_object()
        log = ping(subscription, triggered_by=request.user.username if request.user else "api")
        return Response(
            {
                "success": log.success,
                "status_code": log.response_status_code,
                "duration_ms": log.duration_ms,
                "error": log.error_message,
                # The X-Golden-Delivery-Id the receiver saw (since 2026-10-17;
                # it was the log row's own id before, which no header carried).
                "delivery_id": str(log.delivery_id),
                "log_id": str(log.id),
            },
            status=status.HTTP_200_OK if log.success else status.HTTP_502_BAD_GATEWAY,
        )


class WebhookDeliveryLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Inspect outbound webhook dispatch logs and delivery outcomes."""

    queryset = WebhookDeliveryLog.objects.select_related("subscription").all()
    serializer_class = WebhookDeliveryLogSerializer
    permission_classes = [PrincipalPermission]
    filterset_fields = ["subscription", "event_type", "success"]
    search_fields = ["event_type", "subscription__target_url", "error_message"]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "subscription__company"
        )
