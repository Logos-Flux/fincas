import io
import zipfile
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from accounting.posting import NoPostingRule, PostingError
from communications.services import send_invoice_email
from core import once
from core.managers import scope_to_company
from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin
from documents.services.pdf import render_invoice_html, render_invoice_pdf
from orders.models import SalesInvoice

from .forms import RecordPaymentForm, RefundForm
from .models import Payment
from .services import PaymentRefused, RefundRefused, record_payment, refund_payment


class InvoiceListView(ERPListView):
    model = SalesInvoice
    current_section = "invoices"
    page_title = "Sales Invoices"
    entity_name = "Invoice"
    ordering = ["-invoice_date", "-created_at"]
    search_fields = ["invoice_number", "order__order_number", "order__customer__first_name", "order__customer__last_name"]
    list_columns = [
        {"field": "invoice_number", "label": "Invoice #", "link": True, "link_url_name": "erp-invoice-detail"},
        {"field": "order__customer", "label": "Customer"},
        {"field": "order__order_number", "label": "Order #"},
        {"field": "invoice_date", "label": "Date", "date_only": True},
        {"field": "due_date", "label": "Due Date", "date_only": True},
        {"field": "grand_total", "label": "Total", "money": True, "align": "end"},
        {"field": "amount_paid", "label": "Paid", "money": True, "align": "end"},
        {"field": "balance_due", "label": "Balance", "money": True, "align": "end"},
        {"field": "status", "label": "Status", "badge": True},
    ]
    list_filters = [
        {"field": "status", "label": "Status", "choices": SalesInvoice.Status.choices},
        {"field": "payment_terms", "label": "Terms", "choices": SalesInvoice.PaymentTerms.choices},
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("order", "order__customer")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["bulk_actions"] = [
            {
                "label": "Export PDF",
                "icon": "fas fa-file-pdf",
                "url": reverse("erp-invoice-bulk-pdf"),
                "color": "primary",
            },
        ]
        return context


def _invoice_for_request(request, pk):
    """The invoice, scoped to the viewer's company (superusers see all)."""
    return get_object_or_404(
        scope_to_company(
            SalesInvoice.objects.select_related(
                "order", "order__customer",
                "order__billing_address", "order__shipping_address",
            ),
            request,
        ),
        pk=pk,
    )


class InvoiceDetailView(ERPViewMixin, TemplateView):
    """The invoice's back-office page: totals, the payments on it, record a
    payment, and the way to the preview, the PDF, the email and the order.

    Until 2026-10-17 this URL was the print preview itself (the PDF's HTML,
    no chrome, nothing to click to get back): "Create Invoice" landed there
    and no screen recorded a payment (R1.14, R1.16).
    """

    template_name = "erp/invoice_detail.html"
    current_section = "invoices"
    entity_name = "Invoice"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invoice = _invoice_for_request(self.request, self.kwargs["pk"])
        context["invoice"] = invoice
        context["order"] = invoice.order
        context["customer"] = invoice.order.customer
        context["customer_email"] = invoice.order.customer.email if invoice.order.customer_id else ""
        context["payments"] = list(invoice.payments.order_by("-payment_date", "-created_at"))
        context["page_title"] = f"Invoice {invoice.invoice_number}"
        context["can_record_payment"] = (
            invoice.status not in (SalesInvoice.Status.VOID, SalesInvoice.Status.CANCELLED)
            and Decimal(invoice.balance_due or 0) > 0
        )
        context["form"] = kwargs.get("form") or RecordPaymentForm(invoice=invoice)
        refund_form = RefundForm(invoice=invoice)
        context["refund_form"] = refund_form
        context["refundable"] = refund_form.refundable
        context["can_refund"] = bool(refund_form.refundable)
        context["refunded_total"] = sum(
            (Decimal(p.amount or 0) for p in context["payments"]
             if p.payment_type == Payment.PaymentType.REFUND and p.status == Payment.Status.REFUNDED),
            Decimal("0.00"),
        )
        return context


class InvoicePreviewView(LoginRequiredMixin, View):
    """The invoice as the PDF renders it, in the browser (print preview)."""

    def get(self, request, pk):
        invoice = _invoice_for_request(request, pk)
        return HttpResponse(render_invoice_html(invoice))


class RecordPaymentView(LoginRequiredMixin, View):
    """POST: record money received against the invoice (R1.16).

    Goes through ``payments.services.record_payment`` (one settled manual
    Payment, posted under the company's payment rule, invoice and order
    settled from the ledger). The form's ``_once`` token is claimed first
    (F-033) and doubles as the payment's idempotency key, so a double
    click records one payment; a refusal releases the claim and says why.
    """

    login_url = "/erp/login/"

    def post(self, request, pk):
        invoice = _invoice_for_request(request, pk)
        back = reverse("erp-invoice-detail", args=[invoice.pk])
        form = RecordPaymentForm(request.POST, invoice=invoice)
        if not form.is_valid():
            for field, errors in form.errors.items():
                label = form.fields[field].label if field in form.fields else field
                for error in errors:
                    messages.error(request, f"{label}: {error}")
            return redirect(back)
        if not once.claim(request, "record_payment", invoice):
            messages.info(request, f"That payment on {invoice.invoice_number} was already recorded from this form.")
            return redirect(back)
        token = once.token_from(request.POST)
        data = form.cleaned_data
        try:
            payment = record_payment(
                invoice=invoice,
                amount=data["amount"],
                payment_date=data["payment_date"],
                payment_method=data["payment_method"],
                reference=data["reference"],
                notes=data["notes"],
                received_by=request.user.get_username(),
                idempotency_key=f"erp-once:{token}" if token else "",
            )
        except NoPostingRule as exc:
            once.release(request, "record_payment", invoice)
            messages.error(
                request,
                "Not recorded: the company has no posting rule for payments, so the "
                f"payment cannot reach the ledger. Set one up under Accounting > Posting rules. ({exc})",
            )
            return redirect(back)
        except (PaymentRefused, PostingError) as exc:
            once.release(request, "record_payment", invoice)
            messages.error(request, f"Not recorded: {exc}")
            return redirect(back)
        except ValidationError as exc:
            # The payment's date falls in a closed fiscal period: the posting
            # refused inside the transaction, so nothing was stored (R9.06).
            once.release(request, "record_payment", invoice)
            messages.error(request, f"Not recorded: {' '.join(exc.messages)}")
            return redirect(back)
        invoice.refresh_from_db()
        left = Decimal(invoice.balance_due or 0)
        if left <= 0:
            messages.success(request, f"Payment {payment.payment_number} recorded; {invoice.invoice_number} is paid in full.")
        else:
            messages.success(request, f"Payment {payment.payment_number} recorded; {left} still due on {invoice.invoice_number}.")
        return redirect(back)


class RefundView(LoginRequiredMixin, View):
    """POST: refund part or all of a payment on the invoice (R5.02).

    Goes through ``payments.services.refund_payment`` (one refund row under
    the parent payment, posted under the company's refund rule; the parent,
    the order's refunded amount and payment status follow). The form's
    ``_once`` token is claimed first (F-033) and doubles as the refund's
    idempotency key, so a double click issues one refund; a refusal
    releases the claim and says why.
    """

    login_url = "/erp/login/"

    def post(self, request, pk):
        invoice = _invoice_for_request(request, pk)
        back = reverse("erp-invoice-detail", args=[invoice.pk]) + "#refund"
        form = RefundForm(request.POST, invoice=invoice)
        if not form.is_valid():
            for field, errors in form.errors.items():
                label = form.fields[field].label if field in form.fields else field
                for error in errors:
                    messages.error(request, f"{label}: {error}")
            return redirect(back)
        if not once.claim(request, "refund", invoice):
            messages.info(request, f"That refund on {invoice.invoice_number} was already issued from this form.")
            return redirect(back)
        token = once.token_from(request.POST)
        data = form.cleaned_data
        try:
            refund = refund_payment(
                data["payment"], data["amount"], reason=data["reason"],
                refunded_by=request.user.get_username(),
                idempotency_key=f"erp-once:{token}" if token else "",
            )
        except NoPostingRule as exc:
            once.release(request, "refund", invoice)
            messages.error(
                request,
                "Not refunded: the company has no posting rule for refunds, so the refund "
                f"cannot reach the ledger. Set one up under Accounting > Posting rules. ({exc})",
            )
            return redirect(back)
        except (RefundRefused, PostingError) as exc:
            once.release(request, "refund", invoice)
            messages.error(request, f"Not refunded: {exc}")
            return redirect(back)
        except ValidationError as exc:
            once.release(request, "refund", invoice)
            messages.error(request, f"Not refunded: {' '.join(exc.messages)}")
            return redirect(back)
        messages.success(
            request,
            f"Refund {refund.payment_number} of {refund.amount} issued against "
            f"{refund.parent_payment.payment_number} on {invoice.invoice_number}.",
        )
        return redirect(back)


class InvoicePDFView(LoginRequiredMixin, View):
    """Download invoice as PDF."""

    def get(self, request, pk):
        invoice = get_object_or_404(
            SalesInvoice.objects.select_related(
                "order", "order__customer",
                "order__billing_address", "order__shipping_address",
            ),
            pk=pk,
        )
        pdf_bytes = render_invoice_pdf(invoice)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{invoice.invoice_number}.pdf"'
        return response


class BulkInvoicePDFView(LoginRequiredMixin, View):
    """Export one or more invoices as PDF. Single = direct PDF, multiple = ZIP."""

    def post(self, request):
        ids = request.POST.getlist("ids")
        if not ids:
            messages.warning(request, "No invoices selected.")
            return redirect("erp-invoice-list")

        invoices = list(
            SalesInvoice.objects.filter(pk__in=ids)
            .select_related(
                "order", "order__customer",
                "order__billing_address", "order__shipping_address",
            )
            .order_by("invoice_date")
        )

        if not invoices:
            messages.warning(request, "No invoices found.")
            return redirect("erp-invoice-list")

        # Single invoice — return PDF directly
        if len(invoices) == 1:
            inv = invoices[0]
            pdf_bytes = render_invoice_pdf(inv)
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{inv.invoice_number}.pdf"'
            return response

        # Multiple invoices — return ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for inv in invoices:
                pdf_bytes = render_invoice_pdf(inv)
                zf.writestr(f"{inv.invoice_number}.pdf", pdf_bytes)

        buf.seek(0)
        response = HttpResponse(buf.read(), content_type="application/zip")
        response["Content-Disposition"] = 'attachment; filename="invoices.pdf.zip"'
        return response


class SendInvoiceEmailView(LoginRequiredMixin, View):
    """Send invoice email with PDF attachment."""

    def post(self, request, pk):
        invoice = get_object_or_404(
            SalesInvoice.objects.select_related("order", "order__customer"),
            pk=pk,
        )
        recipient = invoice.order.customer.email
        back = _safe_next(request) or reverse("erp-invoice-list")
        # F-033: the form's _once token is claimed before sending; the same
        # token posted again (double click, retried request) sends nothing.
        if not once.claim(request, "send_invoice", invoice):
            messages.info(request, f"Invoice {invoice.invoice_number} was already sent from this form.")
            return redirect(back)
        try:
            log = send_invoice_email(
                invoice=invoice,
                recipient_email=recipient,
                triggered_by="manual",
                user=request.user,
            )
        except ValidationError as exc:
            # A closed fiscal period (checked before anything is sent), or
            # any other refusal the posting raises: say why, send nothing.
            once.release(request, "send_invoice", invoice)
            messages.error(request, f"Not sent: {' '.join(exc.messages)}")
            return redirect(back)
        if log and log.status == "sent":
            messages.success(request, f"Invoice {invoice.invoice_number} sent to {recipient}")
        elif log:
            once.release(request, "send_invoice", invoice)
            messages.error(request, f"Failed to send: {log.error_message}")
        else:
            messages.warning(request, "Email blocked (historical document)")
        return redirect(back)


def _safe_next(request):
    """A same-site ``next`` from the form, or ``""``."""
    nxt = (request.POST.get("next") or "").strip()
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return ""
