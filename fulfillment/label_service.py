"""
EasyPost label service — creates USPS labels and writes tracking back to SubOrder.

Usage:
    from fulfillment.label_service import create_label_for_pick_ticket, void_label

    result = create_label_for_pick_ticket(pick_ticket)
    if result["success"]:
        zpl = result["zpl"]   # send to Zebra printer
    else:
        print(result["error"])
"""

import logging
from decimal import Decimal

import easypost
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _get_client():
    api_key = getattr(settings, "EASYPOST_API_KEY", None)
    if not api_key:
        raise ValueError("EASYPOST_API_KEY not set in settings/env")
    return easypost.EasyPostClient(api_key)


def create_label_for_pick_ticket(pick_ticket, label_format="ZPL"):
    """
    Create an EasyPost shipment + purchase label for a PickTicket.

    Pulls ship-to from pick_ticket.sub_order, dims/weight from PickTicketLines.
    Writes tracking_number, shipping_label_url, easypost_shipment_id, actual_ship_date
    back to the SubOrder.

    Args:
        pick_ticket: PickTicket instance (with sub_order, lines prefetched)
        label_format: "ZPL" (default for thermal), "PDF", "PNG"

    Returns:
        dict with keys: success, tracking_number, zpl/label_url, postage_cost, error
    """
    sub_order = pick_ticket.sub_order
    if not sub_order:
        return {"success": False, "error": "Pick ticket has no sub_order"}

    # --- Resolve ship-to address ---
    order = sub_order.order
    ship_to = order.shipping_address if order else None
    if not ship_to:
        return {"success": False, "error": "No shipping address on order"}

    # --- Resolve ship-from warehouse ---
    warehouse = pick_ticket.warehouse or sub_order.warehouse
    if not warehouse:
        from inventory.models import Warehouse
        warehouse = Warehouse.objects.filter(can_fulfill=True, is_active=True).order_by("-default_for_company").first()
    if not warehouse:
        return {"success": False, "error": "No fulfillment warehouse configured"}

    # --- Aggregate weight/dims from pick ticket lines ---
    weight_oz, length, width, height = _aggregate_parcel_dims(pick_ticket)

    # --- Resolve service level ---
    service = sub_order.service_level or "GroundAdvantage"
    carrier_str = sub_order.carrier or "USPS"
    # Normalize common aliases
    service_map = {
        "ground": "GroundAdvantage",
        "ground advantage": "GroundAdvantage",
        "priority": "Priority",
        "priority mail": "Priority",
        "first class": "First",
        "express": "Express",
    }
    service = service_map.get(service.lower(), service)

    try:
        client = _get_client()

        # Build from/to address objects
        to_addr = client.address.create(
            name=getattr(ship_to, "attention_name", "") or str(order.customer),
            street1=ship_to.address_line_1,
            street2=getattr(ship_to, "address_line_2", "") or "",
            city=ship_to.city,
            state=ship_to.state_province,
            zip=ship_to.postal_code,
            country=getattr(ship_to, "country", "US") or "US",
            verify=["delivery"],
        )

        from_addr = client.address.create(
            company=warehouse.ship_from_label or warehouse.name or "Golden Logistics",
            street1=warehouse.address_line_1,
            city=warehouse.city,
            state=warehouse.state_province,
            zip=warehouse.postal_code,
            country=getattr(warehouse, "country", "US") or "US",
        )

        parcel = client.parcel.create(
            weight=weight_oz or 16,
            length=length or 12,
            width=width or 12,
            height=height or 6,
        )

        shipment = client.shipment.create(
            to_address=to_addr,
            from_address=from_addr,
            parcel=parcel,
            options={
                "label_format": label_format,
            },
        )

        # Buy lowest rate matching the requested carrier/service
        rate = _select_rate(shipment, carrier_str, service)
        purchased = client.shipment.buy(shipment.id, rate=rate)

        tracking_number = purchased.tracking_code
        postage_cost = Decimal(str(purchased.selected_rate.rate))
        label_url = purchased.postage_label.label_url
        zpl = getattr(purchased.postage_label, "label_zpl_url", None)

        # --- Write back to SubOrder ---
        sub_order.tracking_number = tracking_number
        sub_order.tracking_url = f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}"
        sub_order.shipping_label_url = label_url
        sub_order.carrier = purchased.selected_rate.carrier
        sub_order.service_level = purchased.selected_rate.service
        sub_order.shipping_cost_ex_tax = postage_cost
        sub_order.actual_ship_date = timezone.now()
        sub_order.status = "label_created"
        # Store EP shipment ID in metadata for void/refund later
        meta = sub_order.metadata or {}
        meta["easypost_shipment_id"] = purchased.id
        sub_order.metadata = meta
        sub_order.save(update_fields=[
            "tracking_number", "tracking_url", "shipping_label_url",
            "carrier", "service_level", "shipping_cost_ex_tax",
            "actual_ship_date", "status", "metadata",
        ])

        # Update pick ticket status
        pick_ticket.status = "shipped"
        pick_ticket.shipped_at = timezone.now()
        pick_ticket.save(update_fields=["status", "shipped_at"])

        logger.info(
            "Label created: pick_ticket=%s tracking=%s cost=$%s",
            pick_ticket.ticket_number, tracking_number, postage_cost,
        )

        # --- Auto-print to Zebra if configured ---
        if zpl:
            try:
                from fulfillment.printer import fetch_and_print_zpl_url
                fetch_and_print_zpl_url(zpl)
            except Exception as pe:
                logger.warning("Auto-print failed: %s", pe)

        return {
            "success": True,
            "tracking_number": tracking_number,
            "label_url": label_url,
            "zpl_url": zpl,
            "postage_cost": float(postage_cost),
            "carrier": purchased.selected_rate.carrier,
            "service": purchased.selected_rate.service,
            "easypost_shipment_id": purchased.id,
        }

    except easypost.errors.EasyPostError as e:
        logger.error("EasyPost error for pick_ticket %s: %s", pick_ticket.ticket_number, e)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("Unexpected error creating label for pick_ticket %s", pick_ticket.ticket_number)
        return {"success": False, "error": f"Unexpected error: {e}"}


def void_label(sub_order):
    """
    Void/refund a purchased label via EasyPost.

    Args:
        sub_order: SubOrder with metadata.easypost_shipment_id set

    Returns:
        dict with keys: success, message, error
    """
    ep_id = (sub_order.metadata or {}).get("easypost_shipment_id")
    if not ep_id:
        return {"success": False, "error": "No EasyPost shipment ID on record"}

    try:
        client = _get_client()
        client.shipment.refund(ep_id)

        # Clear tracking fields
        sub_order.tracking_number = ""
        sub_order.tracking_url = ""
        sub_order.shipping_label_url = ""
        sub_order.actual_ship_date = None
        sub_order.status = "packed"
        meta = sub_order.metadata or {}
        meta.pop("easypost_shipment_id", None)
        sub_order.metadata = meta
        sub_order.save(update_fields=[
            "tracking_number", "tracking_url", "shipping_label_url",
            "actual_ship_date", "status", "metadata",
        ])

        logger.info("Label voided for sub_order %s (EP: %s)", sub_order.sub_order_number, ep_id)
        return {"success": True, "message": f"Label voided for {sub_order.sub_order_number}"}

    except easypost.errors.EasyPostError as e:
        logger.error("EasyPost void error for %s: %s", sub_order.sub_order_number, e)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("Unexpected error voiding label for %s", sub_order.sub_order_number)
        return {"success": False, "error": f"Unexpected error: {e}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aggregate_parcel_dims(pick_ticket):
    """
    Aggregate weight (oz) and max dims (in) from pick ticket lines.
    Falls back to variant → product shipping fields.
    """
    total_weight_lbs = 0.0
    max_length = 0.0
    max_width = 0.0
    total_height = 0.0

    for line in pick_ticket.lines.select_related("variant__product").all():
        variant = line.variant
        if not variant:
            continue
        product = variant.product
        qty = float(line.quantity)

        def _f(v):
            return float(v) if v is not None else 0.0

        weight = _f(variant.shipping_weight) or _f(product.shipping_weight)
        length = _f(variant.shipping_length) or _f(product.shipping_length)
        width = _f(variant.shipping_width) or _f(product.shipping_width)
        height = _f(variant.shipping_height) or _f(product.shipping_height)

        total_weight_lbs += weight * qty
        max_length = max(max_length, length)
        max_width = max(max_width, width)
        total_height += height * qty

    weight_oz = round(total_weight_lbs * 16)
    return weight_oz, max_length, max_width, total_height


def _select_rate(shipment, carrier: str, service: str):
    """
    Pick the best matching rate. Falls back to lowest available rate.
    """
    carrier_upper = carrier.upper()
    service_lower = service.lower()

    # Try exact carrier + service match
    for rate in shipment.rates:
        r_carrier = (rate.carrier or "").upper()
        r_service = (rate.service or "").lower()
        if r_carrier == carrier_upper and service_lower in r_service:
            return rate

    # Try carrier-only match, pick cheapest
    carrier_rates = [
        r for r in shipment.rates
        if (r.carrier or "").upper() == carrier_upper
    ]
    if carrier_rates:
        return min(carrier_rates, key=lambda r: float(r.rate or 0))

    # Absolute fallback: lowest overall
    if shipment.rates:
        return min(shipment.rates, key=lambda r: float(r.rate or 0))

    raise ValueError("No rates returned by EasyPost")
