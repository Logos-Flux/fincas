"""Pluggable Shipping Carrier interfaces and data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ShippingAddress:
    """Standard normalized shipping address."""

    name: str = ""
    company: str = ""
    street1: str = ""
    street2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "US"
    phone: str = ""
    email: str = ""


@dataclass(frozen=True)
class Parcel:
    """Standard dimensions and weight for a package."""

    weight_oz: Decimal = Decimal("0.0")
    length_in: Decimal = Decimal("0.0")
    width_in: Decimal = Decimal("0.0")
    height_in: Decimal = Decimal("0.0")


@dataclass(frozen=True)
class ShippingRate:
    """Quoted rate for a shipping service."""

    carrier_id: str
    service_name: str
    service_code: str
    rate: Decimal
    currency: str = "USD"
    estimated_days: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LabelResult:
    """Result of a label generation and postage purchase."""

    success: bool
    tracking_number: str = ""
    label_url: str = ""
    label_data: str = ""  # ZPL, base64, or raw content
    label_format: str = "ZPL"
    postage_cost: Decimal = Decimal("0.00")
    shipment_id: str = ""
    error: str = ""
    raw_response: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, error_message: str, raw_response: Optional[Dict[str, Any]] = None) -> LabelResult:
        return cls(success=False, error=error_message, raw_response=raw_response or {})


@dataclass(frozen=True)
class TrackingInfo:
    """Current tracking status and history for a parcel."""

    carrier_id: str
    tracking_number: str
    status: str  # e.g., 'pre_transit', 'in_transit', 'delivered', 'returned'
    status_details: str = ""
    estimated_delivery: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)


class BaseShippingCarrier(ABC):
    """Abstract interface for all shipping carriers in Fincas."""

    carrier_id: str = ""
    display_name: str = ""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}

    @abstractmethod
    def get_rates(
        self,
        from_address: ShippingAddress,
        to_address: ShippingAddress,
        parcel: Parcel,
    ) -> List[ShippingRate]:
        """Obtain estimated shipping rates across available service levels."""
        pass

    @abstractmethod
    def create_label(
        self,
        from_address: ShippingAddress,
        to_address: ShippingAddress,
        parcel: Parcel,
        service_code: str,
        label_format: str = "ZPL",
    ) -> LabelResult:
        """Buy postage and generate shipping label for parcel."""
        pass

    def track(self, tracking_number: str) -> TrackingInfo:
        """Fetch tracking status for a shipment."""
        raise NotImplementedError(f"{self.display_name} does not support live tracking query.")

    def void_label(self, shipment_id: str) -> bool:
        """Cancel purchased postage."""
        raise NotImplementedError(f"{self.display_name} does not support voiding labels.")
