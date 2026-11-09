import uuid as uuid_lib
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import models
from django.utils import timezone

from core import lookups  # noqa: F401  # registers `__ne` for uniq_order_stripe_pi
from core.managers import CompanyScopedManager
from core.models import BaseModel


def _q2(value):
    """Quantize a Decimal to 2 places (matches DB column scale)."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _previous_status(instance, save_kwargs):
    """Persisted status before this save, or None when it can't change.

    Skips the lookup for inserts and for ``update_fields`` saves that leave
    ``status`` alone (totals recalcs), so only status-bearing saves pay it.
    """
    if instance._state.adding:
        return None
    update_fields = save_kwargs.get("update_fields")
    if update_fields is not None and "status" not in update_fields:
        return None
    using = save_kwargs.get("using") or instance._state.db
    return (
        type(instance)._base_manager.using(using)
        .filter(pk=instance.pk)
        .values_list("status", flat=True)
        .first()
    )


def sales_stage_q(value):
    """Quotes whose read-compat ``sales_stage`` is ``value`` (spec 1.6).

    The inverse of ``crm.engagements.quote_compat_value``: won / lost by the
    engagement's status, an open stage by its key, ``prospecting`` also for
    open stages the old enum lacked. A quote with no engagement yet answers
    from its legacy column.
    """
    from crm.engagements import OLD_OPEN_SALES_STAGES

    if value == "closed_won":
        on_thread = models.Q(engagement__status="won")
    elif value == "closed_lost":
        on_thread = models.Q(engagement__status="lost")
    elif value == "prospecting":
        on_thread = models.Q(engagement__status="open") & (
            models.Q(engagement__stage__key=value)
            | ~models.Q(engagement__stage__key__in=OLD_OPEN_SALES_STAGES)
        )
    else:
        on_thread = models.Q(engagement__status="open", engagement__stage__key=value)
    return on_thread | models.Q(engagement__isnull=True, legacy_sales_stage=value)


class Quote(BaseModel):
    """Sales quote with revision tracking (75 fields)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending Approval"
        APPROVED = "approved", "Approved"
        SENT = "sent", "Sent"
        VIEWED = "viewed", "Viewed"
        NEGOTIATING = "negotiating", "Negotiating"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"
        SUPERSEDED = "superseded", "Superseded"
        LOST = "lost", "Lost"
        CANCELLED = "cancelled", "Canceled"

    class SalesStage(models.TextChoices):
        PROSPECTING = "prospecting", "Prospecting"
        QUALIFICATION = "qualification", "Qualification"
        NEEDS_ANALYSIS = "needs_analysis", "Needs Analysis"
        PROPOSAL_SENT = "proposal_sent", "Proposal Sent"
        NEGOTIATION = "negotiation", "Negotiation"
        VERBAL_COMMIT = "verbal_commit", "Verbal Commit"
        CLOSED_WON = "closed_won", "Closed Won"
        CLOSED_LOST = "closed_lost", "Closed Lost"

    class DealSize(models.TextChoices):
        SMALL = "small", "Small"
        MEDIUM = "medium", "Medium"
        LARGE = "large", "Large"
        ENTERPRISE = "enterprise", "Enterprise"

    class LossReason(models.TextChoices):
        PRICE = "price", "Price"
        COMPETITOR = "competitor", "Competitor"
        TIMING = "timing", "Timing"
        BUDGET = "budget", "Budget"
        NO_RESPONSE = "no_response", "No Response"
        SCOPE = "scope", "Scope Change"
        OTHER = "other", "Other"

    class AcceptanceMethod(models.TextChoices):
        EMAIL = "email_confirmation", "Email Confirmation"
        DIGITAL_SIG = "digital_signature", "Digital Signature"
        VERBAL = "verbal", "Verbal"
        PO_RECEIVED = "po_received", "PO Received"
        PORTAL = "portal_accept", "Portal Accept"

    class RevisionReason(models.TextChoices):
        PRICE_CHANGE = "price_change", "Price Change"
        SCOPE_CHANGE = "scope_change", "Scope Change"
        CUSTOMER_REQUEST = "customer_request", "Customer Request"
        ERROR_CORRECTION = "error_correction", "Error Correction"
        OTHER = "other", "Other"

    # Identification
    quote_number = models.CharField(max_length=20, unique=True)
    revision_number = models.IntegerField(default=1)
    revision_reason = models.CharField(max_length=20, choices=RevisionReason.choices, blank=True)
    superseded_by = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="supersedes")
    original_quote = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="revisions")
    # Audit link to the source quote when this row was created via the
    # duplicate action. Distinct from `original_quote` (revision tree) and
    # `superseded_by` (replacement chain) — duplicates are independent quotes.
    amended_from = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates")

    # Status
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    status_note = models.TextField(blank=True)

    # The sales thread (CRM Phase 3, docs/crm/phase3-engagement.md 1.6).
    # Every revision of a chain points at the same engagement; a duplicate
    # starts a new one. Set by ``crm.engagements.ensure_for_quote`` on save.
    engagement = models.ForeignKey(
        "crm.Engagement", on_delete=models.PROTECT, null=True, blank=True,
        related_name="quotes", db_index=False,
    )

    # Legacy pipeline fields (release N read-compat, spec 1.6). The deal lives
    # on ``engagement`` now: these keep their columns (``db_column``) so a
    # rollback loses nothing, nothing writes them after the step-5 backfill,
    # and step 12 drops them. Read ``sales_stage``, ``win_probability``,
    # ``estimated_close_date``, ``loss_reason`` and ``follow_up_date`` through
    # the deprecated properties below, or better, the engagement.
    legacy_sales_stage = models.CharField(
        max_length=20, choices=SalesStage.choices, default=SalesStage.PROSPECTING,
        db_column="sales_stage", editable=False,
    )
    legacy_win_probability = models.IntegerField(default=0, db_column="win_probability", editable=False)
    legacy_estimated_close_date = models.DateField(
        null=True, blank=True, db_column="estimated_close_date", editable=False,
    )
    legacy_weighted_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, db_column="weighted_value", editable=False,
    )
    legacy_deal_size_category = models.CharField(
        max_length=20, choices=DealSize.choices, blank=True, db_column="deal_size_category",
        editable=False,
    )
    legacy_loss_reason = models.CharField(
        max_length=20, choices=LossReason.choices, blank=True, db_column="loss_reason", editable=False,
    )
    legacy_loss_competitor = models.CharField(
        max_length=255, blank=True, db_column="loss_competitor", editable=False,
    )
    legacy_loss_notes = models.TextField(blank=True, db_column="loss_notes", editable=False)
    legacy_lost_at = models.DateTimeField(null=True, blank=True, db_column="lost_at", editable=False)

    # Validity & delivery
    validity_period_days = models.IntegerField(default=7)
    quote_expiration_date = models.DateField(null=True, blank=True)
    extended_count = models.IntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_via = models.CharField(max_length=50, blank=True)
    sent_to_email = models.EmailField(blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
    legacy_follow_up_date = models.DateField(
        null=True, blank=True, db_column="follow_up_date", editable=False,
    )
    legacy_last_contacted_at = models.DateTimeField(
        null=True, blank=True, db_column="last_contacted_at", editable=False,
    )

    # Acceptance
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by_name = models.CharField(max_length=255, blank=True)
    accepted_by_title = models.CharField(max_length=255, blank=True)
    acceptance_method = models.CharField(max_length=20, choices=AcceptanceMethod.choices, blank=True)
    customer_po_number = models.CharField(max_length=100, blank=True)
    acceptance_document_url = models.URLField(blank=True)

    # Approval
    requires_approval = models.BooleanField(default=False)
    approval_status = models.CharField(max_length=20, blank=True)
    approved_by = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_quotes")
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_notes = models.TextField(blank=True)

    # Profitability
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_margin = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    target_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    pricing_strategy_notes = models.TextField(blank=True)

    # Proposed terms
    payment_terms_proposed = models.CharField(max_length=100, blank=True)
    shipping_terms_proposed = models.CharField(max_length=100, blank=True)
    special_conditions = models.TextField(blank=True)
    include_standard_terms = models.BooleanField(default=True)
    customer_notes = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)

    # Conversion
    converted_to_order = models.ForeignKey("Order", on_delete=models.SET_NULL, null=True, blank=True, related_name="source_quotes")
    converted_at = models.DateTimeField(null=True, blank=True)
    converted_by = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="converted_quotes")

    # Shared fields (same as Order)
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="quotes")
    billing_address = models.ForeignKey("customers.CustomerAddress", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    shipping_address = models.ForeignKey("customers.CustomerAddress", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    contact = models.ForeignKey("customers.CustomerContact", on_delete=models.SET_NULL, null=True, blank=True, related_name="quotes")
    sales_rep = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="sales_quotes")
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    channel = models.CharField(max_length=50, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=6, default=1)
    price_list = models.ForeignKey("pricing.PriceList", on_delete=models.SET_NULL, null=True, blank=True)

    # Totals
    subtotal_ex_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_cost_ex_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    coupon_discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Tax
    tax_rate = models.ForeignKey("pricing.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)
    tax_exempt = models.BooleanField(default=False)

    # Company
    company = models.ForeignKey("core.Company", on_delete=models.CASCADE)
    legacy_campaign = models.CharField(max_length=255, blank=True, db_column="campaign", editable=False)
    legacy_lead_source = models.CharField(
        max_length=255, blank=True, db_column="lead_source", editable=False,
    )
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    # ``manage.py check_tenancy``: the engagement is in the quote's company.
    tenancy_parents = ("engagement",)

    #: The pipeline fields that left Quote for the engagement (spec 1.6),
    #: by their old (API) names. Step 12 drops the ``legacy_*`` columns.
    MOVED_TO_ENGAGEMENT = (
        "sales_stage", "win_probability", "estimated_close_date", "weighted_value",
        "deal_size_category", "loss_reason", "loss_competitor", "loss_notes", "lost_at",
        "follow_up_date", "last_contacted_at", "campaign", "lead_source",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["engagement"], name="quote_engagement_idx"),
        ]

    def __str__(self):
        return f"{self.quote_number} v{self.revision_number}"

    def save(self, *args, **kwargs):
        from django.db import transaction

        if not self.quote_number:
            from core.models import DocumentSequence
            self.quote_number = DocumentSequence.get_next_number(
                self.company, Quote, "quote_number", "QT"
            )
        if self.engagement_id is not None:
            super().save(*args, **kwargs)
            return
        # Every quote sits on a sales thread: a new quote gets one here (a
        # revision inherits its original's), and a legacy quote the step-5
        # backfill left alone (an untouched draft) gets one on its next save.
        from crm.engagements import ensure_for_quote

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "engagement"}
        with transaction.atomic():
            self.engagement = ensure_for_quote(self)
            super().save(*args, **kwargs)

    # --- read-compat for one release (spec 1.6) -----------------------------

    def compat_value(self, name):
        """The old pipeline field ``name``, derived from the engagement.

        No deprecation warning: the staff API's compat keys read through
        this. Code should read the engagement instead.
        """
        from crm.engagements import quote_compat_value

        return quote_compat_value(self, name)

    def _deprecated(self, name):
        import warnings

        warnings.warn(
            f"Quote.{name} moved to the engagement (docs/crm/phase3-engagement.md 1.6); "
            "read quote.engagement instead. The property goes in Phase 3 step 12.",
            DeprecationWarning, stacklevel=3,
        )
        return self.compat_value(name)

    @property
    def sales_stage(self):
        """Deprecated: the engagement's stage in the old vocabulary."""
        return self._deprecated("sales_stage")

    @property
    def win_probability(self):
        """Deprecated: the engagement's effective probability."""
        return self._deprecated("win_probability")

    @property
    def estimated_close_date(self):
        """Deprecated: ``engagement.expected_close_date``."""
        return self._deprecated("estimated_close_date")

    @property
    def loss_reason(self):
        """Deprecated: ``engagement.loss_reason`` (blank unless lost)."""
        return self._deprecated("loss_reason")

    @property
    def follow_up_date(self):
        """Deprecated: the local due date of the engagement's next action."""
        return self._deprecated("follow_up_date")

    #: Guarded status transitions. Terminal states (accepted / expired /
    #: superseded / lost / cancelled) have no outbound moves: a converted or
    #: dead quote is changed via a new revision, never by mutating status.
    #: Direct ORM/ERP-form writes bypass this map; the DRF ``transition``
    #: action enforces it (illegal moves → 400 with a field-error envelope),
    #: and the detail serializer rejects direct ``status`` PATCHes outright.
    VALID_TRANSITIONS = {
        "draft": ["pending_approval", "sent", "cancelled"],
        "pending_approval": ["approved", "sent", "cancelled"],
        "approved": ["sent", "cancelled"],
        "sent": ["viewed", "negotiating", "accepted", "lost", "expired", "cancelled"],
        "viewed": ["negotiating", "accepted", "lost", "expired", "cancelled"],
        "negotiating": ["accepted", "lost", "expired", "cancelled", "sent"],
        "accepted": [],
        "expired": [],
        "superseded": [],
        "lost": [],
        "cancelled": [],
    }

    def transition_to(self, new_status):
        """Validate and apply a status transition."""
        allowed = self.VALID_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            from django.core.exceptions import ValidationError
            raise ValidationError(
                f"Cannot transition from '{self.status}' to '{new_status}'. "
                f"Allowed: {allowed}"
            )
        self.status = new_status
        self.save(update_fields=["status", "updated_at"])

    def recalculate_totals(self):
        """Recompute aggregate totals from active line items.

        Per-line writes use `.update()` to bypass post_save signals and
        avoid recursion back into this method.
        """
        subtotal = Decimal("0")
        tax = Decimal("0")
        for line in self.line_items.all():
            qty = Decimal(line.quantity or 0)
            unit = Decimal(line.unit_price_ex_tax or 0)
            disc_pct = Decimal(line.discount_percent or 0)
            disc_amt = Decimal(line.discount_amount or 0)
            if disc_pct > 0:
                disc_amt = _q2(qty * unit * disc_pct / Decimal(100))
            line_total = _q2((qty * unit) - disc_amt)
            updates = {}
            if line_total != Decimal(line.line_total_ex_tax or 0):
                updates["line_total_ex_tax"] = line_total
            if disc_pct > 0 and disc_amt != Decimal(line.discount_amount or 0):
                updates["discount_amount"] = disc_amt
            if updates:
                updates["updated_at"] = timezone.now()
                QuoteLineItem.objects.filter(pk=line.pk).update(**updates)
            subtotal += line_total
            tax += Decimal(line.line_tax or 0)

        subtotal = _q2(subtotal)
        tax = _q2(tax)

        discount_amount = Decimal(self.discount_amount or 0)
        if Decimal(self.discount_percent or 0) > 0:
            discount_amount = _q2(subtotal * Decimal(self.discount_percent) / Decimal(100))

        shipping = Decimal(self.shipping_cost_ex_tax or 0)
        grand_total = _q2(subtotal - discount_amount + tax + shipping)

        self.subtotal_ex_tax = subtotal
        self.tax_total = tax
        self.discount_amount = discount_amount
        self.grand_total = grand_total
        self.save(
            update_fields=[
                "subtotal_ex_tax",
                "tax_total",
                "discount_amount",
                "grand_total",
                "updated_at",
            ]
        )


class QuoteLineItem(BaseModel):
    """Line item on a quote (44 fields)."""

    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="line_items")
    shipment_group = models.ForeignKey(
        "ShipmentGroup", on_delete=models.SET_NULL, null=True, blank=True, related_name="quote_line_items"
    )
    sort_order = models.IntegerField(default=0)

    # Product snapshot.
    # variant is nullable so a quote can carry a freeform / non-catalog
    # "misc" line, matching OrderLineItem.variant (see :737). item_name and
    # sku stay required — a line still has to say what it is and carry a
    # code, even when nothing in the catalog backs it.
    variant = models.ForeignKey(
        "products.ProductVariant", on_delete=models.PROTECT, null=True, blank=True
    )
    item_name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    image_id = models.CharField(max_length=64, blank=True)
    item_type = models.CharField(max_length=50, blank=True)

    # Quantity
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    selling_uom = models.CharField(max_length=20, default="each")

    # Pricing
    unit_price_ex_tax = models.DecimalField(max_digits=10, decimal_places=4)
    unit_price_inc_tax = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    list_price = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    line_total_ex_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    line_total_inc_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    line_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price_list = models.ForeignKey("pricing.PriceList", on_delete=models.SET_NULL, null=True, blank=True)

    # Cost
    unit_cost = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    supplier_product = models.ForeignKey("products.SupplierProduct", on_delete=models.SET_NULL, null=True, blank=True)
    margin_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Weight
    item_weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    # Tax
    tax_rate = models.ForeignKey("pricing.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)

    # Quote-specific fields
    is_optional = models.BooleanField(default=False)
    is_alternative = models.BooleanField(default=False)
    alternative_group = models.CharField(max_length=50, blank=True)
    customer_selected = models.BooleanField(default=True)
    availability_status = models.CharField(max_length=50, blank=True)
    availability_checked_at = models.DateTimeField(null=True, blank=True)
    estimated_ship_date = models.DateField(null=True, blank=True)
    line_notes = models.TextField(blank=True)
    internal_line_notes = models.TextField(blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.quote} — {self.sku}"


class Order(BaseModel):
    """Sales order (82 fields)."""

    class Status(models.TextChoices):
        INCOMPLETE = "incomplete", "Incomplete"
        PENDING = "pending", "Pending"
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting Payment"
        AWAITING_FULFILLMENT = "awaiting_fulfillment", "Awaiting Fulfillment"
        AWAITING_SHIPMENT = "awaiting_shipment", "Awaiting Shipment"
        PARTIALLY_SHIPPED = "partially_shipped", "Partially Shipped"
        SHIPPED = "shipped", "Shipped"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Canceled"
        DECLINED = "declined", "Declined"
        DISPUTED = "disputed", "Disputed"
        MANUAL_VERIFICATION_REQUIRED = "manual_verification_required", "Manual Verification Required"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially Refunded"
        ON_HOLD = "on_hold", "On Hold"
        BACKORDERED = "backordered", "Backordered"

    # "Open" = still needs work from us before it ships (F-089, 2026-10-19):
    # the dashboard's Open Orders card. Shipped, completed, cancelled,
    # declined and the post-shipment money states (disputed, partially
    # refunded) are not open; neither is an abandoned checkout (incomplete).
    OPEN_STATUSES = (
        Status.PENDING,
        Status.AWAITING_PAYMENT,
        Status.AWAITING_FULFILLMENT,
        Status.AWAITING_SHIPMENT,
        Status.PARTIALLY_SHIPPED,
        Status.ON_HOLD,
        Status.BACKORDERED,
        Status.MANUAL_VERIFICATION_REQUIRED,
    )

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PARTIAL = "partial", "Partial"
        PAID = "paid", "Paid"
        REFUNDED = "refunded", "Refunded"
        VOID = "void", "Void"

    class FulfillmentStatus(models.TextChoices):
        UNFULFILLED = "unfulfilled", "Unfulfilled"
        PARTIAL = "partial", "Partial"
        FULFILLED = "fulfilled", "Fulfilled"
        RETURNED = "returned", "Returned"

    class OrderType(models.TextChoices):
        SALES = "sales", "Sales"
        MAINTENANCE = "maintenance", "Maintenance"
        DROP_SHIP = "drop_ship", "Drop Ship"
        INTERNAL_TRANSFER = "internal_transfer", "Internal Transfer"

    # Identification
    order_number = models.CharField(max_length=20, unique=True)
    slug = models.SlugField(blank=True)

    # Customer
    # Guest checkout (F-037): nullable FK — storefront guest orders carry
    # guest_email with no Customer account link. Staff orders keep the
    # required customer FK.
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
    )
    guest_email = models.EmailField(
        blank=True,
        help_text="Guest checkout email — set when the order has no customer account.",
    )
    billing_address = models.ForeignKey("customers.CustomerAddress", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    shipping_address = models.ForeignKey("customers.CustomerAddress", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    contact = models.ForeignKey("customers.CustomerContact", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    customer_message = models.TextField(blank=True)

    # Status
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.PENDING)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID)
    fulfillment_status = models.CharField(max_length=20, choices=FulfillmentStatus.choices, default=FulfillmentStatus.UNFULFILLED)
    status_note = models.TextField(blank=True)
    booked_at = models.DateTimeField(null=True, blank=True)

    # Order type & source
    order_type = models.CharField(max_length=20, choices=OrderType.choices, default=OrderType.SALES)
    quote = models.ForeignKey(Quote, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    channel = models.CharField(max_length=50, blank=True)
    external_source = models.CharField(max_length=100, blank=True)
    external_id = models.CharField(max_length=255, blank=True)
    customer_po_number = models.CharField(max_length=100, blank=True)
    customer_po_date = models.DateField(null=True, blank=True)

    # Dates
    order_date = models.DateTimeField(default=timezone.now)
    requested_ship_date = models.DateField(null=True, blank=True)
    shipped_date = models.DateTimeField(null=True, blank=True)

    # Totals
    currency = models.CharField(max_length=3, default="USD")
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=6, default=1)
    price_list = models.ForeignKey("pricing.PriceList", on_delete=models.SET_NULL, null=True, blank=True)
    subtotal_ex_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal_inc_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    coupon_discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    store_credit_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    refunded_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Shipping (rollups from SubOrders)
    shipping_cost_ex_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_cost_inc_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_cost_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    handling_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Payment
    payment_terms = models.CharField(max_length=100, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, db_index=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    balance_due = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Tax
    tax_rate = models.ForeignKey("pricing.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)
    tax_provider_id = models.CharField(max_length=255, blank=True)
    tax_exempt = models.BooleanField(default=False)
    tax_exempt_certificate = models.CharField(max_length=100, blank=True)

    # Sales
    sales_rep = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="sales_orders")
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    campaign = models.CharField(max_length=255, blank=True)
    lead_source = models.CharField(max_length=255, blank=True)
    # The sales thread (spec 1.6): inherited from the quote on conversion, or
    # set directly for an order sold without a quote. Storefront orders get
    # none (a checkout is not a sales thread).
    engagement = models.ForeignKey(
        "crm.Engagement", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="orders", db_index=False,
    )

    # Internal
    company = models.ForeignKey("core.Company", on_delete=models.CASCADE)
    amended_from = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True)
    terms_text = models.TextField(blank=True)
    include_standard_terms = models.BooleanField(default=True)
    internal_notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    # Email tracking
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)

    # Legacy system IDs
    bc_id = models.CharField(max_length=64, blank=True, db_index=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)
    d1_id = models.CharField(max_length=64, blank=True, db_index=True)

    objects = CompanyScopedManager()

    # ``manage.py check_tenancy``: the engagement is in the order's company.
    tenancy_parents = ("engagement",)

    class Meta:
        ordering = ["-order_date"]
        constraints = [
            # One Order per Stripe PaymentIntent: the checkout's own create
            # and a webhook backstop can race for the same intent. Blank ids
            # (orders not paid through Stripe) are exempt. `__ne` (core.lookups)
            # stores the predicate as `<>`; `~Q(...)` would store `NOT (... = ...)`.
            models.UniqueConstraint(
                fields=["stripe_payment_intent_id"],
                condition=models.Q(stripe_payment_intent_id__ne=""),
                name="uniq_order_stripe_pi",
            ),
        ]
        indexes = [
            models.Index(fields=["engagement"], name="order_engagement_idx"),
        ]

    def __str__(self):
        return self.order_number

    def save(self, *args, **kwargs):
        if not self.order_number:
            from core.models import DocumentSequence
            self.order_number = DocumentSequence.get_next_number(
                self.company, Order, "order_number", "SO"
            )
            self.slug = self.order_number.lower()
        if not self.slug:
            self.slug = self.order_number.lower()
        adding = self._state.adding
        previous = _previous_status(self, kwargs)
        super().save(*args, **kwargs)
        self._emit_lifecycle(adding, previous)

    def _emit_lifecycle(self, adding, previous):
        """Plugin lifecycle signals — the single emission point for every
        order transition (staff form, DRF, payment settle, sub-order roll-up)."""
        from core.plugins import signals
        from core.plugins.dispatch import emit

        live = self.status not in (
            self.Status.INCOMPLETE, self.Status.CANCELLED, self.Status.DECLINED,
        )
        if adding:
            if live:
                emit(signals.order_placed, Order, self, order=self)
            return
        if previous is None or previous == self.status:
            return
        emit(
            signals.order_status_changed, Order, self,
            order=self, old_status=previous, new_status=self.status,
        )
        if previous == self.Status.INCOMPLETE and live:
            emit(signals.order_placed, Order, self, order=self)
        if self.status == self.Status.CANCELLED:
            emit(signals.order_cancelled, Order, self, order=self, reason="")

    #: Guarded status transitions. The terminal set mirrors the roll-up guard
    #: in ``update_status_from_suborders`` (cancelled / declined / disputed /
    #: partially_refunded) plus completed: a finished or dead order moves only
    #: via a new document, never by mutating status. Direct ORM writes, the
    #: ERP staff form, the payment-reconcile advance, and the sub-order
    #: roll-up bypass this map; the DRF ``transition`` action enforces it
    #: (illegal moves → 400 with a field-error envelope), and the detail
    #: serializer rejects direct ``status`` PATCHes outright.
    VALID_TRANSITIONS = {
        "incomplete": ["pending", "cancelled"],
        "pending": [
            "awaiting_payment", "awaiting_fulfillment", "on_hold",
            "cancelled", "declined", "manual_verification_required",
        ],
        "awaiting_payment": [
            "awaiting_fulfillment", "on_hold", "cancelled", "declined",
        ],
        "awaiting_fulfillment": [
            "awaiting_shipment", "backordered", "on_hold", "cancelled",
        ],
        "awaiting_shipment": [
            "partially_shipped", "shipped", "backordered", "on_hold",
            "cancelled",
        ],
        "partially_shipped": ["shipped", "backordered", "on_hold", "disputed"],
        "shipped": ["completed", "disputed", "partially_refunded"],
        "completed": [],
        "cancelled": [],
        "declined": [],
        "disputed": [],
        "manual_verification_required": ["pending", "declined", "cancelled"],
        "partially_refunded": [],
        "on_hold": [
            "pending", "awaiting_payment", "awaiting_fulfillment",
            "cancelled",
        ],
        "backordered": ["awaiting_fulfillment", "awaiting_shipment", "cancelled"],
    }

    def transition_to(self, new_status):
        """Validate and apply a status transition."""
        allowed = self.VALID_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            from django.core.exceptions import ValidationError
            raise ValidationError(
                f"Cannot transition from '{self.status}' to '{new_status}'. "
                f"Allowed: {allowed}"
            )
        self.status = new_status
        self.save(update_fields=["status", "updated_at"])

    def update_status_from_suborders(self):
        """Derive order status from SubOrder statuses. Call after SubOrder changes."""
        # Terminal statuses are set by explicit staff/payment actions; a late
        # SubOrder roll-up must never resurrect them (WP #764).
        terminal = {
            self.Status.CANCELLED,
            self.Status.DECLINED,
            self.Status.DISPUTED,
            self.Status.PARTIALLY_REFUNDED,
        }
        if self.status in terminal:
            return

        subs = self.sub_orders.all()
        if not subs.exists():
            return

        statuses = set(subs.values_list("status", flat=True))
        shipped_or_delivered = {"shipped", "in_transit", "delivered"}
        in_progress = {"confirmed", "ready_to_ship", "label_created", "picked", "packed"}
        awaiting = {"purchasing", "awaiting_stock"}

        if statuses == {"delivered"}:
            new_status = self.Status.COMPLETED
        elif statuses <= shipped_or_delivered:
            new_status = self.Status.SHIPPED
        elif statuses & shipped_or_delivered:
            new_status = self.Status.PARTIALLY_SHIPPED
        elif statuses & in_progress:
            new_status = self.Status.AWAITING_SHIPMENT
        elif statuses & awaiting:
            new_status = self.Status.AWAITING_FULFILLMENT
        else:
            return

        if self.status != new_status:
            self.status = new_status
            self.save(update_fields=["status", "updated_at"])

        # Update fulfillment status
        if statuses == {"delivered"}:
            new_ff = self.FulfillmentStatus.FULFILLED
        elif statuses & shipped_or_delivered:
            new_ff = self.FulfillmentStatus.PARTIAL
        else:
            new_ff = None

        if new_ff and self.fulfillment_status != new_ff:
            self.fulfillment_status = new_ff
            self.save(update_fields=["fulfillment_status", "updated_at"])

    def recalculate_totals(self):
        """Recompute aggregate totals from line items + parent-level adjustments.

        Per-line writes use `.update()` to bypass post_save signals and
        avoid recursion back into this method.
        """
        subtotal = Decimal("0")
        tax = Decimal("0")
        for line in self.line_items.all():
            qty = Decimal(line.quantity or 0)
            unit = Decimal(line.unit_price_ex_tax or 0)
            disc_pct = Decimal(line.discount_percent or 0)
            disc_amt = Decimal(line.discount_amount or 0)
            if disc_pct > 0:
                disc_amt = _q2(qty * unit * disc_pct / Decimal(100))
            line_total = _q2((qty * unit) - disc_amt)
            updates = {}
            if line_total != Decimal(line.line_total_ex_tax or 0):
                updates["line_total_ex_tax"] = line_total
            if disc_pct > 0 and disc_amt != Decimal(line.discount_amount or 0):
                updates["discount_amount"] = disc_amt
            if updates:
                updates["updated_at"] = timezone.now()
                OrderLineItem.objects.filter(pk=line.pk).update(**updates)
            subtotal += line_total
            tax += Decimal(line.line_tax or 0)

        subtotal = _q2(subtotal)
        tax = _q2(tax)

        discount_amount = Decimal(self.discount_amount or 0)
        if Decimal(self.discount_percent or 0) > 0:
            discount_amount = _q2(subtotal * Decimal(self.discount_percent) / Decimal(100))

        shipping = Decimal(self.shipping_cost_ex_tax or 0)
        handling = Decimal(self.handling_fee or 0)
        grand_total = _q2(subtotal - discount_amount + tax + shipping + handling)

        # Coupon accounting (WP #572 BUG-06 + adversarial gate rounds 1-2):
        # grand_total above already subtracts discount_amount. Web checkout
        # mirrors a coupon's value into BOTH discount_amount and
        # coupon_discount (orders-core.ts buildDjangoOrderPayload), so the
        # MIRRORED portion min(coupon, discount_amount) is already netted in
        # grand_total. What may still be owed a deduction is the NON-mirrored
        # excess max(coupon - discount_amount, 0) — the path where admin
        # edits, the DRF API (fields="__all__"), the AI-assist writable-fields
        # path, or legacy bc/erpnext/d1 rows set coupon_discount without an
        # equal discount_amount. Subtracting exactly that excess makes the
        # coupon deducted in total min(coupon, discount_amount) (via
        # grand_total) + max(coupon - discount_amount, 0) (here) == coupon —
        # never more, never less:
        #   * mirrored web order (coupon == discount): excess 0, no
        #     double-count (the original BUG-06 fix);
        #   * standalone legacy coupon (discount == 0): full coupon deducted;
        #   * non-coupon discount with no coupon: coupon == 0, excess 0.
        amount_paid = Decimal(self.amount_paid or 0)
        store_credit = Decimal(self.store_credit_amount or 0)
        coupon = Decimal(self.coupon_discount or 0)
        balance_due = _q2(
            grand_total - amount_paid - store_credit
            - max(coupon - Decimal(discount_amount or 0), Decimal("0"))
        )

        self.subtotal_ex_tax = subtotal
        self.subtotal_inc_tax = _q2(subtotal + tax)
        self.tax_total = tax
        self.discount_amount = discount_amount
        self.grand_total = grand_total
        self.balance_due = balance_due
        self.save(
            update_fields=[
                "subtotal_ex_tax",
                "subtotal_inc_tax",
                "tax_total",
                "discount_amount",
                "grand_total",
                "balance_due",
                "updated_at",
            ]
        )


class SubOrder(BaseModel):
    """Fulfillment group within an order (44 fields)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        PURCHASING = "purchasing", "Purchasing"
        AWAITING_STOCK = "awaiting_stock", "Awaiting Stock"
        READY_TO_SHIP = "ready_to_ship", "Ready to Ship"
        LABEL_CREATED = "label_created", "Label Created"
        PICKED = "picked", "Picked"
        PACKED = "packed", "Packed"
        SHIPPED = "shipped", "Shipped"
        IN_TRANSIT = "in_transit", "In Transit"
        DELIVERED = "delivered", "Delivered"
        EXCEPTION = "exception", "Exception"
        RETURNED = "returned", "Returned"
        CANCELLED = "cancelled", "Canceled"

    VALID_TRANSITIONS = {
        "pending": ["confirmed", "cancelled"],
        "confirmed": ["purchasing", "awaiting_stock", "ready_to_ship", "cancelled"],
        "purchasing": ["awaiting_stock", "ready_to_ship", "exception"],
        "awaiting_stock": ["ready_to_ship", "exception", "cancelled"],
        "ready_to_ship": ["label_created", "picked", "exception"],
        "label_created": ["picked", "exception"],
        "picked": ["packed", "exception"],
        "packed": ["shipped", "exception"],
        "shipped": ["in_transit", "delivered", "exception"],
        "in_transit": ["delivered", "exception"],
        "delivered": ["returned"],
        "exception": ["pending", "confirmed", "cancelled"],
        "returned": [],
        "cancelled": [],
    }

    sub_order_number = models.CharField(max_length=25, unique=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="sub_orders")
    sort_order = models.IntegerField(default=0)

    # Source
    warehouse = models.ForeignKey("inventory.Warehouse", on_delete=models.SET_NULL, null=True, blank=True)
    supplier = models.ForeignKey("purchasing.Supplier", on_delete=models.SET_NULL, null=True, blank=True)
    supplier_product_default = models.ForeignKey("products.SupplierProduct", on_delete=models.SET_NULL, null=True, blank=True)
    ship_from_address = models.ForeignKey("customers.CustomerAddress", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    ship_from_label = models.CharField(max_length=100, blank=True)
    is_drop_ship = models.BooleanField(default=False)

    # Shipping
    shipping_method = models.CharField(max_length=100, blank=True)
    carrier = models.CharField(max_length=100, blank=True)
    service_level = models.CharField(max_length=100, blank=True)
    carrier_account = models.CharField(max_length=100, blank=True)
    is_third_party_billing = models.BooleanField(default=False)

    # Tracking
    tracking_number = models.CharField(max_length=255, blank=True)
    tracking_url = models.URLField(blank=True)
    bol_number = models.CharField(max_length=100, blank=True)
    shipping_label_url = models.URLField(blank=True)
    pod_url = models.URLField(blank=True)

    # Status
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    status_note = models.TextField(blank=True)

    # Action docs
    purchase_order = models.ForeignKey("purchasing.PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True)
    pick_ticket = models.ForeignKey("fulfillment.PickTicket", on_delete=models.SET_NULL, null=True, blank=True)

    # Dates
    estimated_ship_date = models.DateField(null=True, blank=True)
    actual_ship_date = models.DateTimeField(null=True, blank=True)
    estimated_delivery_date = models.DateField(null=True, blank=True)
    actual_delivery_date = models.DateTimeField(null=True, blank=True)

    # Shipping cost
    shipping_cost_ex_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_cost_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_cost_inc_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    insurance_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    handling_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    actual_freight_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_calc_snapshot = models.JSONField(default=dict, blank=True)

    # Packages
    package_count = models.IntegerField(default=0)
    total_weight = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    packages = models.JSONField(default=list, blank=True)

    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    processed_by = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True)

    # Email tracking
    shipping_notification_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "sort_order"]

    def __str__(self):
        return self.sub_order_number

    def save(self, *args, **kwargs):
        adding = self._state.adding
        previous = _previous_status(self, kwargs)
        super().save(*args, **kwargs)
        self._emit_lifecycle(adding, previous)

    def _emit_lifecycle(self, adding, previous):
        """Shipment signals — single emission point for label service, DRF
        transition, ERP and admin edits. A SubOrder is the shipment."""
        if not adding and (previous is None or previous == self.status):
            return
        from core.plugins import signals
        from core.plugins.dispatch import emit

        moving = {self.Status.SHIPPED, self.Status.IN_TRANSIT, self.Status.DELIVERED}
        if self.status == self.Status.SHIPPED and previous not in moving:
            emit(
                signals.shipment_dispatched, SubOrder, self,
                shipment=self, tracking_number=self.tracking_number,
            )
        elif self.status == self.Status.DELIVERED:
            emit(signals.shipment_delivered, SubOrder, self, shipment=self)

    def transition_to(self, new_status):
        """Validate and apply status transition."""
        allowed = self.VALID_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            from django.core.exceptions import ValidationError
            raise ValidationError(
                f"Cannot transition from '{self.status}' to '{new_status}'. "
                f"Allowed: {allowed}"
            )
        self.status = new_status
        self.save(update_fields=["status", "updated_at"])


class ShipmentGroup(BaseModel):
    """Lightweight line-item grouping for split-ship planning."""

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, null=True, blank=True, related_name="shipment_groups"
    )
    quote = models.ForeignKey(
        Quote, on_delete=models.CASCADE, null=True, blank=True, related_name="shipment_groups"
    )
    name = models.CharField(max_length=100, default="Shipment 1")
    warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.SET_NULL, null=True, blank=True
    )

    class FulfillmentType(models.TextChoices):
        DROP_SHIP = "drop_ship", "Drop Ship"
        INTERNAL = "internal", "Internal Warehouse"

    fulfillment_type = models.CharField(
        max_length=15, choices=FulfillmentType.choices, default=FulfillmentType.DROP_SHIP,
    )
    supplier = models.ForeignKey(
        "purchasing.Supplier", on_delete=models.SET_NULL, null=True, blank=True,
    )
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        parent = self.order or self.quote
        return f"{parent} — {self.name}" if parent else self.name


class OrderLineItem(BaseModel):
    """Line item on an order (57 fields)."""

    class FulfillmentStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PO_SENT = "po_sent", "PO Sent"

        PICK_ASSIGNED = "pick_assigned", "Pick Assigned"
        IN_PROGRESS = "in_progress", "In Progress"
        READY = "ready", "Ready"
        SHIPPED = "shipped", "Shipped"
        DELIVERED = "delivered", "Delivered"
        RETURNED = "returned", "Returned"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="line_items")
    shipment_group = models.ForeignKey(
        ShipmentGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_line_items"
    )
    sort_order = models.IntegerField(default=0)

    # Product snapshot (variant nullable for web orders with unmatched SKUs)
    variant = models.ForeignKey(
        "products.ProductVariant", on_delete=models.PROTECT, null=True, blank=True
    )
    item_name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    image_id = models.CharField(max_length=64, blank=True)
    item_type = models.CharField(max_length=50, blank=True)

    # Quantity
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    quantity_shipped = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    quantity_returned = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    selling_uom = models.CharField(max_length=20, default="each")

    # Pricing
    unit_price_ex_tax = models.DecimalField(max_digits=10, decimal_places=4)
    unit_price_inc_tax = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    list_price = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    line_total_ex_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    line_total_inc_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    line_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price_list = models.ForeignKey("pricing.PriceList", on_delete=models.SET_NULL, null=True, blank=True)

    # Cost
    unit_cost = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    supplier_product = models.ForeignKey("products.SupplierProduct", on_delete=models.SET_NULL, null=True, blank=True)
    supplier_sku = models.CharField(max_length=100, blank=True)
    lead_time_days = models.IntegerField(null=True, blank=True)
    shipping_origin = models.CharField(max_length=100, blank=True)
    margin_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Weight
    item_weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_width = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_height = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_length = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    # Fulfillment
    sub_order = models.ForeignKey(SubOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="line_items")
    purchase_order_line = models.ForeignKey("purchasing.POLineItem", on_delete=models.SET_NULL, null=True, blank=True)
    pick_ticket_line = models.ForeignKey("fulfillment.PickTicketLine", on_delete=models.SET_NULL, null=True, blank=True)
    fulfillment_status = models.CharField(max_length=20, choices=FulfillmentStatus.choices, default=FulfillmentStatus.PENDING)
    requested_ship_date = models.DateField(null=True, blank=True)

    # Tax
    tax_rate = models.ForeignKey("pricing.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)

    # Returns
    return_id = models.CharField(max_length=100, blank=True)
    refunded_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.order} — {self.sku}"


class SalesInvoice(BaseModel):
    """
    Sales Invoice — the payment-tracking document for a Sales Order.
    Tracks what is owed, what has been paid, and provides payment links.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        VIEWED = "viewed", "Viewed"
        OVERDUE = "overdue", "Overdue"
        # PROCESSING covers the ACH 3-5 business day clearance window: Stripe
        # Checkout has completed (customer authorised) but the money hasn't
        # moved yet. Flips to PAID on payment_intent.succeeded.
        PROCESSING = "processing", "Processing (ACH)"
        PARTIAL = "partial", "Partially Paid"
        PAID = "paid", "Paid"
        VOID = "void", "Void"
        CANCELLED = "cancelled", "Canceled"

    class PaymentTerms(models.TextChoices):
        PREPAID = "prepaid", "Prepaid"
        DUE_ON_RECEIPT = "due_on_receipt", "Due on Receipt"
        NET_15 = "net_15", "Net 15"
        NET_30 = "net_30", "Net 30"
        NET_45 = "net_45", "Net 45"
        NET_60 = "net_60", "Net 60"

    # Identification
    invoice_number = models.CharField(max_length=20, unique=True)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="invoices")

    # Status
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)

    # Dates
    invoice_date = models.DateField(default=date.today)
    due_date = models.DateField(null=True, blank=True)
    paid_date = models.DateField(null=True, blank=True)

    # Payment terms (snapshot from SO at invoicing time)
    payment_terms = models.CharField(
        max_length=20, choices=PaymentTerms.choices, default=PaymentTerms.DUE_ON_RECEIPT
    )
    payment_instructions = models.TextField(blank=True)

    # Totals (snapshot from SO)
    subtotal_ex_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Payment link
    payment_token = models.UUIDField(default=uuid_lib.uuid4, unique=True)
    payment_token_expires_at = models.DateTimeField(null=True, blank=True)

    # Tracking
    sent_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
    last_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    # Notes
    notes = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)

    # Multi-tenant
    company = models.ForeignKey("core.Company", on_delete=models.CASCADE)
    metadata = models.JSONField(default=dict, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["-invoice_date", "-created_at"]

    def __str__(self):
        return self.invoice_number

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            from core.models import DocumentSequence
            self.invoice_number = DocumentSequence.get_next_number(
                self.company, SalesInvoice, "invoice_number", "INV"
            )
        previous = (
            SalesInvoice.objects.filter(pk=self.pk)
            .values_list("status", flat=True)
            .first()
            if self.pk
            else None
        )
        # Post BEFORE the doc save: posting is idempotent, so a doc-save
        # failure self-heals on retry, while a post failure blocks the
        # status change loudly instead of leaving a saved-but-unposted doc.
        issued_now = voided_now = False
        if self.status not in (
            self.Status.DRAFT,
            self.Status.VOID,
            self.Status.CANCELLED,
        ) and previous in (None, self.Status.DRAFT):
            from .posting import post_sales_invoice

            post_sales_invoice(self)
            issued_now = True
        elif self.status in (self.Status.VOID, self.Status.CANCELLED) and (
            previous not in (None, self.Status.VOID, self.Status.CANCELLED)
        ):
            from accounting.models import JournalEntry

            from .posting import reverse_sales_invoice

            try:
                reverse_sales_invoice(self)
            except JournalEntry.DoesNotExist:
                pass  # voided before ever leaving DRAFT: nothing to reverse
            voided_now = previous != self.Status.DRAFT
        super().save(*args, **kwargs)
        if (issued_now and previous is not None) or voided_now:
            # Customer deposits linked to this invoice move to A/R when it is
            # issued and back to deposits when it is voided (0.4.4).
            from payments.services import apply_invoice_deposits

            apply_invoice_deposits(self)

    def recalculate_totals(self):
        """Re-snapshot financials from the parent Order.

        Skips finalized invoices (PAID / VOID / CANCELLED) so already-
        settled records aren't perturbed by upstream order edits.
        """
        if self.status in {
            self.Status.PAID,
            self.Status.VOID,
            self.Status.CANCELLED,
        }:
            return

        order = self.order
        self.subtotal_ex_tax = _q2(order.subtotal_ex_tax or 0)
        self.tax_total = _q2(order.tax_total or 0)
        self.shipping_cost = _q2(order.shipping_cost_ex_tax or 0)
        self.grand_total = _q2(order.grand_total or 0)
        amount_paid = Decimal(self.amount_paid or 0)
        self.balance_due = _q2(self.grand_total - amount_paid)
        self.save(
            update_fields=[
                "subtotal_ex_tax",
                "tax_total",
                "shipping_cost",
                "grand_total",
                "balance_due",
                "updated_at",
            ]
        )


class DocumentContact(BaseModel):
    """A person on a quote or an order, beside the customer it is for.

    No role: the title shown is the person's open ``employment`` title at the
    customer's party (``parties.PartyRelationship.title``). Exactly one of
    ``quote`` / ``order`` is set. ``company`` is copied from the document on
    every save. Party merges re-point rows (``parties/merge.py``); a person
    already on the same document keeps one row.
    """

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    quote = models.ForeignKey(
        Quote, on_delete=models.CASCADE, null=True, blank=True, related_name="people",
    )
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, null=True, blank=True, related_name="people",
    )
    party = models.ForeignKey(
        "parties.Party", on_delete=models.PROTECT, related_name="document_contacts",
    )

    objects = CompanyScopedManager()

    tenancy_parents = ("quote", "order", "party")

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(quote__isnull=False, order__isnull=True)
                    | models.Q(quote__isnull=True, order__isnull=False)
                ),
                name="doccontact_one_document",
            ),
            models.UniqueConstraint(
                fields=["quote", "party"], condition=models.Q(quote__isnull=False),
                name="uniq_doccontact_quote_party",
            ),
            models.UniqueConstraint(
                fields=["order", "party"], condition=models.Q(order__isnull=False),
                name="uniq_doccontact_order_party",
            ),
        ]

    def __str__(self):
        return f"{self.party} on {self.quote or self.order}"

    @property
    def document(self):
        return self.quote if self.quote_id else self.order

    def save(self, *args, **kwargs):
        document = self.document
        if document is not None:
            self.company_id = document.company_id
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "company"}
        super().save(*args, **kwargs)
