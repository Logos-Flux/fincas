"""Pluggable Payment Gateway interfaces and data transfer objects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class GatewayResult:
    """Standardized response from any payment gateway invocation."""

    success: bool
    transaction_id: str = ""
    amount: Decimal = Decimal("0.00")
    currency: str = "USD"
    status: str = "pending"
    error_code: str = ""
    error_message: str = ""
    raw_response: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(
        cls,
        error_message: str,
        error_code: str = "GATEWAY_ERROR",
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> GatewayResult:
        return cls(
            success=False,
            error_code=error_code,
            error_message=error_message,
            raw_response=raw_response or {},
        )

    @classmethod
    def successful(
        cls,
        transaction_id: str,
        amount: Decimal,
        currency: str = "USD",
        status: str = "settled",
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> GatewayResult:
        return cls(
            success=True,
            transaction_id=transaction_id,
            amount=amount,
            currency=currency.upper(),
            status=status,
            raw_response=raw_response or {},
        )


class BasePaymentGateway(ABC):
    """Abstract interface for all Fincas payment gateways."""

    gateway_id: str = ""
    display_name: str = ""
    supports_refunds: bool = True
    supports_auth_capture: bool = True

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}

    @abstractmethod
    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_source: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> GatewayResult:
        """Immediately authorize and capture funds."""
        pass

    def authorize(
        self,
        amount: Decimal,
        currency: str,
        payment_source: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> GatewayResult:
        """Reserve funds on the payment method without capturing."""
        raise NotImplementedError(f"{self.display_name} does not support separate authorization.")

    def capture(
        self,
        transaction_id: str,
        amount: Optional[Decimal] = None,
    ) -> GatewayResult:
        """Capture previously authorized funds."""
        raise NotImplementedError(f"{self.display_name} does not support capture.")

    def refund(
        self,
        transaction_id: str,
        amount: Optional[Decimal] = None,
        reason: str = "",
    ) -> GatewayResult:
        """Refund a previously captured transaction."""
        raise NotImplementedError(f"{self.display_name} does not support refunds.")

    def void(
        self,
        transaction_id: str,
    ) -> GatewayResult:
        """Cancel an uncaptured authorization."""
        raise NotImplementedError(f"{self.display_name} does not support voiding transactions.")
