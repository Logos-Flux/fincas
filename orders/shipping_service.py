"""
Shipping rate service — aggregates product dims and fetches live rates
from the storefront shipping APIs (EasyPost for parcel, uShip for LTL).
"""

import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

STOREFRONT_BASE = getattr(settings, "STOREFRONT_URL", "")

# Thresholds matching the storefront's calculateShippingProfile()
LTL_WEIGHT_THRESHOLD = 150  # lbs
LTL_DIMENSION_THRESHOLD = 48  # inches


@dataclass
class ShippingItem:
    """Shipping-relevant data for one line item."""
    sku: str = ""
    title: str = ""
    quantity: int = 1
    weight_lbs: float = 0.0
    length_in: float = 0.0
    width_in: float = 0.0
    height_in: float = 0.0
    ships_usps: bool | None = None
    ships_ups: bool | None = None
    ships_ltl: bool | None = None
    ships_pickup: bool | None = None
    hazmat_flag: bool | None = None
    oversized_flag: bool | None = None


@dataclass
class ShippingProfile:
    """Aggregated shipping profile for a set of items."""
    total_weight_lbs: float = 0.0
    max_length_in: float = 0.0
    max_width_in: float = 0.0
    total_height_in: float = 0.0
    total_items: int = 0
    requires_ltl: bool = False
    has_hazmat: bool = False
    has_oversized: bool = False
    usps_eligible: bool = False
    ups_eligible: bool = False
    ltl_eligible: bool = False
    pickup_available: bool = False
    items_missing_dims: list = field(default_factory=list)


@dataclass
class ShippingRate:
    """One shipping rate option."""
    carrier: str = ""
    service: str = ""
    rate: float = 0.0
    transit_days: int | None = None
    delivery_date: str = ""
    guaranteed: bool = False
    source: str = ""  # "easypost", "uship", "ltl_fallback"


def build_shipping_items(order) -> list[ShippingItem]:
    """Extract ShippingItems from an Order's line items."""
    items = []
    for li in order.line_items.select_related("variant__product").all():
        if not li.variant:
            continue
        variant = li.variant
        product = variant.product

        # Use shipping_* dims (variant overrides product if set)
        weight = _f(variant.shipping_weight) or _f(product.shipping_weight) or 0.0
        length = _f(variant.shipping_length) or _f(product.shipping_length) or 0.0
        width = _f(variant.shipping_width) or _f(product.shipping_width) or 0.0
        height = _f(variant.shipping_height) or _f(product.shipping_height) or 0.0

        # Carrier flags — variant overrides product (None = inherit)
        def _flag(variant_val, product_val):
            return variant_val if variant_val is not None else product_val

        items.append(ShippingItem(
            sku=variant.sku,
            title=product.title,
            quantity=int(li.quantity),
            weight_lbs=weight,
            length_in=length,
            width_in=width,
            height_in=height,
            ships_usps=_flag(variant.ships_usps, product.ships_usps),
            ships_ups=_flag(variant.ships_ups, product.ships_ups),
            ships_ltl=_flag(variant.ships_ltl, product.ships_ltl),
            ships_pickup=_flag(variant.ships_pickup, product.ships_pickup),
            hazmat_flag=_flag(variant.hazmat_flag, product.hazmat_flag),
            oversized_flag=_flag(variant.oversized_flag, product.oversized_flag),
        ))
    return items


def calculate_shipping_profile(items: list[ShippingItem]) -> ShippingProfile:
    """
    Aggregate items into a shipping profile.
    Mirrors the storefront's calculateShippingProfile() logic.
    """
    profile = ShippingProfile()
    has_any_flags = False

    for item in items:
        qty = item.quantity
        profile.total_items += qty
        profile.total_weight_lbs += item.weight_lbs * qty
        profile.max_length_in = max(profile.max_length_in, item.length_in)
        profile.max_width_in = max(profile.max_width_in, item.width_in)
        profile.total_height_in += item.height_in * qty

        if item.hazmat_flag:
            profile.has_hazmat = True
        if item.oversized_flag:
            profile.has_oversized = True

        # Track items missing dims
        if item.weight_lbs == 0 and item.length_in == 0:
            profile.items_missing_dims.append(item.sku)

        # Check if any item has explicit carrier flags
        if any(f is not None for f in [item.ships_usps, item.ships_ups, item.ships_ltl, item.ships_pickup]):
            has_any_flags = True

    if has_any_flags:
        # Explicit flags: AND logic for parcel carriers, OR logic for LTL/pickup
        usps_ok = all(i.ships_usps is True for i in items if i.ships_usps is not None) and any(i.ships_usps is True for i in items)
        ups_ok = all(i.ships_ups is True for i in items if i.ships_ups is not None) and any(i.ships_ups is True for i in items)
        ltl_ok = any(i.ships_ltl is True for i in items)
        pickup_ok = any(i.ships_pickup is True for i in items)
        profile.usps_eligible = usps_ok and not profile.has_hazmat
        profile.ups_eligible = ups_ok and not profile.has_hazmat
        profile.ltl_eligible = ltl_ok
        profile.pickup_available = pickup_ok
    else:
        # Fallback detection based on weight/dimensions
        profile.ups_eligible = (
            profile.total_weight_lbs <= 70
            and profile.max_length_in <= 48
            and profile.max_width_in <= 48
            and profile.total_height_in <= 48
            and not profile.has_hazmat
        )
        profile.usps_eligible = False  # Strict size limits, disabled for fallback
        profile.pickup_available = True
        profile.ltl_eligible = True

    # Determine if LTL is required
    profile.requires_ltl = (
        profile.total_weight_lbs > LTL_WEIGHT_THRESHOLD
        or profile.max_length_in > LTL_DIMENSION_THRESHOLD
        or profile.max_width_in > LTL_DIMENSION_THRESHOLD
        or profile.total_height_in > LTL_DIMENSION_THRESHOLD
        or profile.has_oversized
    )

    return profile


def get_shipping_rates(
    order,
    warehouse=None,
    destination_address=None,
) -> dict:
    """
    Fetch live shipping rates for an order.

    Args:
        order: Order instance with line_items
        warehouse: Warehouse to ship from (defaults to order's or company default)
        destination_address: CustomerAddress for ship-to (defaults to order.shipping_address)

    Returns:
        dict with keys: profile, rates, warnings, ship_from
    """
    from inventory.models import Warehouse as WarehouseModel

    # Resolve ship-from warehouse
    if warehouse is None:
        warehouse = WarehouseModel.objects.filter(
            can_fulfill=True, is_active=True,
        ).order_by("-default_for_company").first()

    if not warehouse:
        return {"error": "No fulfillment warehouse configured", "rates": [], "warnings": []}

    # Resolve destination
    dest = destination_address or order.shipping_address
    if not dest:
        return {"error": "No shipping address on order", "rates": [], "warnings": []}

    # Build shipping profile
    items = build_shipping_items(order)
    if not items:
        return {"error": "No line items with variants", "rates": [], "warnings": []}

    profile = calculate_shipping_profile(items)
    warnings = []

    if profile.items_missing_dims:
        warnings.append(f"Missing shipping dims for: {', '.join(profile.items_missing_dims)}")

    rates = []

    # Build from/to address data
    from_addr = {
        "street1": warehouse.address_line_1,
        "city": warehouse.city,
        "state": warehouse.state_province,
        "zip": warehouse.postal_code,
        "country": warehouse.country or "US",
    }
    to_addr = {
        "street1": dest.address_line_1,
        "city": dest.city,
        "state": dest.state_province,
        "zip": dest.postal_code,
        "country": dest.country or "US",
    }

    # Fetch parcel rates (EasyPost) if eligible and not LTL-required
    should_get_parcel = (
        (profile.usps_eligible or profile.ups_eligible)
        and not profile.requires_ltl
        and not profile.has_hazmat
    )
    if should_get_parcel:
        parcel_rates = _fetch_parcel_rates(from_addr, to_addr, profile)
        rates.extend(parcel_rates)

    # Fetch LTL rates (uShip) if LTL-eligible
    if profile.requires_ltl or profile.ltl_eligible:
        ltl_rates = _fetch_ltl_rates(from_addr, to_addr, profile)
        rates.extend(ltl_rates)

    # Always add pickup option
    if profile.pickup_available or not rates:
        rates.append(ShippingRate(
            carrier="Will Call",
            service="Customer Pickup",
            rate=0.0,
            transit_days=None,
            source="pickup",
        ))

    # Sort by rate (pickup last since it's $0 but optional)
    rates.sort(key=lambda r: (r.source == "pickup", r.rate))

    return {
        "profile": {
            "total_weight_lbs": round(profile.total_weight_lbs, 1),
            "max_length_in": round(profile.max_length_in, 1),
            "max_width_in": round(profile.max_width_in, 1),
            "total_height_in": round(profile.total_height_in, 1),
            "total_items": profile.total_items,
            "requires_ltl": profile.requires_ltl,
            "has_hazmat": profile.has_hazmat,
            "has_oversized": profile.has_oversized,
            "usps_eligible": profile.usps_eligible,
            "ups_eligible": profile.ups_eligible,
        },
        "ship_from": {
            "name": warehouse.ship_from_label or warehouse.name,
            "city": warehouse.city,
            "state": warehouse.state_province,
            "zip": warehouse.postal_code,
        },
        "rates": [
            {
                "carrier": r.carrier,
                "service": r.service,
                "rate": r.rate,
                "transit_days": r.transit_days,
                "delivery_date": r.delivery_date,
                "guaranteed": r.guaranteed,
                "source": r.source,
            }
            for r in rates
        ],
        "warnings": warnings,
    }


def _fetch_parcel_rates(from_addr: dict, to_addr: dict, profile: ShippingProfile) -> list[ShippingRate]:
    """Call the storefront's EasyPost parcel rate endpoint."""
    weight_oz = round(profile.total_weight_lbs * 16)
    payload = {
        "from_address": from_addr,
        "to_address": to_addr,
        "parcel": {
            "length": profile.max_length_in or 12,
            "width": profile.max_width_in or 12,
            "height": profile.total_height_in or 6,
            "weight": weight_oz or 16,  # default 1 lb
        },
    }

    try:
        resp = requests.post(
            f"{STOREFRONT_BASE}/api/shipping/rates",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("EasyPost rate fetch failed: %s", e)
        return []

    if not data.get("success"):
        logger.warning("EasyPost returned error: %s", data)
        return []

    rates = []
    for r in data.get("rates", []):
        carrier = (r.get("carrier") or "").upper()
        service = r.get("service", "")

        # Filter to Ground services only (matching storefront logic)
        if carrier in ("USPS", "USPSRETURN") and "GroundAdvantage" not in service:
            continue
        if carrier in ("UPS", "UPSDAP") and "Ground" not in service:
            continue

        # Check eligibility
        if carrier in ("USPS", "USPSRETURN") and not profile.usps_eligible:
            continue
        if carrier in ("UPS", "UPSDAP") and not profile.ups_eligible:
            continue

        rates.append(ShippingRate(
            carrier=carrier,
            service=service,
            rate=float(r.get("rate", 0)),
            transit_days=r.get("delivery_days"),
            delivery_date=r.get("delivery_date", ""),
            guaranteed=r.get("delivery_date_guaranteed", False),
            source="easypost",
        ))

    return rates


def _fetch_ltl_rates(from_addr: dict, to_addr: dict, profile: ShippingProfile) -> list[ShippingRate]:
    """Call the storefront's uShip LTL rate endpoint."""
    payload = {
        "from_zip": from_addr["zip"],
        "from_city": from_addr["city"],
        "from_state": from_addr["state"],
        "to_zip": to_addr["zip"],
        "to_city": to_addr["city"],
        "to_state": to_addr["state"],
        "weight_lbs": profile.total_weight_lbs or 100,
        "length_in": profile.max_length_in or 48,
        "width_in": profile.max_width_in or 40,
        "height_in": profile.total_height_in or 48,
        "liftgate_delivery": True,  # default for most customers
        "residential_delivery": False,
    }

    try:
        resp = requests.post(
            f"{STOREFRONT_BASE}/api/shipping/ltl-rates",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("uShip LTL rate fetch failed: %s", e)
        return []

    if not data.get("success"):
        logger.warning("uShip returned error: %s", data)
        return []

    rates = []
    for q in data.get("quotes", [])[:5]:  # Top 5 quotes
        rates.append(ShippingRate(
            carrier=q.get("carrier", "LTL Freight"),
            service="LTL Freight",
            rate=float(q.get("rate_with_markup", q.get("base_rate", 0))),
            transit_days=q.get("transit_days"),
            delivery_date=q.get("estimated_delivery", ""),
            guaranteed=q.get("guaranteed", False),
            source="uship",
        ))

    return rates


def _f(val) -> float:
    """Convert Decimal/None to float."""
    if val is None:
        return 0.0
    return float(val)
