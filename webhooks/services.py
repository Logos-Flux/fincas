"""Outbound Webhook dispatch, cryptographic signing, and logging service."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Iterator, List

import httpx
from django.db.models import Q
from django.utils import timezone

from . import events
from .models import WebhookDeliveryLog, WebhookSubscription

logger = logging.getLogger("golden_erp.webhooks")


def sign_payload(secret: str, payload_bytes: bytes) -> str:
    """Compute HMAC-SHA256 hex digest for outbound payload verification."""
    mac = hmac.new(secret.encode("utf-8"), msg=payload_bytes, digestmod=hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def verify_signature(
    secret: str,
    payload_bytes: bytes,
    signature_header: str | None,
    previous_secret: str = "",
) -> bool:
    """Validate incoming HMAC-SHA256 signature using constant-time comparison.

    Missing/malformed headers are a clean False, never an exception.
    When ``previous_secret`` is set (rotation window), a signature made
    with either secret verifies.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = sign_payload(secret, payload_bytes)
    if hmac.compare_digest(expected, signature_header):
        return True
    if previous_secret:
        legacy = sign_payload(previous_secret, payload_bytes)
        return hmac.compare_digest(legacy, signature_header)
    return False


def is_fresh_timestamp(timestamp_str: str | None, max_age_s: float = 300.0) -> bool:
    """Reject replayed/future webhook timestamps outside the skew window."""
    if not timestamp_str:
        return False
    try:
        ts = timezone.datetime.fromisoformat(str(timestamp_str))
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = timezone.now()
    skew = (now - ts).total_seconds()
    return 0 <= skew <= max_age_s


def deliver_webhook(
    subscription: WebhookSubscription,
    event_type: str,
    payload: Dict[str, Any],
    timeout_s: float = 10.0,
    max_retries: int = 3,
    backoff_base_s: float = 1.0,
    delivery_id=None,
) -> WebhookDeliveryLog:
    """Send an HTTP POST request to the subscription endpoint and record delivery outcome.

    Retries receiver 5xx and transport errors with exponential backoff
    (``backoff_base_s * 2**attempt``); 2xx succeeds, 4xx fails fast with
    no retry. Every attempt count is persisted on the delivery log.

    Signal-driven deliveries come from the outbox (``webhooks.outbox``) and
    pass ``max_retries=0``: one request per run, never a sleep, with
    ``delivery_id`` (the ``webhooks.deliver`` row, stable across retries)
    sent as ``X-Golden-Delivery-Id``. The ping makes one attempt too.
    """
    envelope = {
        "event": event_type,
        "timestamp": timezone.now().isoformat(),
        "subscription_id": str(subscription.id),
        "data": payload,
    }
    payload_json = json.dumps(envelope, default=str)
    payload_bytes = payload_json.encode("utf-8")
    signature = sign_payload(subscription.secret, payload_bytes)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Fincas-Webhooks/1.0",
        "X-Golden-Event": event_type,
        "X-Golden-Signature": signature,
        "X-Golden-Timestamp": envelope["timestamp"],
        "X-Golden-Delivery": str(subscription.id),
    }
    if delivery_id is not None:
        headers["X-Golden-Delivery-Id"] = str(delivery_id)
    if subscription.headers:
        headers.update(subscription.headers)

    start_time = time.time()
    response_code = None
    response_body = ""
    success = False
    error_message = ""
    attempts = 0

    try:
        with httpx.Client(timeout=timeout_s) as client:
            for attempt in range(max_retries + 1):
                attempts = attempt + 1
                try:
                    resp = client.post(
                        subscription.target_url,
                        content=payload_bytes,
                        headers=headers,
                    )
                except Exception as exc:
                    error_message = str(exc)
                    logger.warning(
                        "Webhook delivery failed for %s to %s: %s",
                        event_type,
                        subscription.target_url,
                        exc,
                    )
                    if attempt < max_retries:
                        time.sleep(backoff_base_s * (2**attempt))
                        continue
                    break
                response_code = resp.status_code
                response_body = resp.text[:2000]  # Cap recorded response size
                if 200 <= resp.status_code < 300:
                    success = True
                    error_message = ""
                    break
                if 500 <= resp.status_code < 600 and attempt < max_retries:
                    error_message = f"Receiver returned {resp.status_code}; retrying"
                    time.sleep(backoff_base_s * (2**attempt))
                    continue
                # 4xx (or exhausted 5xx): fail fast, no retry.
                success = False
                if not (500 <= resp.status_code < 600):
                    error_message = ""
                break
    except Exception as exc:
        error_message = str(exc)
        logger.warning(
            "Webhook delivery failed for %s to %s: %s",
            event_type,
            subscription.target_url,
            exc,
        )

    duration_ms = int((time.time() - start_time) * 1000)

    return WebhookDeliveryLog.objects.create(
        subscription=subscription,
        event_type=event_type,
        payload=envelope,
        response_status_code=response_code,
        response_body=response_body,
        duration_ms=duration_ms,
        attempts=attempts,
        success=success,
        error_message=error_message,
        delivery_id=delivery_id,
    )


def ping(subscription: WebhookSubscription, *, triggered_by: str = "api") -> WebhookDeliveryLog:
    """An operator's test delivery: one attempt, no sleeps, and its own
    ``X-Golden-Delivery-Id`` (a fresh uuid, stored on the log row) so a
    receiver that dedupes by that header treats each test as one delivery.
    Observed 2026-10-17 (harness R10.09): the ping carried no id, so a
    repeated test could not be told from a repeat."""
    import uuid

    return deliver_webhook(
        subscription=subscription,
        event_type="webhook.ping",
        payload={"message": "Fincas webhook ping check.", "triggered_by": triggered_by},
        timeout_s=5.0,
        max_retries=0,
        delivery_id=uuid.uuid4(),
    )


def matching_subscriptions(event_type: str, company=None) -> Iterator[tuple[WebhookSubscription, int]]:
    """``(subscription, payload version)`` for every active subscription that
    receives ``event_type`` from ``company``.

    Company-scoped subscriptions only ever receive their own company's
    events. An event with no company (a portal customer, a company-less
    warehouse) fails closed: it reaches global (company-less) subscriptions
    only, never every tenant's endpoint.
    """
    qs = WebhookSubscription.objects.filter(is_active=True)
    if company:
        qs = qs.filter(Q(company=company) | Q(company__isnull=True))
    else:
        qs = qs.filter(company__isnull=True)
    for sub in qs.order_by("created_at", "pk"):
        try:
            version = sub.event_version(event_type)
        except Exception:  # noqa: BLE001 - one corrupt row must not silence the others
            logger.exception("Webhook subscription %s has unreadable subscribed_events; skipped", sub.pk)
            continue
        if version is None:
            continue
        if version in events.schema(event_type).deprecated:
            logger.warning(
                "Webhook subscription %s receives deprecated %s@%s; pin a newer version "
                "(docs/webhooks.md section 1.1)",
                sub.pk, event_type, version,
            )
        yield sub, version


def dispatch_event(
    event_type: str,
    payload: Dict[str, Any],
    company=None,
) -> List[WebhookDeliveryLog]:
    """Deliver ``event_type`` to every matching subscription now, in this thread.

    Not used by lifecycle signals any more: those go through the outbox
    (``webhooks.outbox``), one row per subscription, retried on their own.
    Kept for scripts and tests that want a synchronous fan-out.

    ``payload`` is the event's latest schema; each subscriber receives the
    version it subscribes to (``webhooks.events.payload_for``).
    """
    return [
        deliver_webhook(sub, event_type, events.payload_for(event_type, payload, version))
        for sub, version in matching_subscriptions(event_type, company)
    ]
