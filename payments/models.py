from django.db import models

from core.managers import CompanyScopedManager
from core.models import BaseModel


class Payment(BaseModel):
    """Payment record (51 fields) — includes fraud/risk, card/bank details."""

    class PaymentMethod(models.TextChoices):
        CREDIT_CARD = "credit_card", "Credit Card"
        DEBIT_CARD = "debit_card", "Debit Card"
        ACH = "ach", "ACH"
        WIRE_TRANSFER = "wire_transfer", "Wire Transfer"
        CHECK = "check", "Check"
        PAYPAL = "paypal", "PayPal"
        MARKETPLACE = "marketplace", "Marketplace"
        STORE_CREDIT = "store_credit", "Store Credit"
        CASH = "cash", "Cash"
        OTHER = "other", "Other"

    class PaymentProvider(models.TextChoices):
        STRIPE = "stripe", "Stripe"
        PAYPAL = "paypal", "PayPal"
        SQUARE = "square", "Square"
        BANK = "bank", "Bank"
        EBAY = "ebay", "eBay"
        AMAZON = "amazon", "Amazon"
        MANUAL = "manual", "Manual"
        OTHER = "other", "Other"

    class PaymentType(models.TextChoices):
        PAYMENT = "payment", "Payment"
        DEPOSIT = "deposit", "Deposit"
        REFUND = "refund", "Refund"
        CHARGEBACK = "chargeback", "Chargeback"
        VOID = "void", "Void"
        ADJUSTMENT = "adjustment", "Adjustment"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AUTHORIZED = "authorized", "Authorized"
        CAPTURED = "captured", "Captured"
        SETTLED = "settled", "Settled"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Canceled"
        REFUNDED = "refunded", "Refunded"
        DISPUTED = "disputed", "Disputed"

    class RiskLevel(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class RiskDecision(models.TextChoices):
        ACCEPT = "accept", "Accept"
        REVIEW = "review", "Review"
        REJECT = "reject", "Reject"

    # Identification
    payment_number = models.CharField(max_length=20, unique=True)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payments")
    invoice = models.ForeignKey(
        "orders.SalesInvoice", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="payments",
    )

    # Method
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    payment_provider = models.CharField(max_length=20, choices=PaymentProvider.choices)

    # Transaction
    transaction_id = models.CharField(max_length=255, blank=True)
    authorization_code = models.CharField(max_length=100, blank=True)
    gateway_response = models.JSONField(default=dict, blank=True)
    # Client-supplied idempotency key: retried POSTs with the same key
    # return the original Payment instead of charging twice.
    idempotency_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # Amount
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=6, default=1)
    processing_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Surcharge we added on top of the invoice amount (e.g. 3% card convenience
    # fee). Distinct from processing_fee (Stripe's cut) — this one is ours.
    # invoice.amount_paid = payment.amount - payment.convenience_fee.
    convenience_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Type
    payment_type = models.CharField(max_length=20, choices=PaymentType.choices, default=PaymentType.PAYMENT)
    refund_reason = models.CharField(max_length=255, blank=True)
    parent_payment = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="child_payments")

    # Status
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    status_note = models.TextField(blank=True)
    payment_date = models.DateTimeField(null=True, blank=True)
    settled_date = models.DateTimeField(null=True, blank=True)

    # Card details
    card_brand = models.CharField(max_length=20, blank=True)
    card_last_four = models.CharField(max_length=4, blank=True)
    card_exp_month = models.CharField(max_length=2, blank=True)
    card_exp_year = models.CharField(max_length=4, blank=True)

    # Bank details
    bank_name = models.CharField(max_length=255, blank=True)
    bank_reference = models.CharField(max_length=255, blank=True)

    # Fraud/Risk
    risk_score = models.IntegerField(null=True, blank=True)
    risk_level = models.CharField(max_length=10, choices=RiskLevel.choices, blank=True)
    risk_decision = models.CharField(max_length=10, choices=RiskDecision.choices, blank=True)
    risk_reason = models.CharField(max_length=255, blank=True)
    risk_message = models.TextField(blank=True)
    risk_rules_triggered = models.JSONField(default=list, blank=True)
    risk_response_raw = models.JSONField(default=dict, blank=True)
    fraud_report = models.TextField(blank=True)
    fraud_reviewed_by = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="fraud_reviews")
    fraud_reviewed_at = models.DateTimeField(null=True, blank=True)

    # Verification
    avs_result = models.CharField(max_length=10, blank=True)
    cvv_result = models.CharField(max_length=10, blank=True)
    three_d_secure_result = models.CharField(max_length=50, blank=True)
    three_d_secure_version = models.CharField(max_length=10, blank=True)
    network_status = models.CharField(max_length=50, blank=True)
    network_decline_code = models.CharField(max_length=50, blank=True)

    # Device
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_fingerprint = models.CharField(max_length=255, blank=True)

    # Marketplace
    marketplace_order_id = models.CharField(max_length=255, blank=True)
    marketplace_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Internal
    received_by = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    # Company scoping
    company = models.ForeignKey("core.Company", on_delete=models.CASCADE, null=True, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["-payment_date"]
        constraints = [
            # One Payment row per gateway transaction. Partial: manual/legacy
            # rows with a blank transaction_id and non-payment types (refund,
            # chargeback, …) can share the same transaction_id.
            models.UniqueConstraint(
                fields=["transaction_id"],
                condition=models.Q(payment_type="payment") & ~models.Q(transaction_id=""),
                name="uniq_payment_txn",
            ),
        ]

    def __str__(self):
        return f"{self.payment_number} — {self.amount} {self.currency}"

    def save(self, *args, **kwargs):
        if not self.payment_number:
            from core.models import DocumentSequence
            self.payment_number = DocumentSequence.get_next_number(
                self.company or self.order.company, Payment, "payment_number", "PAY"
            )
        previous_row = (
            Payment.objects.filter(pk=self.pk)
            .values_list("status", "invoice_id")
            .first()
            if self.pk
            else None
        )
        previous = previous_row[0] if previous_row else None
        settled = (self.Status.CAPTURED, self.Status.SETTLED)
        # Post BEFORE the doc save (see SalesInvoice.save): idempotent, so a
        # doc-save failure self-heals on retry while a post failure blocks loudly.
        if self.status in settled and (previous not in settled):
            from .posting import post_payment

            post_payment(self)
        elif self.status == self.Status.REFUNDED and previous != self.Status.REFUNDED:
            from accounting.models import JournalEntry

            from .posting import post_payment, reverse_payment

            if self.payment_type == self.PaymentType.REFUND:
                post_payment(self)
            elif self.pk and self.child_payments.filter(
                payment_type=self.PaymentType.REFUND
            ).exists():
                # Refunded through refund rows (payments.services.refund_payment):
                # each row posted the money back under the refund rule, so
                # reversing the payment's own entry would count it twice.
                pass
            else:
                try:
                    reverse_payment(self)
                except JournalEntry.DoesNotExist:
                    pass  # refunded before ever settling: nothing to reverse
        elif self.status == self.Status.CANCELLED and (
            previous not in (None, self.Status.CANCELLED)
        ):
            from accounting.models import JournalEntry

            from .posting import reverse_payment

            try:
                reverse_payment(self)
            except JournalEntry.DoesNotExist:
                pass  # voided before ever settling: nothing to reverse
        super().save(*args, **kwargs)
        # Customer deposits (0.4.4): an existing payment whose invoice link or
        # status changed may have to move its deposit to A/R, or back. New
        # rows need nothing: a payment that settles with no invoice stays in
        # deposits, and one with an invoice posted to A/R directly.
        if previous_row is not None and (
            previous_row[1] != self.invoice_id or previous != self.status
        ):
            from .posting import sync_deposit_application

            sync_deposit_application(self)
        self._emit_lifecycle(previous)

    def _emit_lifecycle(self, previous):
        """Plugin payment signals — the single emission point for gateway
        charges, Stripe/webhook reconcile, ERP, admin and refund rows. Same
        transition test as the GL posting above."""
        from core.plugins import signals
        from core.plugins.dispatch import emit

        settled = (self.Status.CAPTURED, self.Status.SETTLED)
        is_refund_row = self.payment_type == self.PaymentType.REFUND
        if self.status in settled and previous not in settled and not is_refund_row:
            emit(signals.payment_completed, Payment, self, payment=self)
        elif self.status == self.Status.REFUNDED and previous != self.Status.REFUNDED:
            if is_refund_row:
                emit(
                    signals.payment_refunded, Payment, self,
                    payment=self.parent_payment, refund=self,
                )
            else:
                emit(signals.payment_refunded, Payment, self, payment=self, refund=None)
