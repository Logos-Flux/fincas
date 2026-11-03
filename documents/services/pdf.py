"""PDF rendering via WeasyPrint."""

import logging

from django.template.loader import render_to_string
from weasyprint import HTML

logger = logging.getLogger(__name__)


def render_invoice_pdf(invoice):
    """Render a SalesInvoice to PDF bytes.

    Args:
        invoice: SalesInvoice instance (with order, customer prefetched)

    Returns:
        bytes — the PDF file content
    """

    html_string = render_invoice_document_html(invoice)
    pdf_bytes = HTML(string=html_string).write_pdf()
    logger.info("Generated PDF for %s (%d bytes)", invoice.invoice_number, len(pdf_bytes))
    return pdf_bytes


def render_invoice_document_html(invoice):
    """The HTML the PDF is rendered from (fonts by local file, for WeasyPrint)."""
    from .document_context import build_invoice_context

    return render_to_string("documents/sales_invoice.html", build_invoice_context(invoice))


def render_invoice_html(invoice):
    """Render a SalesInvoice to HTML string for the browser preview.

    The same template as the PDF, with the fonts served as statics: a
    browser refuses ``file://`` (R1.14).
    """
    from .document_context import build_invoice_context

    return render_to_string("documents/sales_invoice.html", _for_web(build_invoice_context(invoice)))


def _for_web(context):
    """The browser preview's context: fonts by static URL, not ``file://``."""
    from .document_context import get_font_face_css

    context["font_face_css"] = get_font_face_css(web=True)
    return context


def render_quote_pdf(quote):
    from .document_context import build_quote_context

    context = build_quote_context(quote)
    html_string = render_to_string("documents/sales_quote.html", context)
    pdf_bytes = HTML(string=html_string).write_pdf()
    logger.info("Generated PDF for %s (%d bytes)", quote.quote_number, len(pdf_bytes))
    return pdf_bytes


def render_quote_html(quote):
    from .document_context import build_quote_context

    return render_to_string("documents/sales_quote.html", _for_web(build_quote_context(quote)))


def render_po_pdf(po):
    from .document_context import build_po_context

    context = build_po_context(po)
    html_string = render_to_string("documents/purchase_order.html", context)
    pdf_bytes = HTML(string=html_string).write_pdf()
    logger.info("Generated PDF for %s (%d bytes)", po.po_number, len(pdf_bytes))
    return pdf_bytes


def render_po_html(po):
    from .document_context import build_po_context

    return render_to_string("documents/purchase_order.html", _for_web(build_po_context(po)))
