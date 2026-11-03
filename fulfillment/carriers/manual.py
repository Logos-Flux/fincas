"""Manual / Customer Pickup / Fleet delivery carrier implementation."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import List

from .base import (
    BaseShippingCarrier,
    LabelResult,
    Parcel,
    ShippingAddress,
    ShippingRate,
    TrackingInfo,
)


class ManualCarrier(BaseShippingCarrier):
    """Reference carrier for customer will-call pickup or internal freight delivery."""

    carrier_id = "manual"
    display_name = "Local Delivery / Customer Pickup"

    def get_rates(
        self,
        from_address: ShippingAddress,
        to_address: ShippingAddress,
        parcel: Parcel,
    ) -> List[ShippingRate]:
        return [
            ShippingRate(
                carrier_id=self.carrier_id,
                service_name="Customer Pickup (Will-Call)",
                service_code="PICKUP",
                rate=Decimal("0.00"),
                estimated_days=0,
            ),
            ShippingRate(
                carrier_id=self.carrier_id,
                service_name="Local Delivery (Internal Fleet)",
                service_code="LOCAL_FLEET",
                rate=Decimal("25.00"),
                estimated_days=1,
            ),
        ]

    def create_label(
        self,
        from_address: ShippingAddress,
        to_address: ShippingAddress,
        parcel: Parcel,
        service_code: str,
        label_format: str = "ZPL",
    ) -> LabelResult:
        tracking = f"MAN-{uuid.uuid4().hex[:10].upper()}"
        rate = Decimal("0.00") if service_code == "PICKUP" else Decimal("25.00")
        return LabelResult(
            success=True,
            tracking_number=tracking,
            label_url="",
            label_data="",
            label_format=label_format,
            postage_cost=rate,
            shipment_id=tracking,
        )

    def track(self, tracking_number: str) -> TrackingInfo:
        return TrackingInfo(
            carrier_id=self.carrier_id,
            tracking_number=tracking_number,
            status="delivered" if tracking_number.startswith("MAN-") else "unknown",
            status_details="Managed locally by warehouse staff.",
        )
