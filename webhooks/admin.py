"""Django admin registration for Webhooks."""

from django.contrib import admin

from .models import WebhookDeliveryLog, WebhookSubscription


@admin.register(WebhookSubscription)
class WebhookSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["target_url", "is_active", "subscribed_events", "created_at"]
    list_filter = ["is_active", "company"]
    search_fields = ["target_url", "description"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(WebhookDeliveryLog)
class WebhookDeliveryLogAdmin(admin.ModelAdmin):
    list_display = ["event_type", "subscription", "response_status_code", "duration_ms", "success", "created_at"]
    list_filter = ["success", "event_type"]
    search_fields = ["event_type", "subscription__target_url", "error_message"]
    readonly_fields = [
        "id",
        "subscription",
        "event_type",
        "payload",
        "response_status_code",
        "response_body",
        "duration_ms",
        "success",
        "error_message",
        "created_at",
    ]
