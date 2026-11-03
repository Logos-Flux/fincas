import logging
import uuid
from datetime import date

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from documents.brand import BRAND

from .email_link import golden_headers, log_document_email
from .models import EmailLog

logger = logging.getLogger(__name__)

TEMPLATE_NAME = "order_confirmation"
SUBJECT_TEMPLATE = f"Order Confirmation — {BRAND.get('company_name', 'Commerce')} #{{order_number}}"

# Documents created before this date are historical imports — never auto-send.
# Manual resend still allowed but the view should warn the user first.
EMAIL_CUTOFF_DATE = date(2026, 3, 1)


def is_historical_document(doc):
    """Return True if the document was created before the email cutoff date.

    Used to prevent auto-sending confirmations for imported historical data.
    """
    doc_date = getattr(doc, "order_date", None) or getattr(doc, "created_at", None)
    if doc_date is None:
        return False
    if hasattr(doc_date, "date"):
        doc_date = doc_date.date()
    return doc_date < EMAIL_CUTOFF_DATE


def send_order_confirmation(order, recipient_email, triggered_by, user):
    """
    Render and send an order confirmation email. Log result to EmailLog.

    Auto-sends are silently blocked for orders created before EMAIL_CUTOFF_DATE.
    Manual sends are allowed regardless (the view handles the user warning).

    Args:
        order: Order instance (with customer, line_items prefetched)
        recipient_email: str — email address to send to
        triggered_by: "auto" | "manual"
        user: User instance — the staff member who triggered it

    Returns:
        EmailLog instance (check .status for "sent" or "failed"), or None
        if auto-send was blocked for a historical document.
    """
    # Block auto-send for historical documents
    if triggered_by == "auto" and is_historical_document(order):
        logger.info(
            "Skipping auto-send for historical order %s (created %s, cutoff %s)",
            order.order_number, order.order_date, EMAIL_CUTOFF_DATE,
        )
        return None
    subject = SUBJECT_TEMPLATE.format(order_number=order.order_number)

    # Build template context
    line_items = order.line_items.select_related("variant").order_by("sort_order")
    context = {
        "brand": BRAND,
        "order": order,
        "line_items": line_items,
        "customer": order.customer,
        "shipping_address": order.shipping_address,
        "billing_address": order.billing_address,
    }

    # Render both HTML and plain-text versions
    html_body = render_to_string(
        "communications/emails/order_confirmation.html", context
    )
    text_body = render_to_string(
        "communications/emails/order_confirmation.txt", context
    )

    # Send
    anymail_message_id = ""
    status = EmailLog.Status.SENT
    error_message = ""

    # Internal sales/ops recipients, BCC'd on every confirmation. The setting
    # may be a comma-separated string (env/secret) or a list — normalize both.
    bcc = []
    sales_setting = getattr(settings, "SALES_NOTIFICATION_EMAIL", "")
    if isinstance(sales_setting, str):
        bcc.extend(e.strip() for e in sales_setting.split(",") if e.strip())
    else:
        bcc.extend(e for e in sales_setting if e)

    # Known before sending: the header lets the Gmail adapter skip this email when it comes
    # back through a staff mailbox (a Bcc to sales); it is logged here already.
    log_id = uuid.uuid4()
    try:
        msg = EmailMultiAlternatives(
            headers=golden_headers(log_id),
            subject=subject,
            body=text_body,
            to=[recipient_email],
            bcc=bcc,
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()

        # Capture Anymail message ID (only present when using Anymail backend)
        if hasattr(msg, "anymail_status") and msg.anymail_status:
            anymail_message_id = msg.anymail_status.message_id or ""

        # Mark confirmation as sent (duplicate prevention)
        order.confirmation_sent_at = timezone.now()
        order.save(update_fields=["confirmation_sent_at"])
    except Exception as exc:
        logger.exception("Failed to send order confirmation for %s", order.order_number)
        status = EmailLog.Status.FAILED
        error_message = str(exc)

    # Log
    log = log_document_email(
        order,
        id=log_id,
        document_type=EmailLog.DocumentType.SALES_ORDER,
        document_id=order.pk,
        document_number=order.order_number,
        recipient_email=recipient_email,
        subject=subject,
        template_name=TEMPLATE_NAME,
        status=status,
        error_message=error_message,
        triggered_by=triggered_by,
        sent_by=user,
        anymail_message_id=anymail_message_id,
    )
    return log


# -- Invoice emails --

CARRIER_TRACKING_URLS = {
    "ups": "https://www.ups.com/track?tracknum={tracking_number}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={tracking_number}",
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}",
}


INVOICE_SUBJECT_TEMPLATE = f"Invoice {{invoice_number}} from {BRAND.get('company_name', 'Commerce')}"


def send_invoice_email(invoice, recipient_email, triggered_by, user, message_body=None):
    """Send an invoice email with PDF attachment. Log result to EmailLog."""
    from documents.services.pdf import render_invoice_pdf
    from orders.models import SalesInvoice

    if triggered_by == "auto" and is_historical_document(invoice):
        logger.info("Skipping auto-send for historical invoice %s", invoice.invoice_number)
        return None

    # Sending a draft invoice posts it (draft -> sent below). The period is
    # checked here, before the email goes out and the EmailLog is written:
    # until 2026-10-17 a closed period surfaced only from the save at the
    # end, after the customer already had the email (R9.05, a 500).
    if invoice.status == SalesInvoice.Status.DRAFT:
        from accounting.posting import assert_period_open

        assert_period_open(
            invoice.company, invoice.invoice_date, what=f"Invoice {invoice.invoice_number}"
        )

    subject = INVOICE_SUBJECT_TEMPLATE.format(invoice_number=invoice.invoice_number)

    if not message_body:
        if invoice.status == "paid":
            message_body = f"Please find attached your paid invoice {invoice.invoice_number}. This serves as your receipt for tax purposes."
        else:
            message_body = f"Please find attached invoice {invoice.invoice_number} for ${invoice.balance_due:.2f}. Payment is due {invoice.get_payment_terms_display().lower()}."

    customer = invoice.order.customer
    context = {
        "brand": BRAND,
        "customer_name": f"{customer.first_name} {customer.last_name}",
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date.strftime("%B %d, %Y"),
        "grand_total": f"{invoice.grand_total:,.2f}",
        "status": invoice.status,
        "status_display": invoice.get_status_display(),
        "message_body": message_body,
    }

    html_body = render_to_string("communications/emails/invoice_email.html", context)

    pdf_bytes = render_invoice_pdf(invoice)

    anymail_message_id = ""
    status = EmailLog.Status.SENT
    error_message = ""

    # Known before sending: the header lets the Gmail adapter skip this email when it comes
    # back through a staff mailbox (a Bcc to sales); it is logged here already.
    log_id = uuid.uuid4()
    try:
        msg = EmailMultiAlternatives(
            headers=golden_headers(log_id),
            subject=subject,
            body=message_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.attach(f"{invoice.invoice_number}.pdf", pdf_bytes, "application/pdf")
        msg.send()
        if hasattr(msg, "anymail_status"):
            anymail_message_id = getattr(msg.anymail_status, "message_id", "") or ""
    except Exception as e:
        logger.exception("Failed to send invoice email for %s", invoice.invoice_number)
        status = EmailLog.Status.FAILED
        error_message = str(e)

    log = log_document_email(
        invoice,
        id=log_id,
        document_type=EmailLog.DocumentType.INVOICE,
        document_id=invoice.pk,
        document_number=invoice.invoice_number,
        recipient_email=recipient_email,
        subject=subject,
        template_name="invoice_email",
        status=status,
        error_message=error_message,
        triggered_by=triggered_by,
        sent_by=user,
        anymail_message_id=anymail_message_id,
    )

    if status == EmailLog.Status.SENT and not invoice.sent_at:
        invoice.sent_at = timezone.now()
        if invoice.status == SalesInvoice.Status.DRAFT:
            invoice.status = SalesInvoice.Status.SENT
        invoice.save(update_fields=["sent_at", "status"])

    return log


# ── Tracking URL builders ─────────────────────────────────


def build_tracking_url(carrier, tracking_number, fallback_url=""):
    """Build a tracking URL based on carrier name. Falls back to sub_order.tracking_url."""
    if not tracking_number:
        return fallback_url
    carrier_key = (carrier or "").strip().lower()
    template = CARRIER_TRACKING_URLS.get(carrier_key)
    if template:
        return template.format(tracking_number=tracking_number)
    return fallback_url or ""


# ── Shipping Notification ─────────────────────────────────

SHIPPING_TEMPLATE_NAME = "shipping_notification"
SHIPPING_SUBJECT_TEMPLATE = f"Your Order Has Shipped — {BRAND.get('company_name', 'Commerce')} #{{order_number}}"


def send_shipping_notification(sub_order, triggered_by="auto", user=None):
    """
    Send a shipping notification email for a SubOrder.

    Args:
        sub_order: SubOrder instance (with order, order.customer prefetched)
        triggered_by: "auto" | "manual"
        user: User instance — the staff member who triggered it (optional)

    Returns:
        EmailLog instance, or None if no customer email.
    """
    order = sub_order.order
    customer = order.customer

    if not customer or not customer.email:
        logger.warning(
            "Cannot send shipping notification for %s — no customer email",
            sub_order.sub_order_number,
        )
        return None

    recipient_email = customer.email
    subject = SHIPPING_SUBJECT_TEMPLATE.format(order_number=order.order_number)

    # Count shipments for "Shipment X of Y"
    all_sub_orders = list(
        order.sub_orders.order_by("sort_order", "created_at").values_list("pk", flat=True)
    )
    total_shipments = len(all_sub_orders)
    try:
        shipment_number = all_sub_orders.index(sub_order.pk) + 1
    except ValueError:
        shipment_number = 1

    # Line items in this sub_order
    line_items = sub_order.line_items.select_related("variant").order_by("sort_order")

    # Check if other sub_orders are still pending shipment
    has_pending_shipments = order.sub_orders.exclude(pk=sub_order.pk).exclude(
        status__in=["shipped", "in_transit", "delivered"]
    ).exists()

    # Build tracking URL
    tracking_url = build_tracking_url(
        sub_order.carrier, sub_order.tracking_number, sub_order.tracking_url
    )

    context = {
        "brand": BRAND,
        "order": order,
        "sub_order": sub_order,
        "customer": customer,
        "line_items": line_items,
        "shipping_address": order.shipping_address,
        "carrier": sub_order.carrier,
        "tracking_number": sub_order.tracking_number,
        "tracking_url": tracking_url,
        "shipment_number": shipment_number,
        "total_shipments": total_shipments,
        "has_pending_shipments": has_pending_shipments,
    }

    html_body = render_to_string(
        "communications/emails/shipping_notification.html", context
    )
    text_body = render_to_string(
        "communications/emails/shipping_notification.txt", context
    )

    anymail_message_id = ""
    status = EmailLog.Status.SENT
    error_message = ""

    # Known before sending: the header lets the Gmail adapter skip this email when it comes
    # back through a staff mailbox (a Bcc to sales); it is logged here already.
    log_id = uuid.uuid4()
    try:
        msg = EmailMultiAlternatives(
            headers=golden_headers(log_id),
            subject=subject,
            body=text_body,
            to=[recipient_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()

        if hasattr(msg, "anymail_status") and msg.anymail_status:
            anymail_message_id = msg.anymail_status.message_id or ""

        # Mark as sent (duplicate prevention)
        sub_order.shipping_notification_sent_at = timezone.now()
        sub_order.save(update_fields=["shipping_notification_sent_at"])
    except Exception as exc:
        logger.exception(
            "Failed to send shipping notification for %s",
            sub_order.sub_order_number,
        )
        status = EmailLog.Status.FAILED
        error_message = str(exc)

    log = log_document_email(
        sub_order,
        id=log_id,
        document_type=EmailLog.DocumentType.SHIPPING_NOTIFICATION,
        document_id=sub_order.pk,
        document_number=sub_order.sub_order_number,
        recipient_email=recipient_email,
        subject=subject,
        template_name=SHIPPING_TEMPLATE_NAME,
        status=status,
        error_message=error_message,
        triggered_by=triggered_by,
        sent_by=user,
        anymail_message_id=anymail_message_id,
    )
    return log


# -- Quote emails --

QUOTE_SUBJECT_TEMPLATE = f"Quote {{quote_number}} from {BRAND.get('company_name', 'Commerce')}"


def send_quote_email(quote, recipient_email, triggered_by, user, message_body=None):
    """Send a quote email with PDF attachment. Log result to EmailLog.

    Mirrors send_invoice_email. On success the quote is stamped sent_at /
    sent_to_email / sent_via and promoted DRAFT -> SENT, so the pipeline
    reflects that the customer has actually seen it.
    """
    from documents.services.pdf import render_quote_pdf
    from orders.models import Quote

    if triggered_by == "auto" and is_historical_document(quote):
        logger.info("Skipping auto-send for historical quote %s", quote.quote_number)
        return None

    subject = QUOTE_SUBJECT_TEMPLATE.format(quote_number=quote.quote_number)

    if not message_body:
        message_body = (
            f"Please find attached quote {quote.quote_number} "
            f"for ${quote.grand_total or 0:,.2f}."
        )
        if quote.quote_expiration_date:
            message_body += (
                f" This pricing is held until "
                f"{quote.quote_expiration_date.strftime('%B %d, %Y')}."
            )

    customer = quote.customer
    context = {
        "brand": BRAND,
        "customer_name": f"{customer.first_name} {customer.last_name}",
        "quote_number": quote.quote_number,
        "quote_date": quote.created_at.strftime("%B %d, %Y"),
        "expiration_date": (
            quote.quote_expiration_date.strftime("%B %d, %Y")
            if quote.quote_expiration_date
            else ""
        ),
        "grand_total": f"{quote.grand_total or 0:,.2f}",
        "status": quote.status,
        "status_display": quote.get_status_display(),
        "message_body": message_body,
    }

    html_body = render_to_string("communications/emails/quote_email.html", context)
    pdf_bytes = render_quote_pdf(quote)

    anymail_message_id = ""
    status = EmailLog.Status.SENT
    error_message = ""

    # Known before sending: the header lets the Gmail adapter skip this email when it comes
    # back through a staff mailbox (a Bcc to sales); it is logged here already.
    log_id = uuid.uuid4()
    try:
        msg = EmailMultiAlternatives(
            headers=golden_headers(log_id),
            subject=subject,
            body=message_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.attach(f"{quote.quote_number}.pdf", pdf_bytes, "application/pdf")
        msg.send()
        if hasattr(msg, "anymail_status"):
            anymail_message_id = getattr(msg.anymail_status, "message_id", "") or ""
    except Exception as e:
        logger.exception("Failed to send quote email for %s", quote.quote_number)
        status = EmailLog.Status.FAILED
        error_message = str(e)

    log = log_document_email(
        quote,
        id=log_id,
        document_type=EmailLog.DocumentType.QUOTE,
        document_id=quote.pk,
        document_number=quote.quote_number,
        recipient_email=recipient_email,
        subject=subject,
        template_name="quote_email",
        status=status,
        error_message=error_message,
        triggered_by=triggered_by,
        sent_by=user,
        anymail_message_id=anymail_message_id,
    )

    if status == EmailLog.Status.SENT:
        fields = []
        if not quote.sent_at:
            quote.sent_at = timezone.now()
            fields.append("sent_at")
        if quote.status == Quote.Status.DRAFT:
            quote.status = Quote.Status.SENT
            fields.append("status")
        quote.sent_to_email = recipient_email
        quote.sent_via = "email"
        fields += ["sent_to_email", "sent_via"]
        quote.save(update_fields=fields)

    return log
