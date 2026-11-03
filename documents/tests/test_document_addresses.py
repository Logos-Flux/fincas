"""A document with no address must not print orphaned punctuation.

`{{ city }}, {{ state }} {{ zip }}` rendered a line containing a single
comma when all three were empty, preceded by a stray <br> from an empty
street_1. That has been shipping on live invoices. Address lines are now
assembled in _postal_lines(), which drops the empty ones -- punctuation
belongs where the emptiness is known, not in the template.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from core.models import Company
from customers.models import Customer, CustomerAddress
from documents.services.document_context import _postal_lines
from documents.services.pdf import render_quote_html
from orders.models import Quote, QuoteLineItem


class PostalLinesTests(TestCase):
    def test_drops_every_empty_line(self):
        self.assertEqual(_postal_lines("", "", "", "", "", ""), [])

    def test_locality_needs_no_comma_when_only_one_part(self):
        self.assertEqual(_postal_lines("", "", "", "Acton", "", ""), ["Acton"])
        self.assertEqual(_postal_lines("", "", "", "", "MA", ""), ["MA"])

    def test_zip_alone_is_a_line(self):
        self.assertEqual(_postal_lines("", "", "", "", "", "01720"), ["01720"])

    def test_full_address_in_order(self):
        self.assertEqual(
            _postal_lines("Acme LLC", "1 Main St", "Suite 2", "Acton", "MA", "01720"),
            ["Acme LLC", "1 Main St", "Suite 2", "Acton, MA 01720"],
        )

    def test_city_and_state_without_zip(self):
        self.assertEqual(
            _postal_lines("", "1 Main St", "", "Acton", "MA", ""),
            ["1 Main St", "Acton, MA"],
        )


class EmptyAddressRenderingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="addr@example.com", company=self.company,
        )

    def _render(self, **quote_kwargs):
        quote = Quote.objects.create(
            customer=self.customer, company=self.company, **quote_kwargs
        )
        QuoteLineItem.objects.create(
            quote=quote, variant=None, sku="X", item_name="Thing",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("1.0000"),
        )
        return render_quote_html(quote)

    def test_no_orphaned_comma_when_there_is_no_address(self):
        html = self._render()
        self.assertNotRegex(html, r">\s*,\s*<", "address line rendered as a bare comma")
        self.assertNotRegex(html, r"<br>\s*,", "comma after a stray break")

    def test_a_real_address_still_renders_in_full(self):
        address = CustomerAddress.objects.create(
            customer=self.customer,
            company_name="Northfield Solar LLC",
            address_line_1="1420 Industrial Pkwy",
            address_line_2="Building C",
            city="Northfield", state_province="MA", postal_code="01360",
        )
        html = self._render(billing_address=address, shipping_address=address)
        for fragment in (
            "Northfield Solar LLC", "1420 Industrial Pkwy", "Building C",
            "Northfield, MA 01360",
        ):
            self.assertIn(fragment, html)

    def test_partial_address_omits_only_the_missing_pieces(self):
        address = CustomerAddress.objects.create(
            customer=self.customer,
            address_line_1="1420 Industrial Pkwy",
            city="Northfield", state_province="MA",
        )
        html = self._render(billing_address=address)
        self.assertIn("Northfield, MA", html)
        self.assertNotRegex(html, r">\s*,\s*<")
