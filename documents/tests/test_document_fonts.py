"""Document rendering must never touch the network.

base_document.html used to `@import` fonts.googleapis.com, so WeasyPrint
issued a blocking HTTPS request on EVERY render. Measured on a 40-line quote:

    Google Fonts @import   0.785s
    vendored TTF           0.240s     <- 3.3x
    no fonts at all        0.217s     (baseline; not an option, changes design)

Beyond the speed, it put a third party in the invoice-send path: a Google
outage became a render failure (the render call sits OUTSIDE the try/except in
both PDF senders), and font substitution silently changed line wrapping and
therefore pagination.

WOFF2 is deliberately NOT used: WeasyPrint decompresses every font on every
render, which cost 0.405s vs TTF's 0.240s. Variable fonts are worse still --
Google's css2 endpoint serves Source Sans 3 and Roboto Mono as variable, and
WeasyPrint re-instances them per render. The vendored files are static
instances produced with fontTools.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.test import TestCase

from core.models import Company
from customers.models import Customer
from documents.services.document_context import _FONT_FACES, get_font_face_css
from documents.services.pdf import render_quote_html, render_quote_pdf
from orders.models import Quote, QuoteLineItem

#: Anything that would make a render reach off-box.
REMOTE = re.compile(r"""(?:src|url)\s*\(\s*['"]?(https?:)?//""", re.I)


class VendoredFontTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="fonts@example.com", company=self.company,
        )
        self.quote = Quote.objects.create(customer=customer, company=self.company)
        QuoteLineItem.objects.create(
            quote=self.quote, variant=None, sku="MISC", item_name="Thing",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("10.0000"),
            line_total_ex_tax=Decimal("10.00"),
        )

    def test_every_declared_font_file_is_present(self):
        css = get_font_face_css()
        self.assertEqual(
            css.count("@font-face"), len(_FONT_FACES),
            "a vendored font file is missing — get_font_face_css() skips "
            "missing files and logs, so the document silently falls back",
        )

    def test_rendered_document_fetches_nothing_remote(self):
        html = render_quote_html(self.quote)
        remote = REMOTE.findall(html)
        self.assertEqual(
            remote, [],
            f"rendered document references a remote resource: {remote}. "
            "WeasyPrint fetches these synchronously during render.",
        )

    def test_no_active_import_rule(self):
        # The word "@import" survives in the explanatory CSS comment; what
        # must not survive is an actual `@import url(...)` rule.
        html = render_quote_html(self.quote)
        self.assertIsNone(
            re.search(r"@import\s+url", html),
            "an active @import rule would make WeasyPrint fetch during render",
        )
        self.assertNotIn("fonts.gstatic.com", html)

    def test_fonts_are_referenced_by_local_file_uri(self):
        css = get_font_face_css()
        self.assertIn("file://", css)
        self.assertEqual(css.count("format('truetype')"), len(_FONT_FACES))

    def test_no_template_comment_leaks_into_the_stylesheet(self):
        # Django's {# #} is single-line only; a multi-line one is emitted
        # verbatim, and inside <style> that is invalid CSS.
        html = render_quote_html(self.quote)
        self.assertNotIn("{#", html)

    def test_pdf_still_renders(self):
        self.assertTrue(render_quote_pdf(self.quote).startswith(b"%PDF"))
