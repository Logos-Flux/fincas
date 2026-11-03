"""Stripe Payment Gateway implementation for Fincas."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from django.conf import settings

from .base import BasePaymentGateway, GatewayResult

logger = logging.getLogger("golden_erp.payments.stripe")


class StripePaymentGateway(BasePaymentGateway):
    """Stripe payment processing for cards and online payment methods."""

    gateway_id = "stripe"
    display_name = "Stripe Payments"
    supports_refunds = True
    supports_auth_capture = True

    def _get_api_key(self) -> str:
        key = self.config.get("api_key") or getattr(settings, "STRIPE_SECRET_KEY", "")
        return str(key).strip()

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_source: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> GatewayResult:
        api_key = self._get_api_key()
        if not api_key:
            # If in test/dev mode without key, return mock success or explicit configuration error
            if getattr(settings, "DEBUG", False) or getattr(settings, "STRIPE_MOCK_MODE", False):
                mock_id = f"ch_mock_{uuid.uuid4().hex[:16]}"
                return GatewayResult.successful(
                    transaction_id=mock_id,
                    amount=amount,
                    currency=currency,
                    status="captured",
                    raw_response={"mock": True, "source": payment_source, "metadata": metadata or {}},
                )
            return GatewayResult.failure("STRIPE_SECRET_KEY is not configured.", error_code="NOT_CONFIGURED")

        # In production with live API key:
        import httpx

        try:
            # Convert dollars/units to integer cents
            cents = int((amount * 100).to_integral_value())
            resp = httpx.post(
                "https://api.stripe.com/v1/payment_intents",
                auth=(api_key, ""),
                data={
                    "amount": str(cents),
                    "currency": currency.lower(),
                    "payment_method": payment_source,
                    "confirm": "true",
                    "off_session": "true",
                },
                timeout=15.0,
            )
            data = resp.json()
            if resp.status_code in (200, 201) and data.get("status") in ("succeeded", "processing"):
                return GatewayResult.successful(
                    transaction_id=data.get("id", ""),
                    amount=amount,
                    currency=currency,
                    status=data.get("status", "succeeded"),
                    raw_response=data,
                )
            error_data = data.get("error", {})
            return GatewayResult.failure(
                error_message=error_data.get("message", "Stripe payment failed."),
                error_code=error_data.get("code", "STRIPE_ERROR"),
                raw_response=data,
            )
        except Exception as exc:
            logger.exception("Error processing Stripe charge")
            return GatewayResult.failure(str(exc), error_code="STRIPE_EXCEPTION")

    def authorize(
        self,
        amount: Decimal,
        currency: str,
        payment_source: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> GatewayResult:
        """Reserve funds without capturing (manual-capture PaymentIntent)."""
        api_key = self._get_api_key()
        if not api_key:
            if getattr(settings, "DEBUG", False) or getattr(settings, "STRIPE_MOCK_MODE", False):
                return GatewayResult.successful(
                    transaction_id=f"pi_mock_{uuid.uuid4().hex[:16]}",
                    amount=amount,
                    currency=currency,
                    status="authorized",
                    raw_response={"mock": True, "capture_method": "manual"},
                )
            return GatewayResult.failure("STRIPE_SECRET_KEY is not configured.", error_code="NOT_CONFIGURED")

        import httpx

        try:
            cents = int((amount * 100).to_integral_value())
            resp = httpx.post(
                "https://api.stripe.com/v1/payment_intents",
                auth=(api_key, ""),
                data={
                    "amount": str(cents),
                    "currency": currency.lower(),
                    "payment_method": payment_source,
                    "capture_method": "manual",
                    "confirm": "true",
                    "off_session": "true",
                },
                timeout=15.0,
            )
            data = resp.json()
            if resp.status_code in (200, 201) and data.get("status") in (
                "requires_capture", "processing",
            ):
                return GatewayResult.successful(
                    transaction_id=data.get("id", ""),
                    amount=amount,
                    currency=currency,
                    status="authorized",
                    raw_response=data,
                )
            error_data = data.get("error", {})
            return GatewayResult.failure(
                error_message=error_data.get("message", "Stripe authorization failed."),
                error_code=error_data.get("code", "STRIPE_ERROR"),
                raw_response=data,
            )
        except Exception:
            logger.exception("Error processing Stripe authorization")
            return GatewayResult.failure("Stripe authorization failed.", error_code="STRIPE_EXCEPTION")

    def capture(
        self,
        transaction_id: str,
        amount: Optional[Decimal] = None,
    ) -> GatewayResult:
        """Capture a previously authorized PaymentIntent."""
        api_key = self._get_api_key()
        if not api_key:
            if getattr(settings, "DEBUG", False) or getattr(settings, "STRIPE_MOCK_MODE", False):
                return GatewayResult.successful(
                    transaction_id=transaction_id,
                    amount=amount or Decimal("0.00"),
                    status="captured",
                    raw_response={"mock": True},
                )
            return GatewayResult.failure("STRIPE_SECRET_KEY is not configured.", error_code="NOT_CONFIGURED")

        import httpx

        try:
            payload: Dict[str, Any] = {}
            if amount is not None:
                payload["amount_to_capture"] = str(int((amount * 100).to_integral_value()))
            resp = httpx.post(
                f"https://api.stripe.com/v1/payment_intents/{transaction_id}/capture",
                auth=(api_key, ""),
                data=payload,
                timeout=15.0,
            )
            data = resp.json()
            if resp.status_code in (200, 201) and data.get("status") in (
                "succeeded", "processing",
            ):
                return GatewayResult.successful(
                    transaction_id=data.get("id", transaction_id),
                    amount=amount or Decimal("0.00"),
                    status="captured",
                    raw_response=data,
                )
            return GatewayResult.failure(
                error_message=data.get("error", {}).get("message", "Stripe capture failed."),
                raw_response=data,
            )
        except Exception:
            logger.exception("Error executing Stripe capture")
            return GatewayResult.failure("Stripe capture failed.", error_code="STRIPE_EXCEPTION")

    def refund(
        self,
        transaction_id: str,
        amount: Optional[Decimal] = None,
        reason: str = "",
    ) -> GatewayResult:
        api_key = self._get_api_key()
        if not api_key:
            if getattr(settings, "DEBUG", False) or getattr(settings, "STRIPE_MOCK_MODE", False):
                return GatewayResult.successful(
                    transaction_id=f"re_mock_{uuid.uuid4().hex[:16]}",
                    amount=amount or Decimal("0.00"),
                    status="refunded",
                    raw_response={"mock": True, "reason": reason},
                )
            return GatewayResult.failure("STRIPE_SECRET_KEY is not configured.", error_code="NOT_CONFIGURED")

        import httpx

        try:
            payload: Dict[str, Any] = {"payment_intent": transaction_id}
            if amount is not None:
                payload["amount"] = str(int((amount * 100).to_integral_value()))
            if reason:
                payload["reason"] = "requested_by_customer"

            resp = httpx.post(
                "https://api.stripe.com/v1/refunds",
                auth=(api_key, ""),
                data=payload,
                timeout=15.0,
            )
            data = resp.json()
            if resp.status_code in (200, 201) and data.get("status") in ("succeeded", "pending"):
                return GatewayResult.successful(
                    transaction_id=data.get("id", ""),
                    amount=amount or Decimal("0.00"),
                    status="refunded",
                    raw_response=data,
                )
            return GatewayResult.failure(
                error_message=data.get("error", {}).get("message", "Stripe refund failed."),
                raw_response=data,
            )
        except Exception as exc:
            logger.exception("Error executing Stripe refund")
            return GatewayResult.failure(str(exc), error_code="STRIPE_EXCEPTION")
