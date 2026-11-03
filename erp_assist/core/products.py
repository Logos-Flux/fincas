"""Products, variants, brands, and categories.

All functions take an ERPClient instance and return plain dicts from the DRF API.
Lookup helpers (find_*) accept fuzzy strings and return a match envelope
`{"match_quality": "exact" | "fuzzy", "item": {...}}` so callers can decide
whether to proceed — useful for write paths that shouldn't silently land on
the wrong row.
"""

from __future__ import annotations

import re
from typing import Any

from ._cache import cached_fetch, make_lookup_key
from ._writable_fields import (
    BRAND_WRITABLE,
    PRODUCT_VARIANT_WRITABLE,
    PRODUCT_WRITABLE,
    validate_fields,
)
from .client import ERPClient, ERPNotFoundError


def _match(item: dict[str, Any], quality: str) -> dict[str, Any]:
    """Wrap a matched item in the standard match-quality envelope."""
    return {"match_quality": quality, "item": item}


# ---- Products --------------------------------------------------------------

PRODUCTS_URL = "/api/v1/products/products/"
VARIANTS_URL = "/api/v1/products/variants/"
BRANDS_URL = "/api/v1/products/brands/"
CATEGORIES_URL = "/api/v1/products/categories/"


async def list_products(
    client: ERPClient,
    *,
    search: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    product_type: str | None = None,
    is_active: bool | None = None,
    visible_on_website: bool | None = None,
    detail: bool = False,
    ordering: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List products with optional filters.

    `brand` and `category` each accept a UUID or a title/slug fragment — if it
    doesn't look like a UUID we resolve it via find_brand / find_category first.
    Pass `limit=None` to fetch every page.
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if product_type is not None:
        params["product_type"] = product_type
    if is_active is not None:
        params["is_active"] = str(is_active).lower()
    if visible_on_website is not None:
        params["visible_on_website"] = str(visible_on_website).lower()
    if detail:
        params["detail"] = "true"
    if brand:
        params["brand"] = await _resolve_brand_id(client, brand)
    if category:
        params["categories"] = await _resolve_category_id(client, category)
    if ordering:
        params["ordering"] = ordering
    return await client.list(PRODUCTS_URL, params=params, max_items=limit)


async def find_product(client: ERPClient, query: str) -> dict[str, Any]:
    """Find a single product by title, slug, or part number.

    Returns `{match_quality: "exact"|"fuzzy", item: {...}}`. "exact" means a
    case-insensitive match on `part_number` or `title`; "fuzzy" means the
    first search hit. Write paths should refuse to proceed on "fuzzy" results
    without user confirmation.

    Raises ERPNotFoundError if no candidates are returned at all.
    """
    candidates = await client.list(
        PRODUCTS_URL, params={"search": query, "page_size": 5}, max_items=5
    )
    if not candidates:
        raise ERPNotFoundError(f"No product found matching '{query}'")
    q = query.lower()
    for c in candidates:
        if (c.get("part_number") or "").lower() == q:
            return _match(c, "exact")
    for c in candidates:
        if (c.get("title") or "").lower() == q:
            return _match(c, "exact")
    return _match(candidates[0], "fuzzy")


async def get_product(client: ERPClient, product_id: str) -> dict[str, Any]:
    """Fetch a single product by UUID (detail serializer, includes variants/images)."""
    return await client.get(f"{PRODUCTS_URL}{product_id}/")


async def create_product(
    client: ERPClient,
    *,
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
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new product.

    `brand` and each entry in `categories` may be a UUID or a name/slug.
    `slug` is derived from `title` if omitted. `product_type` must be one
    of "physical", "digital", "service", "kit", or "bom" — matches the
    Product.ProductType TextChoices in Django.
    """
    brand_id = await _resolve_brand_id(client, brand)
    category_ids: list[str] = []
    if categories:
        for c in categories:
            category_ids.append(await _resolve_category_id(client, c))

    payload: dict[str, Any] = {
        "title": title,
        "slug": slug or _slugify(title),
        "part_number": part_number,
        "brand": brand_id,
        "product_type": product_type,
        "is_active": is_active,
        "visible_on_website": visible_on_website,
        "has_variants": has_variants,
    }
    if description is not None:
        payload["description"] = description
    if short_description is not None:
        payload["short_description"] = short_description
    if category_ids:
        payload["categories"] = category_ids
    if extra_fields:
        payload.update(extra_fields)

    return await client.post(PRODUCTS_URL, json=payload)


async def update_product(
    client: ERPClient, product_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing product (partial update). Unknown fields raise."""
    validate_fields(fields, PRODUCT_WRITABLE, "product")
    return await client.patch(f"{PRODUCTS_URL}{product_id}/", json=fields)


async def set_product_visibility(
    client: ERPClient, product_id: str, *, visible: bool
) -> dict[str, Any]:
    """Convenience wrapper: toggle visible_on_website."""
    return await update_product(client, product_id, {"visible_on_website": visible})


# ---- Variants --------------------------------------------------------------


async def list_variants(
    client: ERPClient,
    *,
    product_id: str | None = None,
    search: str | None = None,
    is_active: bool | None = None,
    stock_status: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List variants. `limit=None` fetches every page; default 20."""
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if product_id:
        params["product"] = product_id
    if search:
        params["search"] = search
    if is_active is not None:
        params["is_active"] = str(is_active).lower()
    if stock_status:
        params["stock_status"] = stock_status
    if ordering:
        params["ordering"] = ordering
    return await client.list(VARIANTS_URL, params=params, max_items=limit)


async def find_variant_by_sku(client: ERPClient, sku: str) -> dict[str, Any]:
    """Find a variant by SKU. Returns the match envelope `{match_quality, item}`."""
    hits = await client.list(VARIANTS_URL, params={"search": sku, "page_size": 5}, max_items=5)
    for h in hits:
        if (h.get("sku") or "").lower() == sku.lower():
            return _match(h, "exact")
    if hits:
        return _match(hits[0], "fuzzy")
    raise ERPNotFoundError(f"No variant found for SKU '{sku}'")


async def get_variant(client: ERPClient, variant_id: str) -> dict[str, Any]:
    return await client.get(f"{VARIANTS_URL}{variant_id}/")


async def update_variant(
    client: ERPClient, variant_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing variant. Unknown fields raise."""
    validate_fields(fields, PRODUCT_VARIANT_WRITABLE, "variant")
    return await client.patch(f"{VARIANTS_URL}{variant_id}/", json=fields)


async def update_variant_price(
    client: ERPClient,
    variant_id: str,
    *,
    retail_price: float | None = None,
    sale_price: float | None = None,
    msrp_price: float | None = None,
    map_price: float | None = None,
    wholesale_price: float | None = None,
) -> dict[str, Any]:
    """Update one or more of the default prices stored on the variant itself.

    Note: for price-list specific pricing (dealer tier, MAP, etc.), use pricing.update_price_list_item.
    """
    fields: dict[str, Any] = {}
    if retail_price is not None:
        fields["retail_price"] = retail_price
    if sale_price is not None:
        fields["sale_price"] = sale_price
    if msrp_price is not None:
        fields["msrp_price"] = msrp_price
    if map_price is not None:
        fields["map_price"] = map_price
    if wholesale_price is not None:
        fields["wholesale_price"] = wholesale_price
    if not fields:
        raise ValueError("update_variant_price called with no prices to update")
    return await update_variant(client, variant_id, fields)


# ---- Bulk operations ------------------------------------------------------


async def bulk_update_variants(
    client: ERPClient,
    updates: list[dict[str, Any]],
    *,
    concurrency: int = 8,
) -> dict[str, Any]:
    """Apply a batch of variant updates in parallel.

    `updates` is a list of `{"variant_id": str, "fields": {...}}` dicts. Each
    row is validated and PATCHed independently — rows with unknown fields fail
    but don't abort the rest of the batch. This is meant for campaign-style
    catalog operations ("drop all CW Energy retail prices by 5%") where you
    want partial success to still land rather than all-or-nothing.

    Returns `{"updated": [...], "failed": [...], "summary": {...}}`.
        - `updated`: list of successful rows, each `{variant_id, item}` where
          `item` is the patched variant dict.
        - `failed`: list of failed rows, each `{variant_id, error, message}`.
        - `summary`: `{total, succeeded, failed_count}`.
    """
    import asyncio as _asyncio

    from .client import ERPError

    sem = _asyncio.Semaphore(concurrency)

    async def _one(row: dict[str, Any]) -> dict[str, Any]:
        variant_id = row.get("variant_id")
        fields = row.get("fields", {})
        if not variant_id or not isinstance(fields, dict):
            return {
                "variant_id": variant_id,
                "error": "ValueError",
                "message": "row must have 'variant_id' and 'fields' dict",
            }
        async with sem:
            try:
                item = await update_variant(client, variant_id, fields)
                return {"variant_id": variant_id, "item": item}
            except ERPError as exc:
                return {
                    "variant_id": variant_id,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }

    results = await _asyncio.gather(*(_one(row) for row in updates))
    updated = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    return {
        "updated": updated,
        "failed": failed,
        "summary": {
            "total": len(updates),
            "succeeded": len(updated),
            "failed_count": len(failed),
        },
    }


async def bulk_update_variant_prices(
    client: ERPClient,
    updates: list[dict[str, Any]],
    *,
    concurrency: int = 8,
) -> dict[str, Any]:
    """Apply a batch of variant *price* updates in parallel.

    `updates` is a list of `{variant_id, retail_price?, sale_price?, msrp_price?,
    map_price?, wholesale_price?}` dicts. At least one price must be provided
    per row. Rows with no prices are rejected without a network call.

    Returns the same shape as bulk_update_variants.
    """
    price_fields = {
        "retail_price",
        "sale_price",
        "msrp_price",
        "map_price",
        "wholesale_price",
    }
    normalised: list[dict[str, Any]] = []
    reject_rows: list[dict[str, Any]] = []
    for row in updates:
        variant_id = row.get("variant_id")
        fields = {k: row[k] for k in price_fields if k in row and row[k] is not None}
        if not variant_id:
            reject_rows.append(
                {
                    "variant_id": None,
                    "error": "ValueError",
                    "message": "row missing variant_id",
                }
            )
            continue
        if not fields:
            reject_rows.append(
                {
                    "variant_id": variant_id,
                    "error": "ValueError",
                    "message": "row has no price fields to update",
                }
            )
            continue
        normalised.append({"variant_id": variant_id, "fields": fields})

    result = await bulk_update_variants(client, normalised, concurrency=concurrency)
    # Merge the pre-validation rejects into the failed list
    result["failed"].extend(reject_rows)
    result["summary"]["failed_count"] = len(result["failed"])
    result["summary"]["total"] = len(updates)
    return result


# ---- Brands ----------------------------------------------------------------


async def list_brands(
    client: ERPClient,
    *,
    search: str | None = None,
    visible_on_website: bool | None = None,
    ordering: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List brands.

    `limit=None` (the default) fetches *all* brands across every page.
    Pass an int to cap the result. `ordering` is forwarded to DRF (e.g.
    "title", "-sort_order", "-created_at").
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if visible_on_website is not None:
        params["visible_on_website"] = str(visible_on_website).lower()
    if ordering:
        params["ordering"] = ordering
    return await client.list(BRANDS_URL, params=params, max_items=limit)


async def find_brand(client: ERPClient, query: str) -> dict[str, Any]:
    """Find a brand by title or slug. Returns the match envelope `{match_quality, item}`.

    Brand lookups are cached for 5 minutes on the shared ERPClient to avoid
    hitting Django on every call — the brand list changes maybe once a month.
    """
    key = make_lookup_key("brand_search", q=query)
    hits = await cached_fetch(
        client.lookup_cache,
        key,
        lambda: client.list(BRANDS_URL, params={"search": query, "page_size": 10}, max_items=10),
    )
    q = query.lower()
    for h in hits:
        if (h.get("title") or "").lower() == q or (h.get("slug") or "").lower() == q:
            return _match(h, "exact")
    if hits:
        return _match(hits[0], "fuzzy")
    raise ERPNotFoundError(f"No brand found matching '{query}'")


async def get_brand(client: ERPClient, brand_id: str) -> dict[str, Any]:
    """Fetch a single brand by UUID."""
    return await client.get(f"{BRANDS_URL}{brand_id}/")


def _slugify(value: str) -> str:
    """Minimal ASCII slugify: lowercase, non-alphanumeric → hyphens, trimmed.

    Intentionally self-contained (no Django dependency) so the package can
    be used outside the ERP app. If the input contains nothing slug-safe,
    returns an empty string — DRF will reject that with a validation error,
    which is the right behavior.
    """
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


async def create_brand(
    client: ERPClient,
    *,
    title: str,
    slug: str | None = None,
    description: str | None = None,
    website_url: str | None = None,
    visible_on_website: bool = True,
    is_authorized_dealer: bool = False,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new brand. `slug` is derived from `title` if omitted.

    `extra_fields` is an escape hatch for SEO fields, sort_order, and image
    IDs that aren't in the common-case signature — any key allowed by
    BRAND_WRITABLE is accepted.
    """
    payload: dict[str, Any] = {
        "title": title,
        "slug": slug or _slugify(title),
        "visible_on_website": visible_on_website,
        "is_authorized_dealer": is_authorized_dealer,
    }
    if description is not None:
        payload["description"] = description
    if website_url is not None:
        payload["website_url"] = website_url
    if extra_fields:
        payload.update(extra_fields)
    return await client.post(BRANDS_URL, json=payload)


async def update_brand(
    client: ERPClient, brand_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing brand (partial update). Unknown fields raise."""
    validate_fields(fields, BRAND_WRITABLE, "brand")
    return await client.patch(f"{BRANDS_URL}{brand_id}/", json=fields)


# ---- Categories ------------------------------------------------------------


async def list_categories(
    client: ERPClient,
    *,
    search: str | None = None,
    parent: str | None = None,
    top_level_only: bool = False,
    visible_on_website: bool | None = None,
    ordering: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List categories.

    `limit=None` (the default) fetches *all* categories across every page — the
    previous default cap of 100 hid rows when the tree grew past that count.

    `top_level_only=True` filters to parent categories only (tree_depth=0) —
    use this when you want the root of the category tree without slogging
    through alphabetised subcategories.

    `ordering` is forwarded to DRF — "sort_order" groups categories the way the
    ERP admin UI shows them, "title" is alphabetical, "tree_depth,title" lists
    top-level first then subcategories grouped under them.
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if parent is not None:
        params["parent"] = parent
    if top_level_only:
        params["tree_depth"] = 0
    if visible_on_website is not None:
        params["visible_on_website"] = str(visible_on_website).lower()
    if ordering:
        params["ordering"] = ordering
    return await client.list(CATEGORIES_URL, params=params, max_items=limit)


async def find_category(client: ERPClient, query: str) -> dict[str, Any]:
    """Find a category by title or slug. Returns the match envelope.

    Cached for 5 minutes on the shared client (see find_brand docstring).
    """
    key = make_lookup_key("category_search", q=query)
    hits = await cached_fetch(
        client.lookup_cache,
        key,
        lambda: client.list(CATEGORIES_URL, params={"search": query, "page_size": 10}, max_items=10),
    )
    q = query.lower()
    for h in hits:
        if (h.get("title") or "").lower() == q or (h.get("slug") or "").lower() == q:
            return _match(h, "exact")
    if hits:
        return _match(hits[0], "fuzzy")
    raise ERPNotFoundError(f"No category found matching '{query}'")


async def get_category(client: ERPClient, category_id: str) -> dict[str, Any]:
    """Fetch a single category by UUID."""
    return await client.get(f"{CATEGORIES_URL}{category_id}/")


# ---- Internal helpers ------------------------------------------------------


def _looks_like_uuid(s: str) -> bool:
    return len(s) == 36 and s.count("-") == 4


async def _resolve_brand_id(client: ERPClient, query: str) -> str:
    if _looks_like_uuid(query):
        return query
    result = await find_brand(client, query)
    return result["item"]["id"]


async def _resolve_category_id(client: ERPClient, query: str) -> str:
    if _looks_like_uuid(query):
        return query
    result = await find_category(client, query)
    return result["item"]["id"]
