"""``/erp/integrations/webhooks/`` (mounted by ``core/erp_urls.py``)."""

from django.urls import path

from . import erp_views as v

urlpatterns = [
    path("new/", v.SubscriptionCreateView.as_view(), name="erp-webhook-subscription-create"),
    path("<uuid:pk>/toggle/", v.SubscriptionToggleView.as_view(), name="erp-webhook-subscription-toggle"),
    path("<uuid:pk>/ping/", v.SubscriptionPingView.as_view(), name="erp-webhook-subscription-ping"),
]
