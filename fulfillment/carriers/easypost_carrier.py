"""EasyPost shipping carrier implementation."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import List

from django.conf import settings

from .base import (
    BaseShippingCarrier,
    LabelResult,
    Parcel,
    ShippingAddress,
    ShippingRate,
)

logger = logging.getLogger("golden_erp.fulfillment.easypost")


class EasyPostCarrier(BaseShippingCarrier):
    """EasyPost integration for USPS, UPS, FedEx, and regional parcel carriers."""

    carrier_id = "easypost"
    display_name = "EasyPost Multi-Carrier"

    def _get_client(self):
        api_key = self.config.get("api_key") or getattr(settings, "EASYPOST_API_KEY", None)
        if not api_key:
            return None
        import easypost
        return easypost.EasyPostClient(api_key)

    def get_rates(
        self,
        from_address: ShippingAddress,
        to_address: ShippingAddress,
        parcel: Parcel,
    ) -> List[ShippingRate]:
        client = self._get_client()
        if not client:
            if getattr(settings, "DEBUG", False) or getattr(settings, "EASYPOST_MOCK_MODE", False):
                return [
                    ShippingRate(
                        carrier_id=self.carrier_id,
                        service_name="USPS Ground Advantage (Mock)",
                        service_code="GroundAdvantage",
                        rate=Decimal("6.50"),
                        estimated_days=3,
                    ),
                    ShippingRate(
                        carrier_id=self.carrier_id,
                        service_name="USPS Priority Mail (Mock)",
                        service_code="Priority",
                        rate=Decimal("9.85"),
                        estimated_days=2,
                    ),
                ]
            raise ValueError("EASYPOST_API_KEY is not configured.")

        try:
            shipment = client.shipment.create(
                from_address={
                    "company": from_address.company,
                    "street1": from_address.street1,
                    "city": from_address.city,
                    "state": from_address.state,
                    "zip": from_address.postal_code,
                    "country": from_address.country,
                },
                to_address={
                    "name": to_address.name,
                    "company": to_address.company,
                    "street1": to_address.street1,
                    "city": to_address.city,
                    "state": to_address.state,
                    "zip": to_address.postal_code,
                    "country": to_address.country,
                },
                parcel={
                    "weight": float(parcel.weight_oz),
                    "length": float(parcel.length_in),
                    "width": float(parcel.width_in),
                    "height": float(parcel.height_in),
                },
            )
            rates: List[ShippingRate] = []
            for r in getattr(shipment, "rates", []):
                rates.append(
                    ShippingRate(
                        carrier_id=self.carrier_id,
                        service_name=f"{r.carrier} {r.service}",
                        service_code=r.service,
                        rate=Decimal(str(r.rate)),
                        currency=r.currency or "USD",
                        estimated_days=getattr(r, "est_delivery_days", None),
                        metadata={"rate_id": r.id},
                    )
                )
            return rates
        except Exception as exc:
            logger.exception("Failed to query EasyPost rates: %s", exc)
            return []

    def create_label(
        self,
        from_address: ShippingAddress,
        to_address: ShippingAddress,
        parcel: Parcel,
        service_code: str,
        label_format: str = "ZPL",
    ) -> LabelResult:
        client = self._get_client()
        if not client:
            if getattr(settings, "DEBUG", False) or getattr(settings, "EASYPOST_MOCK_MODE", False):
                mock_tracking = f"940011189956{uuid.uuid4().hex[:10].upper()}"
                return LabelResult(
                    success=True,
                    tracking_number=mock_tracking,
                    label_url="https://example.com/mock-label.pdf",
                    label_format=label_format,
                    postage_cost=Decimal("7.25"),
                    shipment_id=f"shp_mock_{uuid.uuid4().hex[:12]}",
                )
            return LabelResult.failure("EASYPOST_API_KEY is not configured.")

        try:
            shipment = client.shipment.create(
                from_address={
                    "company": from_address.company,
                    "street1": from_address.street1,
                    "city": from_address.city,
                    "state": from_address.state,
                    "zip": from_address.postal_code,
                    "country": from_address.country,
                },
                to_address={
                    "name": to_address.name,
                    "company": to_address.company,
                    "street1": to_address.street1,
                    "city": to_address.city,
                    "state": to_address.state,
                    "zip": to_address.postal_code,
                    "country": to_address.country,
                },
                parcel={
                    "weight": float(parcel.weight_oz),
                    "length": float(parcel.length_in),
                    "width": float(parcel.width_in),
                    "height": float(parcel.height_in),
                },
            )

            # Match lowest rate matching service_code
            matching_rate = None
            for rate in getattr(shipment, "rates", []):
                if rate.service.lower() == service_code.lower():
                    matching_rate = rate
                    break
            if not matching_rate and shipment.rates:
                matching_rate = shipment.lowest_rate()

            if not matching_rate:
                return LabelResult.failure(f"No valid rate found for service '{service_code}'.")

            bought = client.shipment.buy(shipment.id, rate=matching_rate)
            label_url = getattr(getattr(bought, "postage_label", None), "label_url", "")
            zpl = getattr(getattr(bought, "postage_label", None), "label_zpl_url", "")

            return LabelResult(
                success=True,
                tracking_number=getattr(bought, "tracking_code", ""),
                label_url=label_url,
                label_data=zpl or label_url,
                label_format=label_format,
                postage_cost=Decimal(str(matching_rate.rate)),
                shipment_id=bought.id,
                raw_response=bought.to_dict() if hasattr(bought, "to_dict") else {},
            )
        except Exception as exc:
            logger.exception("EasyPost label creation failed: %s", exc)
            return LabelResult.failure(str(exc))
