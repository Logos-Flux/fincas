"""Guest checkout cart lines (OP #3426): validate, price and stock-check.

The storefront posts ``lines: [{"variant": <id>, "quantity": <int>}]`` (or
``{"product": <id>, ...}`` for a product with a single sellable variant, which
is what a catalog card knows). Everything that decides money is the server's:

- **Price** is the variant's public selling price, the one the catalog shows
  (``products.serializers.unit_price``: a non-zero sale price, else retail).
  Price lists and quantity breaks are not consulted, because nothing in
  golden resolves them for a public buyer yet (the catalog does not either,
  so applying them here would charge a price the shopper never saw).
- **Tax** follows the existing rules: golden has no tax engine, and a guest
  order has no structured address, so lines carry no tax. Staff add tax and
  shipping in the order editor, as for any order.
- **Stock** is checked, never reserved (owner decision, OP #3426): a line is
  refused when the variant is out of stock or the quantity exceeds its stock,
  unless the variant is on preorder or backorder. No ``WarehouseStock`` row
  is touched; stock is committed through the normal back-office flow.

Every refusal carries a stable ``code`` (and the line's index and ids) so the
storefront branches on codes, never on messages. Validation happens before
anything is written, so a refused checkout creates nothing and burns no
order number.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone

from products.models import Product, ProductVariant
from products.serializers import unit_price

#: At most this many lines per checkout (after nothing is merged).
MAX_LINES = 50
#: At most this many units of one variant (after duplicates are merged).
MAX_QUANTITY = 9999

# Request-level codes.
LINES_REQUIRED = "lines_required"
TOO_MANY_LINES = "too_many_lines"
INVALID_LINES = "invalid_lines"
#: Also a per-line code: a line (or the order) worth more than its columns hold.
ORDER_TOO_LARGE = "order_too_large"

# Per-line codes.
INVALID_LINE = "invalid_line"
INVALID_QUANTITY = "invalid_quantity"
NOT_AVAILABLE = "not_available"
VARIANT_REQUIRED = "variant_required"
PRICE_UNAVAILABLE = "price_unavailable"
OUT_OF_STOCK = "out_of_stock"
INSUFFICIENT_STOCK = "insufficient_stock"

LINE_CODES = (
    INVALID_LINE, INVALID_QUANTITY, NOT_AVAILABLE, VARIANT_REQUIRED,
    PRICE_UNAVAILABLE, OUT_OF_STOCK, INSUFFICIENT_STOCK, ORDER_TOO_LARGE,
)

_MESSAGES = {
    INVALID_LINE: "Each line needs a variant (or a product) and a quantity.",
    INVALID_QUANTITY: f"Quantity must be a whole number from 1 to {MAX_QUANTITY}.",
    NOT_AVAILABLE: "This item is not available for online ordering.",
    VARIANT_REQUIRED: "This product comes in several options; choose one.",
    PRICE_UNAVAILABLE: "This item has no online price; ask us for a quote.",
    OUT_OF_STOCK: "This item is out of stock.",
    INSUFFICIENT_STOCK: "Not enough of this item is in stock.",
    ORDER_TOO_LARGE: "This line is too large for an online order; ask us for a quote.",
}

#: Stock statuses that may be ordered beyond the units on hand.
_ORDERABLE_WITHOUT_STOCK = (
    ProductVariant.StockStatus.PREORDER,
    ProductVariant.StockStatus.BACKORDER,
)


def _column_max(model, field_name) -> Decimal:
    """The largest value a DecimalField column stores (e.g. 99999999.99)."""
    field = model._meta.get_field(field_name)
    places = field.decimal_places
    return Decimal(10) ** (field.max_digits - places) - Decimal(1).scaleb(-places)


def money_limits() -> tuple[Decimal, Decimal, Decimal]:
    """(max unit price, max line total, max order total) the columns hold.

    Read from the models, so a column change moves the limits with it. The
    order total is the smallest of the order columns ``recalculate_totals``
    writes the subtotal into; a value past any of them would raise
    ``decimal.InvalidOperation`` on save (a 500).
    """
    from orders.models import Order, OrderLineItem

    unit = _column_max(OrderLineItem, "unit_price_ex_tax")
    line = min(_column_max(OrderLineItem, f) for f in ("line_total_ex_tax", "line_total_inc_tax"))
    order = min(
        _column_max(Order, f)
        for f in ("subtotal_ex_tax", "subtotal_inc_tax", "grand_total", "balance_due")
    )
    return unit, line, order


class CheckoutRefused(Exception):
    """The checkout cannot be accepted. ``body`` is the 400 response."""

    def __init__(self, code: str, detail: str, lines: list[dict] | None = None):
        super().__init__(detail)
        self.body: dict = {"code": code, "detail": detail}
        if lines is not None:
            self.body["lines"] = lines


@dataclass
class PricedLine:
    """One accepted line: a sellable variant, its merged quantity and price."""

    index: int
    variant: ProductVariant
    quantity: int
    unit_price: Decimal
    indexes: list[int] = field(default_factory=list)

    @property
    def line_total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))


def _line_error(index, code, raw=None, **extra) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    error = {
        "index": index,
        "code": code,
        "detail": _MESSAGES[code],
        "variant": _as_text(raw.get("variant")),
        "product": _as_text(raw.get("product")),
    }
    error.update(extra)
    return error


def _as_text(value):
    return None if value is None else str(value)


def _as_uuid(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _as_quantity(value):
    """A positive whole number, or None. Booleans and strings are refused."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        quantity = value
    elif isinstance(value, float) and value.is_integer():
        quantity = int(value)
    else:
        return None
    return quantity if 1 <= quantity <= MAX_QUANTITY else None


def _sellable(variant: ProductVariant) -> bool:
    product = variant.product
    return bool(
        variant.is_active and variant.visible_on_website
        and product.is_active and product.visible_on_website
    )


def price_lines(raw_lines) -> list[PricedLine]:
    """Validate the posted lines and price them; raise ``CheckoutRefused``.

    Duplicates (two lines for one variant, including a product line that
    resolves to a variant listed elsewhere) are merged into the first line
    with their quantities summed, and the merged quantity is what the
    quantity cap and the stock check see.
    """
    if not isinstance(raw_lines, list) or not raw_lines:
        raise CheckoutRefused(LINES_REQUIRED, "A guest order needs at least one line.")
    if len(raw_lines) > MAX_LINES:
        raise CheckoutRefused(
            TOO_MANY_LINES, f"A guest order takes at most {MAX_LINES} lines."
        )

    errors: list[dict] = []
    parsed = []  # (index, raw, variant_uuid | None, product_uuid | None, quantity)
    for index, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            errors.append(_line_error(index, INVALID_LINE))
            continue
        has_variant = raw.get("variant") not in (None, "")
        has_product = raw.get("product") not in (None, "")
        if not has_variant and not has_product:
            errors.append(_line_error(index, INVALID_LINE, raw))
            continue
        quantity = _as_quantity(raw.get("quantity"))
        if quantity is None:
            errors.append(_line_error(index, INVALID_QUANTITY, raw))
            continue
        variant_id = _as_uuid(raw.get("variant")) if has_variant else None
        product_id = _as_uuid(raw.get("product")) if not has_variant else None
        if variant_id is None and product_id is None:
            # A malformed id reads exactly like an unknown one.
            errors.append(_line_error(index, NOT_AVAILABLE, raw))
            continue
        parsed.append((index, raw, variant_id, product_id, quantity))

    variant_ids = {v for _, _, v, _, _ in parsed if v}
    product_ids = {p for _, _, _, p, _ in parsed if p}
    variants = {
        v.pk: v
        for v in ProductVariant.objects.select_related("product").filter(pk__in=variant_ids)
    } if variant_ids else {}
    product_variants: dict = {}
    if product_ids:
        for product in Product.objects.filter(pk__in=product_ids).prefetch_related("variants"):
            product_variants[product.pk] = [
                v for v in product.variants.all()
                if v.is_active and v.visible_on_website and product.is_active
                and product.visible_on_website
            ]
            for v in product_variants[product.pk]:
                v.product = product  # already loaded; avoid a query per line

    merged: dict = {}
    for index, raw, variant_id, product_id, quantity in parsed:
        if variant_id is not None:
            variant = variants.get(variant_id)
            if variant is None or not _sellable(variant):
                errors.append(_line_error(index, NOT_AVAILABLE, raw))
                continue
        else:
            choices = product_variants.get(product_id, [])
            if not choices:
                errors.append(_line_error(index, NOT_AVAILABLE, raw))
                continue
            if len(choices) > 1:
                errors.append(_line_error(index, VARIANT_REQUIRED, raw))
                continue
            variant = choices[0]
        price = unit_price(variant)
        if price is None or Decimal(price) <= 0:
            # No price, or a zero/negative one: never sold for nothing.
            errors.append(_line_error(index, PRICE_UNAVAILABLE, raw, variant=str(variant.pk)))
            continue
        line = merged.get(variant.pk)
        if line is None:
            merged[variant.pk] = PricedLine(
                index=index, variant=variant, quantity=quantity,
                unit_price=Decimal(price), indexes=[index],
            )
        else:
            line.quantity += quantity
            line.indexes.append(index)

    max_unit, max_line, max_order = money_limits()
    for line in merged.values():
        raw = {"variant": str(line.variant.pk)}
        if line.quantity > MAX_QUANTITY:
            errors.append(_line_error(line.index, INVALID_QUANTITY, raw))
            continue
        if line.unit_price > max_unit or line.line_total > max_line:
            errors.append(_line_error(line.index, ORDER_TOO_LARGE, raw))
            continue
        error = _stock_error(line, raw)
        if error:
            errors.append(error)

    if errors:
        errors.sort(key=lambda e: e["index"])
        raise CheckoutRefused(
            INVALID_LINES, "Some lines cannot be ordered; nothing was placed.", errors
        )
    if sum(line.line_total for line in merged.values()) > max_order:
        raise CheckoutRefused(
            ORDER_TOO_LARGE, "This order is too large to place online; ask us for a quote."
        )
    return sorted(merged.values(), key=lambda line: line.index)


def _stock_error(line: PricedLine, raw: dict) -> dict | None:
    """Check only: never reserves or writes stock (OP #3426)."""
    variant = line.variant
    if variant.stock_status in _ORDERABLE_WITHOUT_STOCK:
        return None
    available = max(int(variant.stock or 0), 0)
    if variant.stock_status == ProductVariant.StockStatus.OUT_OF_STOCK or available <= 0:
        return _line_error(line.index, OUT_OF_STOCK, raw, available=0)
    if line.quantity > available:
        return _line_error(line.index, INSUFFICIENT_STOCK, raw, available=available)
    return None


def item_name(variant: ProductVariant) -> str:
    """What the back office's variant picker writes (``v.name or title``)."""
    return (variant.name or variant.product.title)[:255]


# --- idempotency (OP #3431) ---------------------------------------------------
#
# The storefront's checkout POST can time out after the order committed; the
# shopper retries. The storefront sends one ``Idempotency-Key`` per checkout
# attempt, and the order stores ``storefront:<sha256(key)>:<fingerprint>`` in
# ``external_id``. A repeat with the same key inside the window and the same
# email and lines answers the original confirmation (200); the same key with a
# different checkout is refused (409). Every browser shares the storefront
# identity, so the key itself is the only scope: a client generates it with
# 122 random bits (``crypto.randomUUID``), and the fingerprint means even a
# leaked key only replays the checkout it was made for.

IDEMPOTENCY_KEY_INVALID = "idempotency_key_invalid"
IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
IDEMPOTENCY_WINDOW = timedelta(hours=24)
#: How long a same-key submit waits for the first one (Postgres ``lock_timeout``).
LOCK_WAIT = "5s"
CHECKOUT_IN_PROGRESS = "checkout_in_progress"
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,200}$")
_REF_PREFIX = "storefront:"


def idempotency_ref(raw_key) -> str | None:
    """The stored prefix for a client ``Idempotency-Key``; None when absent."""
    key = (raw_key or "").strip()
    if not key:
        return None
    if not _KEY_RE.match(key):
        raise CheckoutRefused(
            IDEMPOTENCY_KEY_INVALID,
            "Idempotency-Key must be 16-200 characters of letters, digits, '.', '_', ':' or '-'.",
        )
    return f"{_REF_PREFIX}{hashlib.sha256(key.encode()).hexdigest()}:"


#: Posted header fields the guest order stores, so a replay must match them.
FINGERPRINT_FIELDS = (
    "customer_name", "company_name", "shipping_address", "payment_method",
    "coupon_code", "po_number", "customer_po_number", "notes", "customer_message",
)


def checkout_fingerprint(email: str, body) -> str:
    """What makes two submits the same checkout: everything the order stores.

    The email, the lines and every stored header field, taken from the request
    as posted (not the priced lines), so a replay is recognised even when a
    line has since gone out of stock or changed price, and a resubmit that
    corrects the address or adds a coupon is refused (409) instead of being
    answered with the old order as if the correction had landed.
    """
    raw_lines = body.get("lines") if hasattr(body, "get") else None
    lines = []
    for raw in raw_lines if isinstance(raw_lines, list) else []:
        if not isinstance(raw, dict):
            lines.append(["?", "?", ""])
            continue
        quantity = _as_quantity(raw.get("quantity"))
        lines.append([str(raw.get("variant") or ""), str(raw.get("product") or ""),
                      "" if quantity is None else str(quantity)])
    fields = []
    for name in FINGERPRINT_FIELDS:
        value = body.get(name) if hasattr(body, "get") else None
        fields.append(value.strip() if isinstance(value, str) else "")
    canonical = json.dumps([email.strip().lower(), sorted(lines), fields],
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def lock_idempotency_ref(ref: str) -> None:
    """Serialise submits that share a key, inside the caller's transaction.

    Two concurrent first submits would both miss the lookup and both insert;
    the transaction-scoped advisory lock makes the second wait for the first
    to commit, then find its order. This relies on READ COMMITTED (Django's
    default, and golden sets no ``ATOMIC_REQUESTS``): the lookup after the
    lock wait takes a fresh snapshot. Under REPEATABLE READ the snapshot would
    predate the winner's commit. SQLite has no equivalent here: two deferred
    transactions can both miss the lookup, so only Postgres deployments get
    the concurrency guarantee (a database constraint is OP #3449).

    A waiter gives up after ``LOCK_WAIT`` (``lock_timeout``) instead of
    holding a worker and a connection behind a slow first submit; the
    caller answers that as ``checkout_in_progress`` (503).
    """
    if connection.vendor == "postgresql":
        # Own savepoint, so a lock timeout rolls back to here and the caller's
        # transaction stays usable for its answer (Django's catch-outside-
        # atomic rule). On success the lock belongs to the outer transaction.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_WAIT}'")
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           [f"golden.guest_checkout:{ref}"])


def order_for_ref(company, ref: str):
    """The latest guest order made with this key inside the window.

    No row lock: the advisory lock already serialises same-key submits.
    """
    from .models import Order

    return (
        Order.objects
        .filter(company=company, external_source="storefront", external_id__startswith=ref,
                created_at__gte=timezone.now() - IDEMPOTENCY_WINDOW)
        .order_by("-created_at")
        .first()
    )


def key_reused() -> dict:
    return {
        "code": IDEMPOTENCY_KEY_REUSED,
        "detail": "This Idempotency-Key already placed an order with different details.",
    }
