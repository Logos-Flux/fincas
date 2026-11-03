"""Track C: documents/PDF — PO/quote/invoice edge renders, logo fallback, golden file.

Covers docs/testing-plan.md §3 "Documents/PDF": WeasyPrint renders for
PO, quote, invoice without exception on edge content (long names,
unicode, 200-line orders); missing-logo fallback (no 500); one
golden-file test on a canonical invoice.
"""

import hashlib
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings as dj_settings
from django.test import TestCase

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer, CustomerAddress
from documents.services.pdf import (
    render_invoice_document_html,
    render_invoice_html,
    render_invoice_pdf,
    render_po_html,
    render_po_pdf,
    render_quote_html,
    render_quote_pdf,
)
from golden_erp.paths import STATIC_DIR
from orders.models import Order, OrderLineItem, Quote, QuoteLineItem, SalesInvoice
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, PurchaseOrder, Supplier


def _company_customer(prefix="Doc"):
    company = Company.objects.create(name=f"{prefix} Co")
    make_posting_rules(company)
    customer = Customer.objects.create(
        first_name="Doc", last_name="Reader",
        email=f"{prefix.lower()}@example.com", company=company,
    )
    return company, customer


def _invoice_with_lines(company, customer, n_lines=1, name="Standard Widget",
                        po_number="PO-1"):
    order = Order.objects.create(
        order_number=f"SO-{po_number}",
        customer=customer,
        company=company,
        subtotal_ex_tax=Decimal("100.00"),
        shipping_cost_ex_tax=Decimal("0"),
        grand_total=Decimal("100.00"),
    )
    for i in range(n_lines):
        OrderLineItem.objects.create(
            order=order,
            sku=f"SKU-{i:04d}",
            item_name=f"{name} #{i}" if n_lines > 1 else name,
            quantity=1,
            unit_price_ex_tax=Decimal("100.00"),
            line_total_ex_tax=Decimal("100.00"),
        )
    return SalesInvoice.objects.create(
        invoice_number=f"INV-{po_number}",
        order=order,
        company=company,
        invoice_date=date(2025, 8, 6),
        subtotal_ex_tax=Decimal("100.00"),
        grand_total=Decimal("100.00"),
        amount_paid=Decimal("0.00"),
        balance_due=Decimal("100.00"),
        status=SalesInvoice.Status.SENT,
    )


def _po_with_supplier(company, **po_kw):
    supplier = Supplier.objects.create(
        company_name="Acme Supply",
        supplier_code=f"ACME-{po_kw.get('tag', 'X')}",
        supplier_type=Supplier.SupplierType.MANUFACTURER
        if hasattr(Supplier, "SupplierType") else "manufacturer",
        company=company,
    )
    product = Product.objects.create(
        title="Bracket", part_number=f"BR-{po_kw.get('tag', 'X')}",
        slug=f"br-{po_kw.get('tag', 'x').lower()}", company=company,
    )
    variant = ProductVariant.objects.create(product=product, sku=f"BR-{po_kw.get('tag', 'X')}-A")
    po = PurchaseOrder.objects.create(
        supplier=supplier,
        company=company,
        subtotal=Decimal("50.00"),
        grand_total=Decimal("50.00"),
        status=PurchaseOrder.Status.APPROVED,
    )
    POLineItem.objects.create(
        purchase_order=po,
        variant=variant,
        item_name="Steel bracket",
        supplier_sku="ACME-BR1",
        quantity_ordered=Decimal("10"),
        unit_cost=Decimal("5.0000"),
        line_total=Decimal("50.00"),
    )
    return po


class TestPORendering(TestCase):
    def test_po_html_contains_key_data(self):
        company, _ = _company_customer("PO")
        po = _po_with_supplier(company, tag="PO1")
        html = render_po_html(po)
        self.assertIn("PURCHASE ORDER", html)
        self.assertIn(po.po_number, html)
        self.assertIn("Acme Supply", html)
        self.assertIn("ACME-BR1", html)

    def test_po_pdf_renders_to_bytes(self):
        company, _ = _company_customer("PO2")
        po = _po_with_supplier(company, tag="PO2")
        pdf = render_po_pdf(po)
        self.assertIsInstance(pdf, bytes)
        self.assertGreater(len(pdf), 1000)
        self.assertTrue(pdf.startswith(b"%PDF"))


class TestEdgeContent(TestCase):
    def test_long_names_unicode_and_200_lines(self):
        company, customer = _company_customer("Edge")
        # As long as the column takes (Postgres enforces varchar length; SQLite
        # does not), leaving room for the " #199" line suffix.
        room = OrderLineItem._meta.get_field("item_name").max_length - len(" #199")
        long_name = ("Über-nut M12×1.75 — “tamarack” bracket w/ 日本語テスト " + ("x" * 300))[:room]
        invoice = _invoice_with_lines(company, customer, n_lines=200, name=long_name)
        html = render_invoice_html(invoice)
        self.assertIn("SKU-0199", html)
        pdf = render_invoice_pdf(invoice)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_quote_edge_content(self):
        company, customer = _company_customer("QEdge")
        address = CustomerAddress.objects.create(
            customer=customer, first_name="Doc", last_name="Reader",
            address_line_1="1 Main St — ünïcödé",
            city="Acton", state_province="MA", postal_code="01720",
        )
        quote = Quote.objects.create(
            customer=customer, company=company,
            billing_address=address, shipping_address=address,
            quote_expiration_date=date.today() + timedelta(days=7),
            subtotal_ex_tax=Decimal("10.00"),
            grand_total=Decimal("10.00"),
            status=Quote.Status.SENT,
        )
        QuoteLineItem.objects.create(
            quote=quote, variant=None, sku="UNI-1",
            # Full column width: Postgres enforces varchar(255), SQLite does not.
            item_name=("Pièce déjà-vu — 日本語 " + ("y" * 300))[
                :QuoteLineItem._meta.get_field("item_name").max_length
            ],
            quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("10.0000"),
            line_total_ex_tax=Decimal("10.00"),
        )
        html = render_quote_html(quote)
        self.assertIn("UNI-1", html)
        pdf = render_quote_pdf(quote)
        self.assertTrue(pdf.startswith(b"%PDF"))


class TestMissingLogo(TestCase):
    def test_missing_logo_falls_back_without_500(self):
        company, customer = _company_customer("Logo")
        invoice = _invoice_with_lines(company, customer)
        with mock.patch(
            "documents.services.document_context.BRAND",
            {
                "company_name": "No Logo Inc",
                "logo_path": "images/does-not-exist.webp",
                "colors": {
                    "forest": "#042e0d", "green": "#278040", "blue": "#4d65b2",
                    "bronze": "#c3a859", "bronze_dark": "#7a6223",
                },
                "fonts": {"heading": "sans-serif", "body": "sans-serif", "mono": "monospace"},
                "address_line1": "", "address_line2": "",
                "email": "", "phone": "", "website": "",
            },
        ):
            html = render_invoice_html(invoice)
            self.assertNotIn("<img", html)
            pdf = render_invoice_pdf(invoice)
            self.assertTrue(pdf.startswith(b"%PDF"))


class TestGoldenInvoice(TestCase):
    """Byte-stable golden check on the canonical invoice HTML.

    HTML (not PDF bytes) is the stable artifact: WeasyPrint embeds
    creation timestamps, so PDF bytes drift run-to-run. The HTML hash
    pins layout/content; the PDF assertion pins renderability.
    """

    def test_canonical_invoice_html_is_stable(self):
        company, customer = _company_customer("Golden")
        invoice = _invoice_with_lines(company, customer, po_number="GOLD")
        # The PDF's HTML (file:// fonts): the browser preview differs only
        # by its font URLs (statics, since 2026-10-17).
        html = render_invoice_document_html(invoice)
        # Font CSS embeds absolute file:// URLs derived from the checkout
        # path, so normalize them: the hash must pin layout/content, not
        # where the repo lives (worktrees/CI checkouts differ) or where the
        # packaged static dir sits (golden_erp/static since 2026-10-08).
        html = html.replace(str(STATIC_DIR), "<STATIC>")
        html = html.replace(str(dj_settings.BASE_DIR), "<ROOT>")
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
        # Pinned on first green run; any layout/content change must
        # re-baseline explicitly.
        self.assertEqual(
            digest,
            "720d2e543830b59a9adcf91542c17674879eae6d49136d184195ffae1c3104bc",  # re-pinned 2026-11-03: "Sales Order #" label
            "golden HTML changed — inspect the diff, then re-pin",
        )
