"""Warehouses and warehouse stock."""

from __future__ import annotations

from typing import Any

from ._cache import cached_fetch, make_lookup_key
from ._writable_fields import WAREHOUSE_STOCK_WRITABLE, validate_fields
from .client import ERPClient, ERPNotFoundError

WAREHOUSES_URL = "/api/v1/inventory/warehouses/"
STOCK_URL = "/api/v1/inventory/stock/"


# ---- Warehouses ------------------------------------------------------------


async def list_warehouses(
    client: ERPClient,
    *,
    search: str | None = None,
    warehouse_type: str | None = None,
    is_active: bool | None = None,
    can_fulfill: bool | None = None,
    ordering: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List warehouses. `limit=None` (default) fetches every page."""
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if warehouse_type:
        params["warehouse_type"] = warehouse_type
    if is_active is not None:
        params["is_active"] = str(is_active).lower()
    if can_fulfill is not None:
        params["can_fulfill"] = str(can_fulfill).lower()
    if ordering:
        params["ordering"] = ordering
    return await client.list(WAREHOUSES_URL, params=params, max_items=limit)


async def find_warehouse(client: ERPClient, query: str) -> dict[str, Any]:
    """Find a warehouse by code or name. Returns the match envelope.

    Cached for 5 minutes on the shared client — warehouse definitions change
    rarely and the find_warehouse call is a common precursor to stock writes.
    """
    key = make_lookup_key("warehouse_search", q=query)
    hits = await cached_fetch(
        client.lookup_cache,
        key,
        lambda: client.list(WAREHOUSES_URL, params={"search": query, "page_size": 10}, max_items=10),
    )
    q = query.lower()
    for h in hits:
        if (h.get("warehouse_code") or "").lower() == q or (h.get("name") or "").lower() == q:
            return {"match_quality": "exact", "item": h}
    if hits:
        return {"match_quality": "fuzzy", "item": hits[0]}
    raise ERPNotFoundError(f"No warehouse found matching '{query}'")


async def get_warehouse(client: ERPClient, warehouse_id: str) -> dict[str, Any]:
    return await client.get(f"{WAREHOUSES_URL}{warehouse_id}/")


# ---- Stock -----------------------------------------------------------------


async def list_stock(
    client: ERPClient,
    *,
    variant: str | None = None,
    warehouse: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"page_size": min(limit, 100)}
    if variant:
        params["variant"] = variant
    if warehouse:
        params["warehouse"] = warehouse
    return await client.list(STOCK_URL, params=params, max_items=limit)


async def get_stock_for_variant(
    client: ERPClient,
    variant_id: str,
    *,
    warehouse_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return stock rows for a variant across one or all warehouses."""
    return await list_stock(client, variant=variant_id, warehouse=warehouse_id, limit=100)


async def get_stock_record(client: ERPClient, stock_id: str) -> dict[str, Any]:
    return await client.get(f"{STOCK_URL}{stock_id}/")


async def update_stock_record(
    client: ERPClient, stock_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing WarehouseStock row. Unknown fields raise.

    Common fields: quantity, reserved_qty, reorder_level, reorder_qty, bin_location, zone.
    Note: `available_qty` is read-only — it's computed as quantity - reserved_qty - committed_qty.
    """
    validate_fields(fields, WAREHOUSE_STOCK_WRITABLE, "warehouse stock")
    return await client.patch(f"{STOCK_URL}{stock_id}/", json=fields)


async def set_stock_quantity(
    client: ERPClient,
    *,
    variant_id: str,
    warehouse_id: str,
    quantity: int,
) -> dict[str, Any]:
    """High-level: set the on-hand quantity for a variant in a specific warehouse.

    Looks up or creates the WarehouseStock row, then patches `quantity`.
    """
    rows = await list_stock(client, variant=variant_id, warehouse=warehouse_id, limit=1)
    if rows:
        return await update_stock_record(client, rows[0]["id"], {"quantity": quantity})
    # Create new stock record
    payload = {
        "variant": variant_id,
        "warehouse": warehouse_id,
        "quantity": quantity,
    }
    return await client.post(STOCK_URL, json=payload)


async def list_low_stock(
    client: ERPClient,
    *,
    warehouse: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """Return stock rows where quantity is at or below reorder_level.

    Uses the server-side `?below_reorder=true` filter added to the
    WarehouseStockViewSet — only rows with positive `reorder_level` and
    `quantity <= reorder_level` are returned. Scoped to a single warehouse
    if `warehouse` is provided.
    """
    params: dict[str, Any] = {
        "below_reorder": "true",
        "page_size": 100 if limit is None else min(limit, 100),
    }
    if warehouse:
        params["warehouse"] = warehouse
    return await client.list(STOCK_URL, params=params, max_items=limit)
