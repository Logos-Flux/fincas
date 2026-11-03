"""Back-office payment forms."""

from __future__ import annotations

from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import Payment


class RecordPaymentForm(forms.Form):
    """"Record payment" on the invoice page (R1.16): amount, date, method, reference.

    Defaults to the balance due and today; ``payments.services.record_payment``
    does the work and the balance check, so a stale page cannot overpay.
    """

    amount = forms.DecimalField(min_value=Decimal("0.01"), max_digits=12, decimal_places=2, label="Amount")
    payment_date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    payment_method = forms.ChoiceField(
        choices=Payment.PaymentMethod.choices, initial=Payment.PaymentMethod.CHECK, label="Method",
    )
    reference = forms.CharField(max_length=255, required=False, label="Reference",
                                help_text="Check or transaction number.")
    notes = forms.CharField(max_length=2000, required=False, label="Notes",
                            widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, invoice=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.invoice = invoice
        if invoice is not None:
            self.fields["amount"].initial = invoice.balance_due
            self.fields["amount"].widget.attrs["max"] = str(invoice.balance_due)
        self.fields["payment_date"].initial = timezone.localdate()
        self.fields["amount"].widget.attrs.update({"step": "0.01", "min": "0.01"})

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if self.invoice is not None and amount > Decimal(self.invoice.balance_due or 0):
            raise forms.ValidationError(
                f"More than the balance due ({self.invoice.balance_due})."
            )
        return amount


class RefundForm(forms.Form):
    """"Refund" on the invoice page (R5.02): which payment, how much, why.

    ``payments.services.refund_payment`` does the work and the amount check
    against what is still refundable, so a stale page cannot over-refund.
    """

    payment = forms.ModelChoiceField(queryset=Payment.objects.none(), label="Payment")
    amount = forms.DecimalField(min_value=Decimal("0.01"), max_digits=12, decimal_places=2, label="Amount")
    reason = forms.CharField(max_length=255, label="Reason",
                             widget=forms.TextInput(attrs={"placeholder": "Why the money goes back"}))

    def __init__(self, *args, invoice=None, **kwargs):
        from .services import refundable_amount

        super().__init__(*args, **kwargs)
        self.invoice = invoice
        self.refundable = []
        if invoice is not None:
            candidates = invoice.payments.filter(payment_type=Payment.PaymentType.PAYMENT).order_by("-payment_date")
            self.refundable = [(p, refundable_amount(p)) for p in candidates if refundable_amount(p) > 0]
            self.fields["payment"].queryset = Payment.objects.filter(pk__in=[p.pk for p, _ in self.refundable])
            if self.refundable:
                first, left = self.refundable[0]
                self.fields["payment"].initial = first.pk
                self.fields["amount"].initial = left
        self.fields["payment"].label_from_instance = (
            lambda p: f"{p.payment_number} · {p.get_payment_method_display()} · {p.amount} {p.currency}"
        )
        self.fields["amount"].widget.attrs.update({"step": "0.01", "min": "0.01"})

    def clean(self):
        from .services import refundable_amount

        data = super().clean()
        payment, amount = data.get("payment"), data.get("amount")
        if payment is not None and amount is not None and amount > refundable_amount(payment):
            self.add_error("amount", f"More than the {refundable_amount(payment)} still refundable on {payment.payment_number}.")
        return data
