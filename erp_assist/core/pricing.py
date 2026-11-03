"""Price lists, price list items, price history, and competitor listings."""

from __future__ import annotations

from typing import Any

from ._writable_fields import (
    COMPETITOR_LISTING_WRITABLE,
    PRICE_LIST_ITEM_WRITABLE,
    validate_fields,
)
from .client import ERPClient, ERPNotFoundError

PRICE_LISTS_URL = "/api/v1/pricing/price-lists/"
PRICE_LIST_ITEMS_URL = "/api/v1/pricing/price-list-items/"
PRICE_HISTORY_URL = "/api/v1/pricing/price-history/"
COMPETITOR_LISTINGS_URL = "/api/v1/pricing/competitor-listings/"


# ---- Price lists -----------------------------------------------------------


async def list_price_lists(
    client: ERPClient,
    *,
    search: str | None = None,
    price_list_type: str | None = None,
    is_active: bool | None = None,
    currency: str | None = None,
    ordering: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List price lists. `limit=None` (default) fetches every page."""
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if price_list_type:
        params["price_list_type"] = price_list_type
    if is_active is not None:
        params["is_active"] = str(is_active).lower()
    if currency:
        params["currency"] = currency
    if ordering:
        params["ordering"] = ordering
    return await client.list(PRICE_LISTS_URL, params=params, max_items=limit)


async def find_price_list(client: ERPClient, query: str) -> dict[str, Any]:
    """Find a price list by name. Returns the match envelope `{match_quality, item}`."""
    hits = await client.list(PRICE_LISTS_URL, params={"search": query, "page_size": 10}, max_items=10)
    q = query.lower()
    for h in hits:
        if (h.get("name") or "").lower() == q:
            return {"match_quality": "exact", "item": h}
    if hits:
        return {"match_quality": "fuzzy", "item": hits[0]}
    raise ERPNotFoundError(f"No price list found matching '{query}'")


async def get_price_list(client: ERPClient, price_list_id: str) -> dict[str, Any]:
    return await client.get(f"{PRICE_LISTS_URL}{price_list_id}/")


# ---- Price list items ------------------------------------------------------


async def list_price_list_items(
    client: ERPClient,
    *,
    price_list: str | None = None,
    variant: str | None = None,
    variant_sku: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List price-list items. `variant_sku` is a shortcut — server supports `search` on variant__sku."""
    params: dict[str, Any] = {"page_size": min(limit, 100)}
    if price_list:
        params["price_list"] = price_list
    if variant:
        params["variant"] = variant
    if variant_sku:
        params["search"] = variant_sku
    if is_active is not None:
        params["is_active"] = str(is_active).lower()
    return await client.list(PRICE_LIST_ITEMS_URL, params=params, max_items=limit)


async def get_price_list_item(client: ERPClient, item_id: str) -> dict[str, Any]:
    return await client.get(f"{PRICE_LIST_ITEMS_URL}{item_id}/")


async def create_price_list_item(
    client: ERPClient,
    *,
    price_list: str,
    variant: str,
    price: float,
    min_qty: int = 1,
    uom: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    is_active: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a new price-list item entry. `price_list` and `variant` must be UUIDs."""
    payload: dict[str, Any] = {
        "price_list": price_list,
        "variant": variant,
        "price": price,
        "min_qty": min_qty,
        "is_active": is_active,
    }
    if uom is not None:
        payload["uom"] = uom
    if valid_from is not None:
        payload["valid_from"] = valid_from
    if valid_to is not None:
        payload["valid_to"] = valid_to
    if notes is not None:
        payload["notes"] = notes
    return await client.post(PRICE_LIST_ITEMS_URL, json=payload)


async def update_price_list_item(
    client: ERPClient, item_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a price-list item. Unknown fields raise."""
    validate_fields(fields, PRICE_LIST_ITEM_WRITABLE, "price list item")
    return await client.patch(f"{PRICE_LIST_ITEMS_URL}{item_id}/", json=fields)


async def delete_price_list_item(client: ERPClient, item_id: str) -> dict[str, Any]:
    """Permanently delete a price-list item row.

    Use sparingly — the usual fix for a mistaken entry is to flip `is_active`
    to false via update_price_list_item. This tool is for cleaning up truly
    accidental rows (e.g. entries created with the wrong variant UUID).
    """
    await client.delete(f"{PRICE_LIST_ITEMS_URL}{item_id}/")
    return {"deleted": True, "resource": "price_list_item", "id": item_id}


# ---- Price history (read-only) ---------------------------------------------


async def list_price_history(
    client: ERPClient,
    *,
    listing: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"page_size": min(limit, 100)}
    if listing:
        params["listing"] = listing
    return await client.list(PRICE_HISTORY_URL, params=params, max_items=limit)


# ---- Competitor listings ---------------------------------------------------


async def list_competitor_listings(
    client: ERPClient,
    *,
    product: str | None = None,
    variant: str | None = None,
    competitor_name: str | None = None,
    search: str | None = None,
    is_verified: bool | None = None,
    is_active: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"page_size": min(limit, 100)}
    if product:
        params["product"] = product
    if variant:
        params["variant"] = variant
    if competitor_name:
        params["competitor_name"] = competitor_name
    if search:
        params["search"] = search
    if is_verified is not None:
        params["is_verified"] = str(is_verified).lower()
    if is_active is not None:
        params["is_active"] = str(is_active).lower()
    return await client.list(COMPETITOR_LISTINGS_URL, params=params, max_items=limit)


async def get_competitor_listing(client: ERPClient, listing_id: str) -> dict[str, Any]:
    return await client.get(f"{COMPETITOR_LISTINGS_URL}{listing_id}/")


async def update_competitor_listing(
    client: ERPClient, listing_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a competitor listing. Unknown fields raise."""
    validate_fields(fields, COMPETITOR_LISTING_WRITABLE, "competitor listing")
    return await client.patch(f"{COMPETITOR_LISTINGS_URL}{listing_id}/", json=fields)


async def delete_competitor_listing(client: ERPClient, listing_id: str) -> dict[str, Any]:
    """Permanently delete a competitor listing row.

    Competitor listings are scraped data, so a mistaken row is safe to delete —
    the scraper can re-populate it if it still exists on the target site.
    """
    await client.delete(f"{COMPETITOR_LISTINGS_URL}{listing_id}/")
    return {"deleted": True, "resource": "competitor_listing", "id": listing_id}
