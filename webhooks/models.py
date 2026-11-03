"""Models for Outbound Webhook Subscriptions and Delivery Logs."""

from __future__ import annotations

import secrets

from django.db import models

from core.models import BaseModel


def generate_webhook_secret() -> str:
    """Generate a secure cryptographic secret for HMAC signatures."""
    return f"whsec_{secrets.token_hex(24)}"


class WebhookSubscription(BaseModel):
    """Subscribed external webhook endpoint."""

    target_url = models.URLField(max_length=500, help_text="Destination URL to receive HTTP POST payloads.")
    secret = models.CharField(
        max_length=128,
        default=generate_webhook_secret,
        help_text="Secret key used to compute the X-Golden-Signature HMAC-SHA256 header.",
    )
    previous_secret = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Prior secret kept valid during the rotation window. Clear to retire it.",
    )
    subscribed_events = models.JSONField(
        default=list,
        help_text="List of event names this webhook listens to (e.g. ['order.placed', 'payment.completed', '*']).",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    headers = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional custom HTTP headers to include with each request.",
    )
    company = models.ForeignKey(
        "core.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_subscriptions",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        desc = f" ({self.description})" if self.description else ""
        return f"{self.target_url}{desc}"

    def event_version(self, event_name: str) -> int | None:
        """The payload version this subscription receives for ``event_name``,
        or None when it does not subscribe to it (or is inactive).

        Entries may pin a version with ``@<n>`` (``customer.registered@1``);
        see ``webhooks.events`` for the rules.
        """
        if not self.is_active:
            return None
        from .events import resolve_version

        return resolve_version(self.subscribed_events or [], event_name, created_at=self.created_at)

    def matches_event(self, event_name: str) -> bool:
        """Check if this subscription matches the given event name."""
        return self.event_version(event_name) is not None


class WebhookDeliveryLog(BaseModel):
    """Record of an outbound webhook dispatch attempt."""

    subscription = models.ForeignKey(
        WebhookSubscription,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    event_type = models.CharField(max_length=100, db_index=True)
    payload = models.JSONField()
    response_status_code = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    duration_ms = models.IntegerField(default=0)
    attempts = models.IntegerField(
        default=1,
        help_text="Total HTTP attempts for this delivery (initial + retries).",
    )
    success = models.BooleanField(default=False, db_index=True)
    error_message = models.TextField(blank=True)
    # The core.OutboxEvent (``webhooks.deliver`` row) this attempt belongs to,
    # sent as X-Golden-Delivery-Id. Null for a ping and for pre-outbox rows.
    delivery_id = models.UUIDField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return f"[{status}] {self.event_type} -> {self.subscription.target_url} ({self.response_status_code})"
