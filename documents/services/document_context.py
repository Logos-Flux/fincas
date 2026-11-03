"""Build template context dicts from model instances."""

import base64
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from documents.brand import BRAND
from golden_erp.paths import STATIC_DIR

logger = logging.getLogger(__name__)


def _get_logo_data_uri():
    """Read the logo file and return a data URI for embedding in PDFs."""
    logo_path = Path(settings.STATIC_ROOT or "") / BRAND["logo_path"]
    if not logo_path.exists():
        # Fallback to staticfiles dirs
        for static_dir in settings.STATICFILES_DIRS:
            candidate = Path(static_dir) / BRAND["logo_path"]
            if candidate.exists():
                logo_path = candidate
                break
    if not logo_path.exists():
        # Fall back to golden's packaged static dir
        logo_path = STATIC_DIR / BRAND["logo_path"]
    if logo_path.exists():
        data = logo_path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:image/webp;base64,{b64}"
    return None


def _postal_lines(company, street_1, street_2, city, state, postal_code):
    """Assemble the printable address lines, omitting every empty one.

    The templates used to punctuate inline -- `{{ city }}, {{ state }}
    {{ zip }}` -- which on a document with no address rendered a line
    containing a single orphaned comma, preceded by a stray <br> from an
    empty street_1. That shipped on live invoices. Punctuation belongs
    where the emptiness is known.
    """
    locality = ", ".join(part for part in (city, state) if part)
    if postal_code:
        locality = f"{locality} {postal_code}".strip()
    return [line for line in (company, street_1, street_2, locality) if line]


#: Document fonts, vendored so a render never touches the network.
#: Barlow ships one file per weight; Source Sans 3 and Roboto Mono are
#: variable fonts where one file covers the whole range.
#: All STATIC instances. Google's css2 endpoint serves Source Sans 3 and
#: Roboto Mono as variable fonts, and WeasyPrint re-instances a variable font
#: on EVERY render -- so vendoring the variable files moved the cost from the
#: network to the CPU instead of removing it. These are pre-instanced with
#: fontTools; regenerate with docs/plans/.../fonts note if a weight is added.
_FONT_FACES = (
    ("Barlow", 400, "barlow-400.ttf"),
    ("Barlow", 600, "barlow-600.ttf"),
    ("Barlow", 700, "barlow-700.ttf"),
    ("Barlow", 800, "barlow-800.ttf"),
    ("Source Sans 3", 400, "source-sans-3-400.ttf"),
    ("Source Sans 3", 600, "source-sans-3-600.ttf"),
    ("Roboto Mono", 400, "roboto-mono-400.ttf"),
    ("Roboto Mono", 500, "roboto-mono-500.ttf"),
)


def _static_dir(relative):
    """Resolve a path under golden's static dir, mirroring _get_logo_data_uri's probe order."""
    candidates = [Path(settings.STATIC_ROOT or "") / relative]
    candidates += [Path(d) / relative for d in settings.STATICFILES_DIRS]
    candidates.append(STATIC_DIR / relative)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@lru_cache(maxsize=2)
def get_font_face_css(web=False):
    """@font-face rules pointing at vendored WOFF2 files on local disk.

    ``web=True`` points them at the served static URLs instead: the same
    template is the browser preview (``/erp/payments/<pk>/preview/`` and
    the quote and PO previews), and a browser refuses ``file://`` (R1.14:
    console errors on every visit, fallback fonts).

    The templates used to `@import` Google Fonts, which made WeasyPrint issue
    a blocking HTTPS request on EVERY render -- measured 0.605s vs 0.037s on a
    40-line document, ~16x, with no caching between renders. That put a third
    party in the invoice-send path: a Google outage became a render failure,
    and font substitution silently changed line wrapping and therefore
    pagination.

    Returns "" if the files are missing, so a render degrades to the CSS
    fallback stack rather than raising.
    """
    from django.templatetags.static import static

    rules = []
    for family, weight, filename in _FONT_FACES:
        relative = f"assets/fonts/docs/{filename}"
        path = _static_dir(relative)
        if path is None:
            logger.warning("document font missing: %s", filename)
            continue
        url = static(relative) if web else path.as_uri()
        rules.append(
            "@font-face{"
            f"font-family:'{family}';"
            "font-style:normal;"
            f"font-weight:{weight};"
            "font-display:block;"
            f"src:url('{url}') format('truetype');"
            "}"
        )
    return "".join(rules)


def build_bill_to(address, customer):
    """Bill-to block, degrading to a customer-only dict with no address."""
    if address:
        return {
            "name": f"{address.first_name} {address.last_name}".strip()
            or f"{customer.first_name} {customer.last_name}",
            "company": address.company_name or "",
            "street_1": address.address_line_1 or "",
            "street_2": address.address_line_2 or "",
            "city": address.city or "",
            "state": address.state_province or "",
            "zip": address.postal_code or "",
            "email": customer.email,
            "phone": address.phone or customer.phone or "",
            "lines": _postal_lines(
                address.company_name or "",
                address.address_line_1 or "",
                address.address_line_2 or "",
                address.city or "",
                address.state_province or "",
                address.postal_code or "",
            ),
        }
    if customer:
        return {
            "name": f"{customer.first_name} {customer.last_name}",
            "company": customer.company_name or "",
            "street_1": "", "street_2": "", "city": "", "state": "", "zip": "",
            "email": customer.email,
            "phone": customer.phone or "",
            "lines": _postal_lines(customer.company_name or "", "", "", "", "", ""),
        }
    return None


def build_ship_to(address, customer):
    """Ship-to block. None when the document has no shipping address."""
    if not address:
        return None
    return {
        "name": f"{address.first_name} {address.last_name}".strip()
        or f"{customer.first_name} {customer.last_name}",
        "company": address.company_name or "",
        "street_1": address.address_line_1 or "",
        "street_2": address.address_line_2 or "",
        "city": address.city or "",
        "state": address.state_province or "",
        "zip": address.postal_code or "",
        "lines": _postal_lines(
            address.company_name or "",
            address.address_line_1 or "",
            address.address_line_2 or "",
            address.city or "",
            address.state_province or "",
            address.postal_code or "",
        ),
    }


def build_line_items(related_manager):
    """Flatten a document's line items into the shape the template loops.

    QuoteLineItem and OrderLineItem expose the same names for every field
    used here, so one function serves both. `variant` is deliberately not
    read: a freeform line has none, and the printed document only ever
    shows the snapshotted sku/name anyway.
    """
    return [
        {
            "sku": li.sku or "",
            "description": li.item_name or li.description or "",
            "quantity": li.quantity,
            "unit_price": li.unit_price_ex_tax,
            "line_total": li.line_total_ex_tax,
        }
        for li in related_manager.order_by("sort_order", "pk")
    ]


def build_invoice_context(invoice):
    """Build the full template context for a SalesInvoice."""
    order = invoice.order
    customer = order.customer
    billing = order.billing_address
    shipping = order.shipping_address

    bill_to = build_bill_to(billing, customer)
    ship_to = build_ship_to(shipping, customer)
    line_items = build_line_items(order.line_items)

    # Payment method from the most recent payment
    payments = invoice.payments.filter(status__in=["captured", "settled"]).order_by("-payment_date")
    payment_method = ""
    if payments.exists():
        p = payments.first()
        payment_method = p.get_payment_method_display()

    # Shipping method from first sub-order
    sub = order.sub_orders.first()
    shipping_method = sub.shipping_method if sub else ""

    # STOREFRONT_URL fails closed (see golden_erp/settings/base.py). Without it a
    # pay link would be a relative URL printed on a customer's invoice, which
    # is worse than no link at all -- the template hides the box when empty.
    pay_url = (
        f"{settings.STOREFRONT_URL}/pay/{invoice.payment_token}/"
        if settings.STOREFRONT_URL
        else ""
    )

    return {
        "brand": BRAND,
        "logo_data_uri": _get_logo_data_uri(),
        "font_face_css": get_font_face_css(),
        "invoice": invoice,
        "order": order,
        "customer": customer,
        "bill_to": bill_to,
        "ship_to": ship_to,
        "line_items": line_items,
        "totals": {
            "subtotal": invoice.subtotal_ex_tax,
            # Pulled from the underlying Order — SalesInvoice doesn't carry
            # its own discount field; the invoice's grand_total already
            # reflects the discount baked in upstream.
            "discount": (order.discount_amount or 0) + (order.coupon_discount or 0),
            "shipping": invoice.shipping_cost,
            "tax": invoice.tax_total,
            "grand_total": invoice.grand_total,
            "amount_paid": invoice.amount_paid,
            "balance_due": invoice.balance_due,
        },
        "payment_method": payment_method,
        "shipping_method": shipping_method,
        "pay_url": pay_url,
    }


def build_quote_context(quote):
    """Build the full template context for a Quote.

    Mirrors build_invoice_context. The differences are all absences: a
    quote has no payments, no sub-orders and no pay link, so there is no
    payment_method / amount_paid / balance_due. What it adds is validity —
    an expiry date is the whole point of a quote.
    """
    customer = quote.customer

    subtotal = quote.subtotal_ex_tax or 0
    discount = (quote.discount_amount or 0) + (quote.coupon_discount or 0)

    return {
        "brand": BRAND,
        "logo_data_uri": _get_logo_data_uri(),
        "font_face_css": get_font_face_css(),
        "quote": quote,
        "customer": customer,
        "bill_to": build_bill_to(quote.billing_address, customer),
        "ship_to": build_ship_to(quote.shipping_address, customer),
        "line_items": build_line_items(quote.line_items),
        "totals": {
            "subtotal": subtotal,
            "discount": discount,
            "shipping": quote.shipping_cost_ex_tax or 0,
            "tax": quote.tax_total or 0,
            "grand_total": quote.grand_total or 0,
        },
        # Quotes propose terms rather than carrying settled ones.
        "payment_terms": quote.payment_terms_proposed or "",
        "shipping_terms": quote.shipping_terms_proposed or "",
        "is_expired": bool(
            quote.quote_expiration_date
            and quote.quote_expiration_date < date.today()
        ),
    }


def build_po_context(po):
    """Build the full template context for a PurchaseOrder.

    The supplier is the "bill-to"-shaped block (vendor name + address);
    the ship-to is the PO's own ship_to_* snapshot text. Line items use
    PO field names (quantity_ordered / unit_cost / line_total).
    """
    supplier = po.supplier
    if supplier is not None:
        vendor_block = {
            "name": supplier.display_name or supplier.company_name,
            "lines": _postal_lines(
                "",
                supplier.address_line_1 or "",
                supplier.address_line_2 or "",
                supplier.city or "",
                supplier.state_province or "",
                supplier.postal_code or "",
            ),
            "email": supplier.po_email or supplier.primary_contact_email or "",
            "phone": supplier.primary_contact_phone or "",
        }
    else:
        vendor_block = {
            "name": po.supplier_contact_name or "",
            "lines": [],
            "email": po.supplier_contact_email or "",
            "phone": "",
        }

    ship_lines = [line for line in (po.ship_to_label, po.ship_to_attention, po.ship_to_address) if line]
    ship_block = {"name": "", "lines": ship_lines} if ship_lines else None

    line_items = [
        {
            "sku": li.supplier_sku or li.our_sku or "",
            "description": li.item_name or li.description or "",
            "quantity": li.quantity_ordered,
            "unit_price": li.unit_cost,
            "line_total": li.line_total,
        }
        for li in po.line_items.order_by("sort_order", "pk")
    ]

    return {
        "brand": BRAND,
        "logo_data_uri": _get_logo_data_uri(),
        "font_face_css": get_font_face_css(),
        "po": po,
        "supplier": supplier,
        "bill_to": vendor_block,
        "ship_to": ship_block,
        "line_items": line_items,
        "totals": {
            "subtotal": po.subtotal or 0,
            "discount": po.discount_amount or 0,
            "shipping": po.shipping_cost or 0,
            "tax": po.tax_total or 0,
            "grand_total": po.grand_total or 0,
        },
        "payment_terms": po.vendor_payment_terms or "",
        "shipping_terms": po.incoterms or "",
    }
