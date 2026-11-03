"""Manual / Offline Payment Gateway reference implementation."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from .base import BasePaymentGateway, GatewayResult


class ManualPaymentGateway(BasePaymentGateway):
    """Reference gateway for cash, checks, wire transfers, and net-terms invoicing."""

    gateway_id = "manual"
    display_name = "Manual / Offline Payment"
    supports_refunds = True
    supports_auth_capture = False

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_source: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> GatewayResult:
        tx_id = f"MANUAL-{uuid.uuid4().hex[:12].upper()}"
        return GatewayResult.successful(
            transaction_id=tx_id,
            amount=amount,
            currency=currency,
            status="settled",
            raw_response={
                "gateway": "manual",
                "payment_source": payment_source,
                "metadata": metadata or {},
            },
        )

    def refund(
        self,
        transaction_id: str,
        amount: Optional[Decimal] = None,
        reason: str = "",
    ) -> GatewayResult:
        refund_id = f"REF-{uuid.uuid4().hex[:12].upper()}"
        return GatewayResult.successful(
            transaction_id=refund_id,
            amount=amount or Decimal("0.00"),
            status="refunded",
            raw_response={
                "original_transaction_id": transaction_id,
                "reason": reason,
            },
        )
