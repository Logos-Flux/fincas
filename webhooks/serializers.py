"""Serializers for Webhook Subscription and Delivery models."""

from rest_framework import serializers

from .events import validate_entries
from .models import WebhookDeliveryLog, WebhookSubscription


class WebhookSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookSubscription
        fields = [
            "id",
            "target_url",
            "secret",
            "subscribed_events",
            "is_active",
            "description",
            "headers",
            "company",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {
            "secret": {"write_only": False},  # Readable upon creation so caller can store it
        }

    def validate_subscribed_events(self, value):
        # ``@<n>`` pins a payload version (docs/webhooks.md, Payload versions).
        errors = validate_entries(value)
        if errors:
            raise serializers.ValidationError(errors)
        return value


class WebhookDeliveryLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookDeliveryLog
        fields = [
            "id",
            "subscription",
            "event_type",
            "payload",
            "response_status_code",
            "response_body",
            "duration_ms",
            "success",
            "error_message",
            "delivery_id",
            "created_at",
        ]
        read_only_fields = fields
