"""Quote PDF rendering, mirroring test_pdf_service.py for invoices."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from core.models import Company
from customers.models import Customer, CustomerAddress
from documents.services.pdf import render_quote_html, render_quote_pdf
from orders.models import Quote, QuoteLineItem


class QuotePDFTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="James", last_name="Tapscott",
            email="jt@example.com", company=self.company,
        )
        self.address = CustomerAddress.objects.create(
            customer=self.customer,
            first_name="James", last_name="Tapscott",
            address_line_1="3 Post Office Sq",
            city="Acton", state_province="MA", postal_code="01720",
        )
        self.quote = Quote.objects.create(
            customer=self.customer,
            company=self.company,
            billing_address=self.address,
            shipping_address=self.address,
            quote_expiration_date=date.today() + timedelta(days=7),
            customer_po_number="PO-4471",
            payment_terms_proposed="Net 30",
            shipping_terms_proposed="FOB Destination",
            special_conditions="Crated delivery only.",
            subtotal_ex_tax=Decimal("1355.94"),
            shipping_cost_ex_tax=Decimal("222.63"),
            grand_total=Decimal("1578.57"),
            status=Quote.Status.SENT,
        )
        QuoteLineItem.objects.create(
            quote=self.quote,
            variant=None,
            sku="UNIPGRM-5P2",
            item_name="Universal Programmer, 5-pack",
            quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("1355.9400"),
            line_total_ex_tax=Decimal("1355.94"),
        )

    def test_html_contains_the_key_data(self):
        html = render_quote_html(self.quote)
        self.assertIn("QUOTE", html)
        self.assertIn(self.quote.quote_number, html)
        self.assertIn("Tapscott", html)
        self.assertIn("UNIPGRM-5P2", html)
        # intcomma formatting, same as the invoice template.
        self.assertIn("1,355.94", html)
        self.assertIn("1,578.57", html)
        self.assertIn("PO-4471", html)
        self.assertIn("Net 30", html)
        self.assertIn("FOB Destination", html)
        self.assertIn("Crated delivery only.", html)

    def test_pdf_renders_to_bytes(self):
        pdf_bytes = render_quote_pdf(self.quote)
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertGreater(len(pdf_bytes), 1000)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_a_freeform_line_renders(self):
        # A variant-less line must print. The document only ever shows the
        # snapshotted sku/name, so there is nothing to dereference.
        QuoteLineItem.objects.create(
            quote=self.quote, variant=None, sku="MISC",
            item_name="Custom fabrication, on-site",
            quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("450.0000"),
            line_total_ex_tax=Decimal("450.00"),
        )
        html = render_quote_html(self.quote)
        self.assertIn("Custom fabrication, on-site", html)
        self.assertIn("MISC", html)

    def test_expired_quote_is_banner_flagged(self):
        self.quote.quote_expiration_date = date.today() - timedelta(days=1)
        self.quote.save(update_fields=["quote_expiration_date"])
        html = render_quote_html(self.quote)
        self.assertIn("THIS QUOTE HAS EXPIRED", html)
        # Assert the CLASS, not just the words. base_document.html styles
        # `.doc-status-banner`; a bare `status-banner` matches nothing and
        # renders an unstyled line of text that still passes a text check.
        self.assertIn('class="doc-status-banner status-expired"', html)

    def test_every_banner_uses_the_base_class(self):
        cases = {
            Quote.Status.ACCEPTED: "status-accepted",
            Quote.Status.LOST: "status-lost",
            Quote.Status.DRAFT: "status-draft",
            Quote.Status.CANCELLED: "status-cancelled",
        }
        for status, modifier in cases.items():
            with self.subTest(status=status):
                self.quote.status = status
                self.quote.save(update_fields=["status"])
                html = render_quote_html(self.quote)
                self.assertIn(f'class="doc-status-banner {modifier}"', html)

    def test_custom_banner_styles_are_scoped_to_the_base_class(self):
        # A bare `.status-accepted { }` rule would win on colour but lose the
        # padding/centring/weight that only .doc-status-banner provides.
        html = render_quote_html(self.quote)
        for modifier in ("status-expired", "status-accepted", "status-lost"):
            self.assertIn(f".doc-status-banner.{modifier}", html)

    def test_live_quote_shows_the_validity_note_not_the_expiry_banner(self):
        html = render_quote_html(self.quote)
        self.assertNotIn("THIS QUOTE HAS EXPIRED", html)
        self.assertIn("Pricing held until", html)

    def test_draft_quote_is_marked_as_not_issued(self):
        self.quote.status = Quote.Status.DRAFT
        self.quote.save(update_fields=["status"])
        self.assertIn("DRAFT — NOT YET ISSUED", render_quote_html(self.quote))

    def test_renders_without_addresses(self):
        # The customer-only fallback branch, as the invoice fixture exercises.
        bare = Quote.objects.create(
            customer=self.customer, company=self.company,
        )
        QuoteLineItem.objects.create(
            quote=bare, variant=None, sku="X", item_name="Thing",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("1.0000"),
        )
        html = render_quote_html(bare)
        self.assertIn("James Tapscott", html)
        self.assertTrue(render_quote_pdf(bare).startswith(b"%PDF"))

    def test_revision_number_appears_only_when_revised(self):
        self.assertNotIn("rev ", render_quote_html(self.quote))
        self.quote.revision_number = 3
        self.quote.save(update_fields=["revision_number"])
        self.assertIn("rev 3", render_quote_html(self.quote))
