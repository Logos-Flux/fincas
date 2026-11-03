"""MCP stdio adapter: exposes core.* operations as MCP tools.

Each tool is a thin wrapper that reuses a shared ERPClient, calls the core
function, and returns plain dicts/lists that FastMCP serialises to structured
content. No business logic lives here.

Run with: python -m erp_assist.mcp_adapter
"""

from __future__ import annotations

import asyncio
import atexit
from typing import Any

from mcp.server.fastmcp import FastMCP

from .core import ERPAmbiguousError, ERPClient, ERPError
from .core import crm as crm_core
from .core import customers as customers_core
from .core import inventory as inventory_core
from .core import orders as orders_core
from .core import parties as parties_core
from .core import pricing as pricing_core
from .core import products as products_core
from .core import purchasing as purchasing_core

mcp = FastMCP("golden-erp-assist")


# Shared ERPClient reused across tool calls to avoid a fresh TLS handshake per
# invocation. Lazy-initialised on first use; protected by a lock so concurrent
# tool calls can't race on creation. The stdio server is long-lived — this
# singleton lives for the process lifetime.
_shared_client: ERPClient | None = None
_shared_client_lock = asyncio.Lock()


async def _get_client() -> ERPClient:
    global _shared_client
    if _shared_client is not None:
        return _shared_client
    async with _shared_client_lock:
        if _shared_client is None:
            _shared_client = ERPClient.from_env()
    return _shared_client


def _close_shared_client() -> None:
    """atexit hook — tear down the shared client on interpreter shutdown.

    We can't await from an atexit callback, so we spin up a throwaway event
    loop just for the close. If no client was ever created we're a no-op.
    """
    global _shared_client
    if _shared_client is None:
        return
    client = _shared_client
    _shared_client = None
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(client.aclose())
        finally:
            loop.close()
    except Exception:  # noqa: BLE001 — best-effort cleanup at shutdown
        pass


atexit.register(_close_shared_client)


# Fields that carry no useful information for an LLM skimming a list — SEO
# metadata (irrelevant unless specifically asked), external-system identifiers
# (BigCommerce, D1, ERPNext, Vectorize), and timestamps that rarely drive
# decisions. These get stripped from *list* responses; detail calls (get_*)
# keep everything so the LLM can still inspect them when needed.
_LIST_NOISE_FIELDS = frozenset({
    "seo_title",
    "seo_meta_description",
    "seo_og_title",
    "seo_og_description",
    "seo_og_image_id",
    "seo_canonical_url",
    "bc_id",
    "d1_id",
    "erpnext_name",
    "vectorize_id",
    "created_at",
    "updated_at",
    "metadata",
})


def _is_empty(value: Any) -> bool:
    """True for None, empty strings, empty collections — things we can safely drop."""
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)) and len(value) == 0:
        return True
    return False


def _compact_item(item: Any) -> Any:
    """Drop noise fields and empty values from a list-item dict.

    Non-dict items (e.g. raw strings or numbers from a list endpoint) pass through.
    """
    if not isinstance(item, dict):
        return item
    return {
        k: v
        for k, v in item.items()
        if k not in _LIST_NOISE_FIELDS and not _is_empty(v)
    }


def _require_confirm(confirm: bool, what: str) -> dict[str, Any] | None:
    """Return an error envelope if `confirm` isn't True, else None.

    Used as the first line of every destructive delete tool. This stops the
    LLM from one-shotting a delete — it has to pass `confirm=True` explicitly,
    which means the call has to come from a reasoning step that explicitly
    intends to destroy data.
    """
    if confirm is not True:
        return {
            "error": "ConfirmationRequired",
            "message": (
                f"Refusing to {what} without explicit confirm=True. "
                "Pass confirm=True as an argument to proceed. This is a "
                "destructive operation that cannot be undone via this tool."
            ),
        }
    return None


async def _run(fn, **kwargs) -> Any:
    """Run a core function against the shared ERPClient and return the raw result.

    - Errors become structured `{error, message}` dicts instead of raised exceptions,
      so the LLM can reason about them without the MCP server crashing.
    - List results are compacted in place — noise fields (SEO, BC/D1/ERPNext IDs,
      timestamps, empty strings) are dropped so a single tool call doesn't burn
      the MCP output cap on empty boilerplate.
    - Dict results (get_* detail calls) pass through unchanged.
    - ERPAmbiguousError (phase2-party.md 2.7) is rendered as the match
      envelope `{error, match_quality, item: null, candidates, message}` so
      the assistant can quote the candidates and ask the user which one.
    """
    try:
        client = await _get_client()
        result = await fn(client, **kwargs)
    except ERPAmbiguousError as exc:
        return {"error": "ERPAmbiguousError", **exc.envelope, "item": None}
    except ERPError as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — MCP layer must not crash the server
        return {"error": "UnexpectedError", "message": str(exc)}
    if isinstance(result, list):
        return [_compact_item(item) for item in result]
    return result


# ========== PRODUCTS ========================================================


@mcp.tool()
async def list_products(
    search: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    product_type: str | None = None,
    is_active: bool | None = None,
    visible_on_website: bool | None = None,
    detail: bool = False,
    ordering: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List products with optional filters.

    Args:
        search: Free-text match against title, part number, or slug.
        brand: Brand UUID or brand title/slug (e.g. "sol-ark", "MidNite Solar").
        category: Category UUID or category title/slug (e.g. "inverters", "solar-panels").
                  More reliable than `search` for catalog browsing because title search
                  misses products labelled differently (e.g. "module" vs "panel").
        product_type: e.g. "standalone", "variant_parent".
        is_active: If set, filter to active or inactive products.
        visible_on_website: If set, filter to storefront-visible products.
        detail: If true, include full nested data (variants, images, options).
        ordering: DRF ordering string — "title", "-created_at", etc.
        limit: Max number of products to return. Pass null to fetch every page
               (use sparingly — the catalog has hundreds of products). Default 20.
    """
    return await _run(
        products_core.list_products,
        search=search,
        brand=brand,
        category=category,
        product_type=product_type,
        is_active=is_active,
        visible_on_website=visible_on_website,
        detail=detail,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def find_product(query: str) -> Any:
    """Find a single product by title, slug, or part number.

    Returns `{"match_quality": "exact" | "fuzzy", "item": {...}}`. "exact" is a
    case-insensitive match on part_number or title; "fuzzy" is the first search
    hit. **Before writing to a fuzzy match, confirm with the user** — you may
    land on the wrong row.
    """
    return await _run(products_core.find_product, query=query)


@mcp.tool()
async def get_product(product_id: str) -> Any:
    """Fetch full details of a product by UUID (includes variants, images, options)."""
    return await _run(products_core.get_product, product_id=product_id)


@mcp.tool()
async def create_product(
    title: str,
    part_number: str,
    brand: str,
    slug: str | None = None,
    product_type: str = "physical",
    description: str | None = None,
    short_description: str | None = None,
    categories: list[str] | None = None,
    is_active: bool = True,
    visible_on_website: bool = False,
    has_variants: bool = False,
) -> Any:
    """Create a new product.

    Args:
        title: Product display name.
        part_number: SKU-like manufacturer part number (unique identifier).
        brand: Brand UUID, or a brand name/slug like "sol-ark" (resolved automatically).
        slug: URL slug. Auto-derived from `title` if omitted (e.g. "Sol-Ark 15K" → "sol-ark-15k").
        product_type: One of "physical", "digital", "service", "kit", "bom". Defaults to "physical".
        description: Long HTML-capable description.
        short_description: Plain-text summary.
        categories: List of category UUIDs or names/slugs (resolved automatically).
        is_active: Whether the product is active in the ERP.
        visible_on_website: Whether the product shows on the storefront. Default false — new products are hidden until manually enabled.
        has_variants: Set true if this will be a parent product with child variants.
    """
    return await _run(
        products_core.create_product,
        title=title,
        part_number=part_number,
        brand=brand,
        slug=slug,
        product_type=product_type,
        description=description,
        short_description=short_description,
        categories=categories,
        is_active=is_active,
        visible_on_website=visible_on_website,
        has_variants=has_variants,
    )


@mcp.tool()
async def update_product(product_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing product.

    Args:
        product_id: UUID of the product to update.
        fields: Dict of field name → new value. Any writable product field is allowed
                (title, part_number, brand, is_active, visible_on_website, description, etc.).
    """
    return await _run(products_core.update_product, product_id=product_id, fields=fields)


@mcp.tool()
async def set_product_visibility(product_id: str, visible: bool) -> Any:
    """Show or hide a product on the storefront.

    Shortcut for update_product with only visible_on_website. Setting visible=true
    publishes the product to the storefront; false hides it.
    """
    return await _run(products_core.set_product_visibility, product_id=product_id, visible=visible)


# ---- Variants --------------------------------------------------------------


@mcp.tool()
async def list_variants(
    product_id: str | None = None,
    search: str | None = None,
    is_active: bool | None = None,
    stock_status: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List product variants, optionally scoped to a parent product or filtered by SKU/name.

    Args:
        product_id: UUID of the parent product (optional).
        search: Free-text match against SKU or variant name.
        is_active: Filter by active status.
        stock_status: e.g. "in_stock", "out_of_stock", "backorder".
        ordering: DRF ordering string (e.g. "sku", "-sort_order").
        limit: Max number of variants. Pass null to fetch every page. Default 20.
    """
    return await _run(
        products_core.list_variants,
        product_id=product_id,
        search=search,
        is_active=is_active,
        stock_status=stock_status,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def find_variant_by_sku(sku: str) -> Any:
    """Find a variant by SKU.

    Returns `{"match_quality": "exact" | "fuzzy", "item": {...}}`. "exact" is a
    case-insensitive match on the SKU; "fuzzy" is the first search hit.
    **Confirm with the user before writing to a fuzzy match.**
    """
    return await _run(products_core.find_variant_by_sku, sku=sku)


@mcp.tool()
async def get_variant(variant_id: str) -> Any:
    """Fetch full details of a variant by UUID."""
    return await _run(products_core.get_variant, variant_id=variant_id)


@mcp.tool()
async def update_variant(variant_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing variant.

    Writable fields include sku, name, is_active, retail_price, sale_price, msrp_price,
    stock, stock_status, dimensions, GTIN, MPN, etc. For price updates specifically,
    update_variant_price is more ergonomic.
    """
    return await _run(products_core.update_variant, variant_id=variant_id, fields=fields)


@mcp.tool()
async def update_variant_price(
    variant_id: str,
    retail_price: float | None = None,
    sale_price: float | None = None,
    msrp_price: float | None = None,
    map_price: float | None = None,
    wholesale_price: float | None = None,
) -> Any:
    """Update one or more default prices on a variant.

    These prices are the "base" prices stored on the variant itself. For dealer-tier
    or price-list-specific pricing, use update_price_list_item instead.
    """
    return await _run(
        products_core.update_variant_price,
        variant_id=variant_id,
        retail_price=retail_price,
        sale_price=sale_price,
        msrp_price=msrp_price,
        map_price=map_price,
        wholesale_price=wholesale_price,
    )


@mcp.tool()
async def bulk_update_variants(
    updates: list[dict[str, Any]],
    concurrency: int = 8,
) -> Any:
    """Apply a batch of variant updates in parallel.

    For campaign-style operations that touch many rows at once. Individual
    rows can fail without aborting the batch — the response has separate
    `updated` and `failed` lists so you can see exactly what landed.

    Args:
        updates: List of `{"variant_id": "<uuid>", "fields": {"retail_price": 99.99, ...}}`.
                 Each row is validated against the variant writable-fields allow-list
                 before hitting the API.
        concurrency: Max parallel PATCH calls (default 8). Raise this for very
                     large batches; lower it if you're hitting rate limits.

    Returns:
        `{"updated": [{"variant_id", "item"}], "failed": [{"variant_id", "error", "message"}],
          "summary": {"total", "succeeded", "failed_count"}}`.
    """
    return await _run(
        products_core.bulk_update_variants, updates=updates, concurrency=concurrency
    )


@mcp.tool()
async def bulk_update_variant_prices(
    updates: list[dict[str, Any]],
    concurrency: int = 8,
) -> Any:
    """Apply a batch of variant *price* updates in parallel.

    Common use case: catalog-wide price adjustments ("drop all CW Energy retail
    prices by 5%"). Typical workflow is to list variants via `list_variants`,
    compute new prices client-side, then call this with the full batch.

    Args:
        updates: List of per-row dicts. Each row must have `variant_id` and at
                 least one of: retail_price, sale_price, msrp_price, map_price,
                 wholesale_price. Rows with no price fields are rejected locally
                 without a network call.
        concurrency: Max parallel PATCH calls (default 8).

    Returns:
        `{"updated": [...], "failed": [...], "summary": {...}}` — same shape as
        bulk_update_variants. Check the `failed` list for rows that need retry.
    """
    return await _run(
        products_core.bulk_update_variant_prices,
        updates=updates,
        concurrency=concurrency,
    )


# ---- Brands & Categories ---------------------------------------------------


@mcp.tool()
async def list_brands(
    search: str | None = None,
    visible_on_website: bool | None = None,
    ordering: str | None = None,
    limit: int | None = None,
) -> Any:
    """List brands.

    Args:
        search: Free-text match on brand title.
        visible_on_website: If set, filter to storefront-visible brands.
        ordering: DRF ordering string (e.g. "title", "-sort_order").
        limit: Max brands to return. Pass null (the default) to fetch every page.
    """
    return await _run(
        products_core.list_brands,
        search=search,
        visible_on_website=visible_on_website,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def find_brand(query: str) -> Any:
    """Find a brand by title or slug.

    Returns `{"match_quality": "exact" | "fuzzy", "item": {...}}`. Confirm
    before writing to a fuzzy match.
    """
    return await _run(products_core.find_brand, query=query)


@mcp.tool()
async def get_brand(brand_id: str) -> Any:
    """Fetch full details of a brand by UUID (includes SEO, description, timestamps)."""
    return await _run(products_core.get_brand, brand_id=brand_id)


@mcp.tool()
async def create_brand(
    title: str,
    slug: str | None = None,
    description: str | None = None,
    website_url: str | None = None,
    visible_on_website: bool = True,
    is_authorized_dealer: bool = False,
) -> Any:
    """Create a new brand.

    Args:
        title: Brand display name (e.g. "Sol-Ark").
        slug: URL slug. Auto-derived from `title` if omitted (e.g. "Sol-Ark" → "sol-ark").
        description: Long description shown on brand pages.
        website_url: Manufacturer's website.
        visible_on_website: Whether the brand shows on the storefront. Defaults true.
        is_authorized_dealer: Whether we're an authorized dealer. Defaults false.

    Follow-up fields (SEO title/meta, logo/banner image IDs, sort_order,
    is_featured_store, etc.) can be set afterwards with `update_brand`.
    """
    return await _run(
        products_core.create_brand,
        title=title,
        slug=slug,
        description=description,
        website_url=website_url,
        visible_on_website=visible_on_website,
        is_authorized_dealer=is_authorized_dealer,
    )


@mcp.tool()
async def update_brand(brand_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing brand.

    Args:
        brand_id: UUID of the brand to update.
        fields: Dict of field name → new value. Any writable brand field is allowed
                (title, slug, description, website_url, visible_on_website,
                is_authorized_dealer, is_featured_store, sort_order, logo_image_id,
                banner_image_id, seo_title, seo_meta_description, etc.).
    """
    return await _run(products_core.update_brand, brand_id=brand_id, fields=fields)


@mcp.tool()
async def list_categories(
    search: str | None = None,
    parent: str | None = None,
    top_level_only: bool = False,
    visible_on_website: bool | None = None,
    ordering: str | None = None,
    limit: int | None = None,
) -> Any:
    """List categories.

    Args:
        search: Free-text match on category title.
        parent: UUID of a parent category — returns only its direct children.
        top_level_only: If true, returns only root categories (tree_depth=0).
                        Use this when you want the top of the tree without
                        subcategories mixed in alphabetically.
        visible_on_website: If set, filter to storefront-visible categories.
        ordering: DRF ordering string. "sort_order" matches admin UI order;
                  "tree_depth,title" groups top-level first then subcategories;
                  "title" is alphabetical (the DRF default).
        limit: Max categories to return. Pass null (the default) to fetch every
               page — the category tree is small enough that this is safe.
    """
    return await _run(
        products_core.list_categories,
        search=search,
        parent=parent,
        top_level_only=top_level_only,
        visible_on_website=visible_on_website,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def find_category(query: str) -> Any:
    """Find a category by title or slug.

    Returns `{"match_quality": "exact" | "fuzzy", "item": {...}}`. Confirm
    before writing to a fuzzy match.
    """
    return await _run(products_core.find_category, query=query)


@mcp.tool()
async def get_category(category_id: str) -> Any:
    """Fetch full details of a category by UUID (includes parent, tree_depth, SEO, description)."""
    return await _run(products_core.get_category, category_id=category_id)


# ========== PRICING =========================================================


@mcp.tool()
async def list_price_lists(
    search: str | None = None,
    price_list_type: str | None = None,
    is_active: bool | None = None,
    currency: str | None = None,
    ordering: str | None = None,
    limit: int | None = None,
) -> Any:
    """List price lists (dealer tiers, retail, MAP, wholesale, etc.).

    Args:
        search: Free-text match on price list name.
        price_list_type: Filter by type (e.g. "selling", "buying").
        is_active: Filter by active status.
        currency: ISO currency code filter.
        ordering: DRF ordering string.
        limit: Pass null (default) to fetch every page.
    """
    return await _run(
        pricing_core.list_price_lists,
        search=search,
        price_list_type=price_list_type,
        is_active=is_active,
        currency=currency,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def find_price_list(query: str) -> Any:
    """Find a price list by name.

    Returns `{"match_quality": "exact" | "fuzzy", "item": {...}}`.
    """
    return await _run(pricing_core.find_price_list, query=query)


@mcp.tool()
async def get_price_list(price_list_id: str) -> Any:
    """Fetch full details of a price list by UUID."""
    return await _run(pricing_core.get_price_list, price_list_id=price_list_id)


@mcp.tool()
async def list_price_list_items(
    price_list: str | None = None,
    variant: str | None = None,
    variant_sku: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
) -> Any:
    """List entries in price lists.

    Args:
        price_list: UUID of a specific price list.
        variant: UUID of a specific variant.
        variant_sku: Shortcut — search by variant SKU.
        is_active: Filter by active status.
        limit: Max results (default 50).
    """
    return await _run(
        pricing_core.list_price_list_items,
        price_list=price_list,
        variant=variant,
        variant_sku=variant_sku,
        is_active=is_active,
        limit=limit,
    )


@mcp.tool()
async def create_price_list_item(
    price_list: str,
    variant: str,
    price: float,
    min_qty: int = 1,
    uom: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    notes: str | None = None,
) -> Any:
    """Add a new variant price to a price list.

    Args:
        price_list: UUID of the price list.
        variant: UUID of the variant.
        price: Price value.
        min_qty: Minimum order quantity for this price tier (default 1).
        uom: Unit of measure override (optional).
        valid_from: ISO date YYYY-MM-DD when this price becomes active (optional).
        valid_to: ISO date YYYY-MM-DD when this price expires (optional).
        notes: Free-text notes.
    """
    return await _run(
        pricing_core.create_price_list_item,
        price_list=price_list,
        variant=variant,
        price=price,
        min_qty=min_qty,
        uom=uom,
        valid_from=valid_from,
        valid_to=valid_to,
        notes=notes,
    )


@mcp.tool()
async def get_price_list_item(item_id: str) -> Any:
    """Fetch a single price-list-item row by UUID (full detail, no compaction)."""
    return await _run(pricing_core.get_price_list_item, item_id=item_id)


@mcp.tool()
async def update_price_list_item(item_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing price-list item (price, min_qty, valid_from/to, is_active, notes)."""
    return await _run(pricing_core.update_price_list_item, item_id=item_id, fields=fields)


@mcp.tool()
async def delete_price_list_item(item_id: str, confirm: bool = False) -> Any:
    """Permanently delete a price-list item. Requires confirm=True.

    Usually the right fix for a mistaken row is to flip `is_active` to
    false via update_price_list_item. Use this only when you're sure the
    row shouldn't exist at all.

    Args:
        item_id: UUID of the price-list item to delete.
        confirm: Must be explicitly set to True. Default False refuses the
                 call with an error — this is a destructive operation.
    """
    guard = _require_confirm(confirm, "delete price-list item")
    if guard is not None:
        return guard
    return await _run(pricing_core.delete_price_list_item, item_id=item_id)


@mcp.tool()
async def list_price_history(
    listing: str | None = None,
    limit: int | None = 50,
) -> Any:
    """List historical competitor price observations.

    Args:
        listing: CompetitorListing UUID to scope the history to one competitor row.
        limit: Max number of history rows. Pass null to fetch every page.
    """
    return await _run(pricing_core.list_price_history, listing=listing, limit=limit)


@mcp.tool()
async def list_competitor_listings(
    product: str | None = None,
    variant: str | None = None,
    competitor_name: str | None = None,
    search: str | None = None,
    is_verified: bool | None = None,
    is_active: bool | None = None,
    limit: int | None = 50,
) -> Any:
    """List competitor listings (scraped prices from other retailers).

    `limit=None` fetches every page. Default 50 — competitor data is the one
    domain where response size can balloon quickly.
    """
    return await _run(
        pricing_core.list_competitor_listings,
        product=product,
        variant=variant,
        competitor_name=competitor_name,
        search=search,
        is_verified=is_verified,
        is_active=is_active,
        limit=limit,
    )


@mcp.tool()
async def get_competitor_listing(listing_id: str) -> Any:
    """Fetch full details of a competitor listing by UUID."""
    return await _run(pricing_core.get_competitor_listing, listing_id=listing_id)


@mcp.tool()
async def update_competitor_listing(listing_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on a competitor listing (price, in_stock, is_verified, is_active, notes)."""
    return await _run(
        pricing_core.update_competitor_listing, listing_id=listing_id, fields=fields
    )


@mcp.tool()
async def delete_competitor_listing(listing_id: str, confirm: bool = False) -> Any:
    """Permanently delete a competitor listing. Requires confirm=True.

    Competitor listings are scraped data — deleting a row is safe because
    the scraper can re-populate it if the listing still exists on the
    target site.
    """
    guard = _require_confirm(confirm, "delete competitor listing")
    if guard is not None:
        return guard
    return await _run(pricing_core.delete_competitor_listing, listing_id=listing_id)


# ========== INVENTORY =======================================================


@mcp.tool()
async def list_warehouses(
    search: str | None = None,
    warehouse_type: str | None = None,
    is_active: bool | None = None,
    can_fulfill: bool | None = None,
    ordering: str | None = None,
    limit: int | None = None,
) -> Any:
    """List warehouses (ship-from locations).

    Args:
        search: Free-text match on warehouse code or name.
        warehouse_type: Filter by warehouse type.
        is_active: Filter by active status.
        can_fulfill: If set, filter to warehouses that can fulfill orders.
        ordering: DRF ordering string.
        limit: Pass null (default) to fetch every page.
    """
    return await _run(
        inventory_core.list_warehouses,
        search=search,
        warehouse_type=warehouse_type,
        is_active=is_active,
        can_fulfill=can_fulfill,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def find_warehouse(query: str) -> Any:
    """Find a warehouse by code or name.

    Returns `{"match_quality": "exact" | "fuzzy", "item": {...}}`. Inventory
    writes on a fuzzy match can land on the wrong warehouse — confirm first.
    """
    return await _run(inventory_core.find_warehouse, query=query)


@mcp.tool()
async def get_warehouse(warehouse_id: str) -> Any:
    """Fetch full details of a warehouse by UUID (address, contact, metadata)."""
    return await _run(inventory_core.get_warehouse, warehouse_id=warehouse_id)


@mcp.tool()
async def list_stock(
    variant: str | None = None,
    warehouse: str | None = None,
    limit: int | None = 50,
) -> Any:
    """List WarehouseStock rows. Prefer get_stock_for_variant for the common case.

    Args:
        variant: Filter by variant UUID.
        warehouse: Filter by warehouse UUID.
        limit: Max rows. Pass null to fetch every page.
    """
    return await _run(
        inventory_core.list_stock,
        variant=variant,
        warehouse=warehouse,
        limit=limit,
    )


@mcp.tool()
async def get_stock_record(stock_id: str) -> Any:
    """Fetch a single WarehouseStock row by UUID (full detail including reserved/committed quantities)."""
    return await _run(inventory_core.get_stock_record, stock_id=stock_id)


@mcp.tool()
async def get_stock_for_variant(variant_id: str, warehouse_id: str | None = None) -> Any:
    """Return stock rows for a variant across one or all warehouses."""
    return await _run(
        inventory_core.get_stock_for_variant,
        variant_id=variant_id,
        warehouse_id=warehouse_id,
    )


@mcp.tool()
async def set_stock_quantity(variant_id: str, warehouse_id: str, quantity: int) -> Any:
    """Set the on-hand quantity for a variant in a specific warehouse.

    Finds the existing WarehouseStock row or creates one. Does NOT perform a delta —
    this is an absolute set. The `available_qty` field is recomputed server-side.
    """
    return await _run(
        inventory_core.set_stock_quantity,
        variant_id=variant_id,
        warehouse_id=warehouse_id,
        quantity=quantity,
    )


@mcp.tool()
async def update_stock_record(stock_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing WarehouseStock row.

    Common fields: quantity, reserved_qty, reorder_level, reorder_qty, bin_location, zone.
    """
    return await _run(inventory_core.update_stock_record, stock_id=stock_id, fields=fields)


@mcp.tool()
async def list_low_stock(
    warehouse: str | None = None,
    limit: int | None = 50,
) -> Any:
    """Return stock rows where quantity is at or below reorder_level.

    Uses the server-side `?below_reorder=true` filter — O(matching rows),
    not O(all stock). Only rows with a positive reorder_level participate;
    rows with reorder tracking disabled are excluded.

    Args:
        warehouse: Optional warehouse UUID to scope to one location.
        limit: Max rows. Pass null to fetch every page.
    """
    return await _run(
        inventory_core.list_low_stock, warehouse=warehouse, limit=limit
    )


# ========== CUSTOMERS =======================================================


@mcp.tool()
async def list_customers(
    search: str | None = None,
    customer_type: str | None = None,
    pricing_tier: str | None = None,
    is_active: bool | None = None,
    company: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List customers.

    Args:
        search: Free-text match on first_name, last_name, email.
        customer_type: Filter (e.g. "retail", "wholesale", "dealer").
        pricing_tier: Filter by pricing tier.
        is_active: Filter by active status.
        company: Filter by company UUID.
        ordering: DRF ordering (e.g. "last_name", "-last_order_at", "-created_at").
        limit: Pass null to fetch every page. Default 20.
    """
    return await _run(
        customers_core.list_customers,
        search=search,
        customer_type=customer_type,
        pricing_tier=pricing_tier,
        is_active=is_active,
        company=company,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def find_customer(query: str) -> Any:
    """Find a customer by email, phone, or name. Never guesses.

    Returns `{"match_quality", "item", "candidates", "message"}`:

    - "exact": one customer whose email (normalised) or phone (E.164) equals
      the query. Proceed.
    - "fuzzy": exactly one name hit, not exact. **Confirm with the user
      before any write.**
    - "ambiguous" (returned as `{"error": "ERPAmbiguousError", ...}` with
      `item: null`): several customers match. **Stop and ask the user which
      one**, quoting the distinguishing fields of `candidates` (email, phone
      last 4, organisation, tenant, last order). Never pick one yourself and
      never retry with a narrower query on your own.
    - "none": nothing matches. Say so and offer to create one.
    """
    return await _run(customers_core.find_customer, query=query)


@mcp.tool()
async def get_customer(customer_id: str) -> Any:
    """Fetch full customer detail by UUID (includes nested addresses)."""
    return await _run(customers_core.get_customer, customer_id=customer_id)


@mcp.tool()
async def create_customer(
    first_name: str,
    last_name: str,
    email: str,
    customer_type: str = "individual",
    pricing_tier: str | None = None,
    phone: str | None = None,
    company_name: str | None = None,
    is_active: bool = True,
    accepts_marketing: bool = False,
    notes: str | None = None,
    confirm_new: bool = False,
) -> Any:
    """Create a new customer record.

    `customer_type` is `individual` (default) or `business`; `pricing_tier`
    defaults to the model default (`retail`).

    Checks for an existing party with this email first. If one (or several)
    exists, nothing is created and the result is `{"created": false,
    "match_quality", "candidates", "message"}`: show the candidates and ask
    the user. Pass `confirm_new=True` only after the user confirms this is a
    different person.
    """
    return await _run(
        customers_core.create_customer,
        first_name=first_name,
        last_name=last_name,
        email=email,
        customer_type=customer_type,
        pricing_tier=pricing_tier,
        phone=phone,
        company_name=company_name,
        is_active=is_active,
        accepts_marketing=accepts_marketing,
        notes=notes,
        confirm_new=confirm_new,
    )


@mcp.tool()
async def update_customer(customer_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing customer.

    Common edits: phone, email, company_name, pricing_tier, is_tax_exempt,
    notes, default_sales_rep, default_price_list, accepts_marketing.
    """
    return await _run(
        customers_core.update_customer, customer_id=customer_id, fields=fields
    )


@mcp.tool()
async def list_customer_addresses(
    customer: str | None = None,
    address_type: str | None = None,
    country: str | None = None,
    search: str | None = None,
    limit: int | None = 50,
) -> Any:
    """List customer addresses, optionally scoped to a customer UUID.

    `search` matches city and postal_code.
    """
    return await _run(
        customers_core.list_customer_addresses,
        customer=customer,
        address_type=address_type,
        country=country,
        search=search,
        limit=limit,
    )


@mcp.tool()
async def get_customer_address(address_id: str) -> Any:
    """Fetch a single customer address by UUID."""
    return await _run(customers_core.get_customer_address, address_id=address_id)


@mcp.tool()
async def create_customer_address(
    customer: str,
    address_line_1: str,
    city: str,
    state_province: str,
    postal_code: str,
    country: str,
    address_type: str = "shipping",
    first_name: str | None = None,
    last_name: str | None = None,
    company_name: str | None = None,
    address_line_2: str | None = None,
    phone: str | None = None,
    is_default: bool = False,
    is_residential: bool | None = None,
    has_loading_dock: bool | None = None,
    requires_liftgate: bool | None = None,
    delivery_instructions: str | None = None,
) -> Any:
    """Create a new address on a customer.

    `customer` is the customer's UUID, or an email / phone / name. Free text
    must match exactly one customer exactly; an ambiguous or fuzzy match
    returns `{"error": "ERPAmbiguousError", "candidates": [...]}` and writes
    nothing. Ask the user which candidate, then call again with its id.
    """
    return await _run(
        customers_core.create_customer_address,
        customer=customer,
        address_line_1=address_line_1,
        city=city,
        state_province=state_province,
        postal_code=postal_code,
        country=country,
        address_type=address_type,
        first_name=first_name,
        last_name=last_name,
        company_name=company_name,
        address_line_2=address_line_2,
        phone=phone,
        is_default=is_default,
        is_residential=is_residential,
        has_loading_dock=has_loading_dock,
        requires_liftgate=requires_liftgate,
        delivery_instructions=delivery_instructions,
    )


@mcp.tool()
async def update_customer_address(address_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing address."""
    return await _run(
        customers_core.update_customer_address, address_id=address_id, fields=fields
    )


@mcp.tool()
async def delete_customer_address(address_id: str, confirm: bool = False) -> Any:
    """Permanently delete a customer address. Requires confirm=True.

    Historical orders keep their own snapshotted ship-to fields, so deleting
    an address doesn't affect past order records.
    """
    guard = _require_confirm(confirm, "delete customer address")
    if guard is not None:
        return guard
    return await _run(
        customers_core.delete_customer_address, address_id=address_id
    )


# ========== PARTIES =========================================================


@mcp.tool()
async def find_party(query: str, company: str | None = None) -> Any:
    """Find a party (a person or an organisation) by email, phone, or name.

    Parties are the CRM's people and organisations: customers, suppliers,
    partners, leads and contacts all hang off one. Returns
    `{"match_quality", "item", "candidates", "message"}`:

    - "exact": one party whose email (normalised) or phone (E.164) equals
      the query. Proceed.
    - "fuzzy": exactly one name hit, not exact. **Confirm with the user
      before any write.**
    - "ambiguous" (returned as `{"error": "ERPAmbiguousError", ...}` with
      `item: null`): several parties match. **Stop and ask the user which
      one**, quoting the distinguishing fields of `candidates` (kind, email,
      phone last 4, roles, organisation, tenant, last order). Never pick one
      yourself and never retry with a narrower query on your own.
    - "none": nothing matches. Say so and offer `create_party`.

    Args:
        query: An email, a phone number, or a name.
        company: Optional tenant UUID to search in (service keys see all
            tenants, so the same email in two tenants is "ambiguous").
    """
    return await _run(parties_core.find_party, query=query, company=company)


@mcp.tool()
async def get_party(party_id: str) -> Any:
    """Fetch a party by UUID: names, emails, phones, addresses, roles.

    `mirrored_fields` lists fields owned by a linked customer or contact
    (`owned_by`); edit those on the customer, not the party.
    """
    return await _run(parties_core.get_party, party_id=party_id)


@mcp.tool()
async def list_parties(
    search: str | None = None,
    kind: str | None = None,
    role: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List live parties.

    Args:
        search: Name prefix, exact email, or exact phone.
        kind: "person" or "organisation".
        role: Only parties holding this role now (e.g. "customer", "supplier").
        limit: Pass null to fetch every page. Default 20.
    """
    return await _run(parties_core.list_parties, search=search, kind=kind, role=role, limit=limit)


@mcp.tool()
async def create_party(
    kind: str,
    display_name: str | None = None,
    given_name: str | None = None,
    family_name: str | None = None,
    legal_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    region: str | None = None,
    primary_domain: str | None = None,
    marketable: bool | None = None,
    company: str | None = None,
    confirm_new: bool = False,
) -> Any:
    """Create a person or organisation party, with its first email and phone.

    Checks the email (then the phone) against existing parties first. If one
    or several already hold it, nothing is created and the result is
    `{"created": false, "match_quality", "candidates", "message"}`: show the
    candidates and ask the user. Pass `confirm_new=True` only after the user
    confirms this really is a different party.

    `marketable` only marks a marketing contact; it is **not** consent.
    No merge, erase or suppression tools exist here: those stay with staff.

    Args:
        kind: "person" or "organisation".
        display_name: Required for an organisation; derived from the names
            for a person.
        given_name, family_name: Person only.
        legal_name: Organisation only.
        email, phone: The first contact points (normalised server-side).
        region: ISO country for a national-format phone (e.g. "US").
        primary_domain: Organisation web domain.
        company: Tenant UUID; needed only when several tenants exist.
        confirm_new: Skip the duplicate check (only after asking the user).
    """
    return await _run(
        parties_core.create_party,
        kind=kind,
        display_name=display_name,
        given_name=given_name,
        family_name=family_name,
        legal_name=legal_name,
        email=email,
        phone=phone,
        region=region,
        primary_domain=primary_domain,
        marketable=marketable,
        company=company,
        confirm_new=confirm_new,
    )


# ========== CRM: ENGAGEMENTS AND TASKS ======================================
#
# docs/crm/phase3-engagement.md 5.3. The assistant's key is shared and is not
# any person: tools that act for someone take `for_user` (a staff email or
# profile id) and resolve it through the staff directory. Several matches, or
# a name-only match, return the ambiguity envelope instead of a guess. Every
# call stays inside one tenant (the person's, the engagement's, or `company`).


@mcp.tool()
async def find_staff(query: str, company: str | None = None) -> Any:
    """Find a staff member (a person who can own engagements and tasks).

    Returns `{"match_quality", "item", "candidates", "message"}`. "exact" =
    one staff account with that email, or the profile id. A name is at best
    "fuzzy": confirm with the user. "ambiguous" (as `{"error":
    "ERPAmbiguousError", ...}`): ask the user which person, never pick.

    Args:
        query: A staff email, profile id, or name.
        company: Tenant id; the same email in two tenants is ambiguous without it.
    """
    return await _run(crm_core.find_staff, query=query, company=company)


@mcp.tool()
async def my_overdue(for_user: str, company: str | None = None) -> Any:
    """What is overdue for one staff person: their open tasks past due, and
    their open engagements with no next action.

    The assistant has no "me": always name the person. `for_user` must
    resolve to exactly one staff account; otherwise the ambiguity envelope
    comes back and nothing is read. Due times are in that person's zone.

    Args:
        for_user: The person's staff email or profile id (who is asking).
        company: Tenant id, when the email exists in several tenants.
    """
    return await _run(crm_core.my_overdue, for_user=for_user, company=company)


@mcp.tool()
async def my_day(for_user: str, company: str | None = None) -> Any:
    """One staff person's day: overdue tasks, tasks due by the end of their
    local day, and their open engagements that need a next action.

    Args:
        for_user: The person's staff email or profile id (who is asking).
        company: Tenant id, when the email exists in several tenants.
    """
    return await _run(crm_core.my_day, for_user=for_user, company=company)


@mcp.tool()
async def list_engagements(
    company: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    pipeline: str | None = None,
    stage: str | None = None,
    party: str | None = None,
    source: str | None = None,
    needs_next_action: bool | None = None,
    overdue: bool | None = None,
    stale: bool | None = None,
    no_party: bool | None = None,
    search: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List engagements (deals), in attention order: no next action first,
    then the next action due soonest.

    Args:
        company: Tenant id. Pass it: without it a cross-tenant key lists
            every tenant. An `owner` pins the tenant to theirs.
        status: "open", "won" or "lost".
        owner: Staff email or profile id (resolved, ambiguity envelope on
            several), or "none" for the unassigned queue.
        pipeline, stage, party: Ids.
        source: An engagement source key.
        needs_next_action: Open with no open task.
        overdue: Open with the next action past due.
        stale: Open with no activity for the stage's stale period.
        no_party: No counterparty (the exception queue).
        search: Number, title, or party name prefix.
        ordering: e.g. "-expected_value", "next_action_due_at".
        limit: Default 20; null fetches every page.
    """
    return await _run(
        crm_core.list_engagements, company=company, status=status, owner=owner, pipeline=pipeline,
        stage=stage, party=party, source=source, needs_next_action=needs_next_action,
        overdue=overdue, stale=stale, no_party=no_party, search=search, ordering=ordering, limit=limit,
    )


@mcp.tool()
async def find_engagement(query: str, company: str | None = None) -> Any:
    """Find an engagement by id, number ("EN-00001"), title or party name.

    "exact" for an id or number; one text hit is "fuzzy" (confirm before
    acting); several are "ambiguous" (`{"error": "ERPAmbiguousError", ...}`
    with candidates): ask the user which one.
    """
    return await _run(crm_core.find_engagement, query=query, company=company)


@mcp.tool()
async def get_engagement(engagement: str, company: str | None = None) -> Any:
    """The engagement thread summary: stage, status, owner, next action,
    last communication, open tasks, contacts (with their can_contact
    decision; erased people never appear) and the latest 20 timeline items
    (subjects and summaries, not message bodies).

    Args:
        engagement: Id or number (exact). A title returns the envelope.
        company: Tenant id.
    """
    return await _run(crm_core.get_engagement, engagement=engagement, company=company)


@mcp.tool()
async def draft_follow_up(
    engagement: str,
    channel: str = "email",
    intent: str | None = None,
    company: str | None = None,
) -> Any:
    """Propose a follow-up message for an engagement. WRITES NOTHING AND
    SENDS NOTHING.

    Returns the recipient and contact point (`to`), a plain draft
    `subject` / `body` for you to improve with the user, a
    `suggested_task`, the `consent` decision, and `apply`: the API calls a
    human confirms *after sending it themselves* (log the communication, set
    the next action). Refuses with `refused: "suppressed"` and no address
    when the recipient's address is on the do-not-contact list: do not look
    for another address; tell the user. Also `no_recipient` / `no_address`.

    Args:
        engagement: Id or number (exact).
        channel: "email" (default) or "sms".
        intent: What the follow-up is about, in a sentence (optional).
        company: Tenant id.
    """
    return await _run(crm_core.draft_follow_up, engagement=engagement, channel=channel,
                      intent=intent, company=company)


@mcp.tool()
async def create_task(
    title: str,
    engagement: str | None = None,
    assignee: str | None = None,
    due: str | None = None,
    due_tz: str | None = None,
    kind: str | None = None,
    priority: str | None = None,
    description: str | None = None,
    company: str | None = None,
) -> Any:
    """Create a CRM task (on an engagement it becomes the next action when it
    is the earliest due). Writes one task.

    `assignee` is a staff email or profile id: several matches or a name-only
    match return the ambiguity envelope and nothing is created. Ask the user.

    Args:
        title: What to do.
        engagement: Id or number (exact).
        assignee: Staff email or profile id; omit for the unassigned queue.
        due: "YYYY-MM-DD" (all day, end of that local day) or an ISO datetime.
        due_tz: IANA zone; default the assignee's.
        kind: Task kind key (e.g. "todo", "call", "follow_up").
        priority: "low", "normal" or "high".
        description: Details.
        company: Tenant id when there is no engagement or assignee.
    """
    return await _run(
        crm_core.create_task, title=title, engagement=engagement, assignee=assignee, due=due,
        due_tz=due_tz, kind=kind, priority=priority, description=description, company=company,
    )


@mcp.tool()
async def move_stage(
    engagement: str,
    stage: str,
    note: str | None = None,
    company: str | None = None,
    confirm: bool = False,
) -> Any:
    """Move an open engagement to another stage. Requires confirm=True.

    Without confirm=True nothing is written and a preview (from / to stage,
    `needs_close`) comes back: show it to the user first. Never wins or
    loses a deal: when `needs_close` is set, the user confirms the outcome
    in the ERP.

    Args:
        engagement: Id or number (exact).
        stage: Stage key or name in the engagement's pipeline, or a stage id
            (another pipeline's id changes the pipeline).
        note: Why (kept on the history row).
        company: Tenant id.
        confirm: Must be True to write.
    """
    return await _run(crm_core.move_stage, engagement=engagement, stage=stage, note=note,
                      company=company, confirm=confirm)


# ========== CRM: REPORTS ====================================================
#
# docs/crm/phase6-adapters.md 3.6 (Phase 6 step 7). The ERP's own report pages,
# as JSON in canonical nouns plus a deterministic `summary` the ERP writes.
# Reports are company-wide aggregates: no `for_user`, but `company` is
# required on every call (the assistant's key reads every tenant). Periods
# are local days in the site time zone, as on the pages.


@mcp.tool()
async def list_crm_reports() -> Any:
    """List the CRM reports (funnel, stage-duration, follow-ups): parameters,
    allowed values, column names and the definitions behind each number.
    Read this when unsure what a report counts."""
    return await _run(crm_core.list_crm_reports)


@mcp.tool()
async def crm_funnel_report(
    company: str,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    attribution: str | None = None,
) -> Any:
    """Engagement funnel by source: engagements created in the period, how
    many reached qualification, won, lost, closed without counting as a loss
    (disqualified / duplicate), still open, the win rate, and won value per
    currency (never converted).

    Returns `{report, company, params, data, summary}`. `data.rows` and
    `data.totals` are typed: counts are integers, `win_rate` a percent string
    ("66.7") or null, `won_value` a list of `{currency, amount, lower_bound,
    won_amount_known, won}`. `amount_incomplete: true` means some won
    engagements have no amount, so the value is a LOWER BOUND: say so ("at
    least"). `summary` is a plain statement of the data to rephrase; do not
    add numbers it does not hold.

    Args:
        company: Tenant id (uuid). Required.
        period: "today", "week", "month" (default), "quarter" or "year".
        date_from, date_to: "YYYY-MM-DD", together, instead of period (max 366 days).
        attribution: "engagement" (the engagement's source, default) or
            "first_touch" (the first touchpoint's channel).
    """
    return await _run(crm_core.crm_funnel_report, company=company, period=period,
                      date_from=date_from, date_to=date_to, attribution=attribution)


@mcp.tool()
async def crm_stage_report(
    company: str,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    pipeline: str | None = None,
) -> Any:
    """Stage duration and conversion for one pipeline: per stage, engagements
    that entered it in the period, median and 75th-percentile days in stage,
    how many are still there, advanced or won, and lost at that stage.

    Durations come only from observed stage history. `data.coverage.
    no_history_excluded` counts engagements left out because their only
    history was a backfill or import (time in stage unknown), and
    `reconstructed_visits` visits that were counted but not timed: mention
    both when non-zero. Days are decimal strings ("2.0"), null when unknown.

    Args:
        company: Tenant id (uuid). Required.
        period: "today", "week", "month" (default), "quarter" or "year".
        date_from, date_to: "YYYY-MM-DD", together, instead of period.
        pipeline: Pipeline key (e.g. "sales") or id; default the tenant's
            default pipeline. An unknown one returns the allowed keys.
    """
    return await _run(crm_core.crm_stage_report, company=company, period=period,
                      date_from=date_from, date_to=date_to, pipeline=pipeline)


@mcp.tool()
async def crm_follow_up_report(
    company: str,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Any:
    """Follow-up completion: tasks due in the period by assignee (completed on
    time, late, open and overdue, not yet due, cancelled, times rescheduled,
    on-time rate), plus right now: open engagements, how many have no next
    action, and how many next actions are overdue.

    Args:
        company: Tenant id (uuid). Required.
        period: "today", "week", "month" (default), "quarter" or "year".
        date_from, date_to: "YYYY-MM-DD", together, instead of period.
    """
    return await _run(crm_core.crm_follow_up_report, company=company, period=period,
                      date_from=date_from, date_to=date_to)


# ========== ORDERS ==========================================================


@mcp.tool()
async def list_orders(
    search: str | None = None,
    customer: str | None = None,
    status: str | None = None,
    payment_status: str | None = None,
    fulfillment_status: str | None = None,
    order_type: str | None = None,
    sales_rep: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List orders.

    Args:
        search: Matches order_number, customer email, and external_id.
        customer: Filter by customer UUID.
        status, payment_status, fulfillment_status: Independent status filters.
        order_type: Order type filter.
        sales_rep: Filter by sales rep UUID.
        ordering: DRF ordering ("-order_date", "-grand_total").
        limit: Pass null to fetch every page. Default 20.
    """
    return await _run(
        orders_core.list_orders,
        search=search,
        customer=customer,
        status=status,
        payment_status=payment_status,
        fulfillment_status=fulfillment_status,
        order_type=order_type,
        sales_rep=sales_rep,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def get_order(order_id: str) -> Any:
    """Fetch full order detail by UUID (includes line items and sub-orders)."""
    return await _run(orders_core.get_order, order_id=order_id)


@mcp.tool()
async def update_order(order_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing order.

    Common edits: payment_status, fulfillment_status, status_note,
    internal_notes, requested_ship_date, customer_message. A ``status``
    key is accepted but routed through the guarded transition action
    (POST .../transition/) — illegal moves return the server's field
    errors, legal moves apply normally.
    """
    return await _run(orders_core.update_order, order_id=order_id, fields=fields)


@mcp.tool()
async def update_order_status(
    order_id: str,
    status: str,
    status_note: str | None = None,
) -> Any:
    """Change an order's status (with optional note). Shortcut for the most common write.

    The status change goes through the guarded transition action, so the
    target must be a legal move per the order's transition map (e.g.
    "pending" → "awaiting_payment"); illegal moves fail with the server's
    field errors. The note is applied separately after the transition.
    """
    return await _run(
        orders_core.update_order_status,
        order_id=order_id,
        status=status,
        status_note=status_note,
    )


@mcp.tool()
async def send_order_confirmation(order_id: str) -> Any:
    """Trigger an order confirmation email via the DRF custom action."""
    return await _run(orders_core.send_order_confirmation, order_id=order_id)


@mcp.tool()
async def convert_order_to_invoice(order_id: str) -> Any:
    """Convert a Sales Order to a draft SalesInvoice. Idempotent — if the
    order already has an invoice, returns the existing one (response body
    will have `"created": false`); otherwise a fresh draft invoice is
    minted with the order's financials snapshotted and a new
    invoice_number. Payment terms are mapped from the order's free-text
    terms to the invoice's Net-15/30/45/60 / Prepaid / Due-on-Receipt
    enum, and due_date is computed from those terms when applicable.

    Args:
        order_id: UUID of the Sales Order to convert.
    """
    return await _run(orders_core.convert_order_to_invoice, order_id=order_id)


@mcp.tool()
async def duplicate_order(order_id: str) -> Any:
    """Duplicate an existing Sales Order. Creates a new draft order with the
    same customer, addresses, line items, terms, and metadata. Status,
    payment_status, fulfillment_status, financials, stripe refs, and
    confirmation tracking all reset; the new order's amended_from points
    at the source. Returns the new order's full detail (including its
    fresh order_number).

    Args:
        order_id: UUID of the order to duplicate.
    """
    return await _run(orders_core.duplicate_order, order_id=order_id)


@mcp.tool()
async def recalculate_order_totals(order_id: str) -> Any:
    """Trigger a server-side recalculation of all aggregate fields
    (subtotal, grand total, balance due) on a Sales Order based on
    current line items. Per-line line_total_ex_tax is also re-derived
    from quantity * unit_price - discount when stale.

    Args:
        order_id: UUID of the order to recalculate.
    """
    return await _run(orders_core.recalculate_order_totals, order_id=order_id)


@mcp.tool()
async def recalculate_invoice_totals(invoice_id: str) -> Any:
    """Trigger a server-side recalculation of all aggregate fields
    (subtotal, grand total, balance due) on a Sales Invoice. Re-snapshots
    financials from the parent Sales Order. No-op for PAID / VOID /
    CANCELLED invoices.

    Args:
        invoice_id: UUID of the sales invoice to recalculate.
    """
    return await _run(orders_core.recalculate_invoice_totals, invoice_id=invoice_id)


@mcp.tool()
async def list_order_lines(
    order: str | None = None,
    fulfillment_status: str | None = None,
    sub_order: str | None = None,
    search: str | None = None,
    limit: int | None = 50,
) -> Any:
    """List order line items, optionally scoped to one order or sub-order."""
    return await _run(
        orders_core.list_order_lines,
        order=order,
        fulfillment_status=fulfillment_status,
        sub_order=sub_order,
        search=search,
        limit=limit,
    )


@mcp.tool()
async def get_order_line(line_id: str) -> Any:
    """Fetch a single order line item by UUID."""
    return await _run(orders_core.get_order_line, line_id=line_id)


@mcp.tool()
async def update_order_line(line_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an order line (quantity_shipped, fulfillment_status, notes, etc.)."""
    return await _run(orders_core.update_order_line, line_id=line_id, fields=fields)


@mcp.tool()
async def list_sub_orders(
    order: str | None = None,
    status: str | None = None,
    is_drop_ship: bool | None = None,
    search: str | None = None,
    limit: int | None = 50,
) -> Any:
    """List sub-orders (shipments from an order)."""
    return await _run(
        orders_core.list_sub_orders,
        order=order,
        status=status,
        is_drop_ship=is_drop_ship,
        search=search,
        limit=limit,
    )


@mcp.tool()
async def get_sub_order(sub_order_id: str) -> Any:
    """Fetch a single sub-order by UUID (shipment details, tracking, etc.)."""
    return await _run(orders_core.get_sub_order, sub_order_id=sub_order_id)


@mcp.tool()
async def update_sub_order(sub_order_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on a sub-order.

    Common edits: tracking_number, tracking_url, carrier,
    actual_ship_date, actual_delivery_date, shipping_label_url. A
    ``status`` key is accepted but routed through the guarded transition
    action (POST .../transition/) — legal moves roll up to the parent
    order; illegal moves fail with the server's field errors.
    """
    return await _run(
        orders_core.update_sub_order, sub_order_id=sub_order_id, fields=fields
    )


@mcp.tool()
async def list_quotes(
    search: str | None = None,
    customer: str | None = None,
    status: str | None = None,
    sales_stage: str | None = None,
    sales_rep: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List quotes.

    Args:
        search: Matches quote_number and customer email.
        sales_stage: Deal stage filter, read from the quote's engagement:
                "prospecting", "qualification", "needs_analysis",
                "proposal_sent", "negotiation", "verbal_commit",
                "closed_won", "closed_lost". Deprecated (CRM Phase 3):
                the deal lives on the engagement.
        ordering: "-created_at", "-grand_total", etc.
    """
    return await _run(
        orders_core.list_quotes,
        search=search,
        customer=customer,
        status=status,
        sales_stage=sales_stage,
        sales_rep=sales_rep,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def get_quote(quote_id: str) -> Any:
    """Fetch full quote detail including nested line items."""
    return await _run(orders_core.get_quote, quote_id=quote_id)


@mcp.tool()
async def create_quote(
    customer: str,
    status: str = "draft",
    billing_address: str | None = None,
    shipping_address: str | None = None,
    sales_rep: str | None = None,
    price_list: str | None = None,
    currency: str = "USD",
    payment_terms: str | None = None,
    customer_notes: str | None = None,
    internal_notes: str | None = None,
    line_items: list[dict[str, Any]] | None = None,
) -> Any:
    """Create a new quote, optionally with nested line items.

    Follows the same find_* → confirm-fuzzy → write pattern used by the
    other create_* tools: resolve `billing_address`, `shipping_address`,
    `sales_rep`, and `price_list` to UUIDs *before* calling this (use
    `list_customer_addresses`, etc.), and confirm fuzzy matches with the user.

    Args:
        customer: Customer UUID, or an email / phone / name. Free text must
                match exactly one customer exactly; an ambiguous or fuzzy
                match returns `{"error": "ERPAmbiguousError", "candidates":
                [...]}` and creates nothing. Ask the user which candidate
                they mean, then call again with its id.
        status: Quote status — "draft", "sent", "viewed", "accepted",
                "expired", "lost", "cancelled", etc. Defaults to "draft".
                The deal's stage, probability and close date live on the
                quote's engagement (created with the quote), not here.
        billing_address: CustomerAddress UUID.
        shipping_address: CustomerAddress UUID.
        sales_rep: UserProfile UUID for the assigned rep.
        price_list: PriceList UUID to use for pricing lookup.
        currency: ISO 4217 code. Defaults to "USD".
        payment_terms: Free text proposed payment terms (e.g. "net_30",
                "due_on_receipt"). Stored on the Quote as
                `payment_terms_proposed`.
        customer_notes: Notes the customer will see on the rendered quote.
        internal_notes: Internal-only notes. Not shown to the customer.
        line_items: List of line item dicts. Each item supports:
                `variant` (UUID, required), `sku`, `item_name`, `quantity`,
                `unit_price_ex_tax`, `discount_percent`, `item_type`,
                `sort_order`, `notes` (mapped to line_notes on the model).

    Returns the refreshed quote detail (same shape as get_quote), with
    the auto-generated `quote_number` and `id` populated, plus any
    line items that were written.
    """
    return await _run(
        orders_core.create_quote,
        customer=customer,
        status=status,
        billing_address=billing_address,
        shipping_address=shipping_address,
        sales_rep=sales_rep,
        price_list=price_list,
        currency=currency,
        payment_terms=payment_terms,
        customer_notes=customer_notes,
        internal_notes=internal_notes,
        line_items=line_items,
    )


@mcp.tool()
async def duplicate_quote(quote_id: str) -> Any:
    """Duplicate an existing Quote. Creates a new draft quote with the
    same customer, addresses, line items, and proposed terms. Status
    resets to draft, conversion + acceptance audit fields are cleared, and
    the copy starts its own engagement (a duplicate is a new deal). The new
    quote's amended_from points at the source.

    Args:
        quote_id: UUID of the quote to duplicate.
    """
    return await _run(orders_core.duplicate_quote, quote_id=quote_id)


@mcp.tool()
async def recalculate_quote_totals(quote_id: str) -> Any:
    """Trigger a server-side recalculation of all aggregate fields
    (subtotal, grand total) on a Quote based on current line items.
    Per-line line_total_ex_tax is also re-derived from quantity *
    unit_price - discount when stale.

    Args:
        quote_id: UUID of the quote to recalculate.
    """
    return await _run(orders_core.recalculate_quote_totals, quote_id=quote_id)


@mcp.tool()
async def update_quote(quote_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing quote.

    Common edits: status_note, customer_notes, internal_notes, sales_rep.
    The deal's stage, probability, close date and loss reason moved to the
    quote's engagement; writing them here fails. A ``status`` key is
    accepted but routed through the guarded transition action
    (POST .../transition/) — legal moves follow the quote's transition map;
    illegal moves fail with the server's field errors.
    """
    return await _run(orders_core.update_quote, quote_id=quote_id, fields=fields)


@mcp.tool()
async def delete_quote(quote_id: str, confirm: bool = False) -> Any:
    """Permanently delete a quote. Requires confirm=True.

    Safe before conversion to an order. Do NOT call this on a quote that's
    been accepted — the resulting Order has a dangling FK and the DRF layer
    should refuse, but don't rely on that as your only safety net.
    """
    guard = _require_confirm(confirm, "delete quote")
    if guard is not None:
        return guard
    return await _run(orders_core.delete_quote, quote_id=quote_id)


@mcp.tool()
async def list_quote_lines(
    quote: str | None = None,
    is_optional: bool | None = None,
    is_alternative: bool | None = None,
    limit: int | None = 50,
) -> Any:
    """List quote line items, optionally scoped to one quote."""
    return await _run(
        orders_core.list_quote_lines,
        quote=quote,
        is_optional=is_optional,
        is_alternative=is_alternative,
        limit=limit,
    )


@mcp.tool()
async def get_quote_line(line_id: str) -> Any:
    """Fetch a single quote line item by UUID."""
    return await _run(orders_core.get_quote_line, line_id=line_id)


# ========== PURCHASING ======================================================


@mcp.tool()
async def list_suppliers(
    search: str | None = None,
    supplier_type: str | None = None,
    is_active: bool | None = None,
    is_approved: bool | None = None,
    ordering: str | None = None,
    limit: int | None = 50,
) -> Any:
    """List suppliers.

    `search` matches supplier_code and company_name.
    `ordering` examples: "company_name", "-created_at".
    """
    return await _run(
        purchasing_core.list_suppliers,
        search=search,
        supplier_type=supplier_type,
        is_active=is_active,
        is_approved=is_approved,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def find_supplier(query: str) -> Any:
    """Find a supplier by code or company name.

    Returns `{"match_quality": "exact" | "fuzzy", "item": {...}}`.
    """
    return await _run(purchasing_core.find_supplier, query=query)


@mcp.tool()
async def get_supplier(supplier_id: str) -> Any:
    """Fetch full supplier detail by UUID."""
    return await _run(purchasing_core.get_supplier, supplier_id=supplier_id)


@mcp.tool()
async def create_supplier(
    company_name: str,
    supplier_code: str,
    supplier_type: str = "manufacturer",
    primary_contact_email: str | None = None,
    primary_contact_name: str | None = None,
    primary_contact_phone: str | None = None,
    website: str | None = None,
    payment_terms: str | None = None,
    default_lead_time_days: int | None = None,
    is_active: bool = True,
) -> Any:
    """Create a new supplier."""
    return await _run(
        purchasing_core.create_supplier,
        company_name=company_name,
        supplier_code=supplier_code,
        supplier_type=supplier_type,
        primary_contact_email=primary_contact_email,
        primary_contact_name=primary_contact_name,
        primary_contact_phone=primary_contact_phone,
        website=website,
        payment_terms=payment_terms,
        default_lead_time_days=default_lead_time_days,
        is_active=is_active,
    )


@mcp.tool()
async def update_supplier(supplier_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing supplier."""
    return await _run(
        purchasing_core.update_supplier, supplier_id=supplier_id, fields=fields
    )


@mcp.tool()
async def list_purchase_orders(
    search: str | None = None,
    supplier: str | None = None,
    status: str | None = None,
    po_type: str | None = None,
    receiving_status: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List purchase orders.

    `search` matches po_number, supplier company_name, and tracking_number.
    `ordering`: "-po_date", "-grand_total".
    """
    return await _run(
        purchasing_core.list_purchase_orders,
        search=search,
        supplier=supplier,
        status=status,
        po_type=po_type,
        receiving_status=receiving_status,
        ordering=ordering,
        limit=limit,
    )


@mcp.tool()
async def get_purchase_order(po_id: str) -> Any:
    """Fetch full PO detail (includes nested line items)."""
    return await _run(purchasing_core.get_purchase_order, po_id=po_id)


@mcp.tool()
async def duplicate_purchase_order(po_id: str) -> Any:
    """Duplicate an existing Purchase Order. Creates a new draft PO with
    the same supplier, warehouse, line items, and commercial terms.
    Status resets to draft, receiving counts zero, tracking and vendor
    invoice refs clear, and amended_from points at the source. Source-
    order links (sub_order, order, shipment_group) are cleared because
    the duplicate is independent of the original demand.

    Args:
        po_id: UUID of the purchase order to duplicate.
    """
    return await _run(purchasing_core.duplicate_purchase_order, po_id=po_id)


@mcp.tool()
async def recalculate_purchase_order_totals(po_id: str) -> Any:
    """Trigger a server-side recalculation of all aggregate fields
    (subtotal, grand total, balance due) on a Purchase Order based on
    current line items. Per-line line_total is also re-derived from
    quantity_ordered * unit_cost - discount when stale.

    Args:
        po_id: UUID of the purchase order to recalculate.
    """
    return await _run(
        purchasing_core.recalculate_purchase_order_totals, po_id=po_id
    )


@mcp.tool()
async def update_purchase_order(po_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing PO.

    Common edits: status_note, vendor_estimated_ship_date, tracking_number,
    carrier, actual_ship_date, actual_delivery_date, vendor_invoice_number,
    internal_notes. A ``status`` key is accepted but routed through the
    guarded transition action (POST .../transition/) — legal moves follow
    the PO's transition map; illegal moves fail with the server's field
    errors.
    """
    return await _run(
        purchasing_core.update_purchase_order, po_id=po_id, fields=fields
    )


@mcp.tool()
async def list_po_lines(
    purchase_order: str | None = None,
    receiving_status: str | None = None,
    search: str | None = None,
    limit: int | None = 50,
) -> Any:
    """List PO line items, optionally scoped to one PO."""
    return await _run(
        purchasing_core.list_po_lines,
        purchase_order=purchase_order,
        receiving_status=receiving_status,
        search=search,
        limit=limit,
    )


@mcp.tool()
async def get_po_line(line_id: str) -> Any:
    """Fetch a single PO line item by UUID."""
    return await _run(purchasing_core.get_po_line, line_id=line_id)


@mcp.tool()
async def update_po_line(line_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on a PO line.

    Common edits: quantity_received, quantity_rejected, actual_received_date,
    receiving_status, unit_cost, landed_cost_per_unit, notes.
    """
    return await _run(purchasing_core.update_po_line, line_id=line_id, fields=fields)


@mcp.tool()
async def list_receipts(
    purchase_order: str | None = None,
    status: str | None = None,
    condition_on_arrival: str | None = None,
    search: str | None = None,
    limit: int | None = 20,
) -> Any:
    """List PO receipts, optionally scoped to one PO."""
    return await _run(
        purchasing_core.list_receipts,
        purchase_order=purchase_order,
        status=status,
        condition_on_arrival=condition_on_arrival,
        search=search,
        limit=limit,
    )


@mcp.tool()
async def get_receipt(receipt_id: str) -> Any:
    """Fetch full receipt detail (includes nested receipt lines)."""
    return await _run(purchasing_core.get_receipt, receipt_id=receipt_id)


@mcp.tool()
async def create_receipt(
    purchase_order: str,
    warehouse: str | None = None,
    status: str = "received",
    carrier: str | None = None,
    tracking_number: str | None = None,
    bol_number: str | None = None,
    package_count: int | None = None,
    condition_on_arrival: str | None = None,
    notes: str | None = None,
) -> Any:
    """Create a new PO receipt.

    Args:
        purchase_order: UUID of the PO being received.
        warehouse: UUID of the receiving warehouse.
        status: "received", "partial", "damaged", etc.
        condition_on_arrival: "good", "damaged", "short", etc.
    """
    return await _run(
        purchasing_core.create_receipt,
        purchase_order=purchase_order,
        warehouse=warehouse,
        status=status,
        carrier=carrier,
        tracking_number=tracking_number,
        bol_number=bol_number,
        package_count=package_count,
        condition_on_arrival=condition_on_arrival,
        notes=notes,
    )


@mcp.tool()
async def update_receipt(receipt_id: str, fields: dict[str, Any]) -> Any:
    """Patch fields on an existing receipt."""
    return await _run(
        purchasing_core.update_receipt, receipt_id=receipt_id, fields=fields
    )


# ========== ENTRY POINT =====================================================


def main() -> None:
    """Entry point: `python -m erp_assist.mcp_adapter` (or a console script a deployment defines)."""
    mcp.run()


if __name__ == "__main__":
    main()
