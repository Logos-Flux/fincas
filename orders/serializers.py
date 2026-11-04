from rest_framework import serializers
from rest_framework.exceptions import APIException

from core.serializers import GuardedDocumentSerializer
from crm.api_serializers import ScopedEngagement

from .models import Order, OrderLineItem, Quote, QuoteLineItem, SalesInvoice, SubOrder


class QuoteLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteLineItem
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


#: Where clients read about the move (sent as the ``Link`` on quote responses).
ENGAGEMENT_DOCS = "docs/crm/phase3-engagement.md"
#: The old pipeline keys kept on quote payloads for one release (spec 1.6),
#: read-only and derived from the engagement.
QUOTE_COMPAT_KEYS = (
    "sales_stage", "win_probability", "estimated_close_date", "loss_reason", "follow_up_date",
)
LEGACY_QUOTE_FIELDS = [f"legacy_{name}" for name in Quote.MOVED_TO_ENGAGEMENT]


class MovedToEngagement(APIException):
    """400 ``moved_to_engagement``: a write to a pipeline field that left Quote."""

    status_code = 400
    default_code = "moved_to_engagement"

    def __init__(self, fields, quote=None):
        engagement_id = getattr(quote, "engagement_id", None)
        super().__init__()
        self.detail = {
            "detail": (
                f"{', '.join(fields)} moved to the quote's engagement (CRM Phase 3); "
                "change the deal there."
            ),
            "code": self.default_code,
            "fields": list(fields),
            "engagement": str(engagement_id) if engagement_id else "",
            "engagement_url": _engagement_url(engagement_id),
            "docs": ENGAGEMENT_DOCS,
        }


def _engagement_url(engagement_id):
    """The engagement's page: the staff API once it exists (step 7), the
    thread view (step 8), else its admin page."""
    if not engagement_id:
        return ""
    from django.urls import NoReverseMatch, reverse

    for name in ("engagement-detail", "erp-engagement-detail", "admin:crm_engagement_change"):
        try:
            return reverse(name, args=[engagement_id])
        except NoReverseMatch:
            continue
    return ""


class _QuoteCompatMixin(serializers.Serializer):
    """Read-only compat keys plus ``engagement``; refuse writes to moved keys."""

    engagement = serializers.PrimaryKeyRelatedField(read_only=True)
    sales_stage = serializers.SerializerMethodField()
    win_probability = serializers.SerializerMethodField()
    estimated_close_date = serializers.SerializerMethodField()
    loss_reason = serializers.SerializerMethodField()
    follow_up_date = serializers.SerializerMethodField()

    def get_sales_stage(self, obj):
        return obj.compat_value("sales_stage")

    def get_win_probability(self, obj):
        return obj.compat_value("win_probability")

    def get_estimated_close_date(self, obj):
        value = obj.compat_value("estimated_close_date")
        return value.isoformat() if value else None

    def get_loss_reason(self, obj):
        return obj.compat_value("loss_reason")

    def get_follow_up_date(self, obj):
        value = obj.compat_value("follow_up_date")
        return value.isoformat() if value else None

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            moved = [k for k in Quote.MOVED_TO_ENGAGEMENT if k in data]
            moved += [k for k in LEGACY_QUOTE_FIELDS if k in data]
            if moved:
                raise MovedToEngagement(moved, self.instance)
        return super().to_internal_value(data)


class QuoteListSerializer(_QuoteCompatMixin, serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = Quote
        fields = [
            "id", "quote_number", "revision_number", "status",
            "sales_stage", "engagement", "customer", "customer_name", "grand_total",
            "created_at",
        ]

    def get_customer_name(self, obj):
        return str(obj.customer)


class QuoteDetailSerializer(_QuoteCompatMixin, GuardedDocumentSerializer):
    line_items = QuoteLineItemSerializer(many=True, read_only=True)
    # Auto-generated in Quote.save() if blank — opt out of DRF's required=True
    # default for unique CharField, mirroring how OrderDetailSerializer treats
    # order_number.
    quote_number = serializers.CharField(
        required=False, allow_blank=True, max_length=20
    )

    # Computed by Quote.recalculate_totals() — never caller-written.
    SERVER_TOTAL_FIELDS = frozenset({
        "subtotal_ex_tax",
        "tax_total",
        "grand_total",
    })

    class Meta:
        model = Quote
        # Every column except the legacy pipeline ones; the compat keys and
        # ``engagement`` come from _QuoteCompatMixin.
        exclude = LEGACY_QUOTE_FIELDS
        read_only_fields = ["id", "created_at", "updated_at"]


class OrderLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderLineItem
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class SubOrderSerializer(GuardedDocumentSerializer):
    class Meta:
        model = SubOrder
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class OrderListSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "status", "payment_status",
            "fulfillment_status", "customer", "customer_name",
            "grand_total", "order_date", "external_id", "channel",
        ]

    def get_customer_name(self, obj):
        if obj.customer:
            return str(obj.customer)
        return obj.guest_email or ""


class OrderDetailSerializer(GuardedDocumentSerializer):
    line_items = OrderLineItemSerializer(many=True, read_only=True)
    sub_orders = SubOrderSerializer(many=True, read_only=True)
    order_number = serializers.CharField(required=False, allow_blank=True, max_length=20)

    # Computed by Order.recalculate_totals() / the payment ledger — never
    # caller-written. Discount inputs (discount_amount/discount_percent),
    # shipping_cost_ex_tax, handling_fee, and coupon/store-credit fields
    # remain writable as calculation inputs.
    SERVER_TOTAL_FIELDS = frozenset({
        "subtotal_ex_tax",
        "subtotal_inc_tax",
        "tax_total",
        "grand_total",
        "balance_due",
        "amount_paid",
    })

    # From the quote on conversion; set directly for an order sold without a
    # quote (spec 1.6). Scoped to the requester's tenant (CRM Phase 3 step 7).
    engagement = ScopedEngagement(required=False, allow_null=True)

    class Meta:
        model = Order
        fields = "__all__"
        read_only_fields = ["id", "order_date", "created_at", "updated_at"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Either side may change: a PATCH of ``company`` alone must not leave
        # the order on another tenant's engagement.
        engagement = attrs.get("engagement", getattr(self.instance, "engagement", None))
        company = attrs.get("company") or getattr(self.instance, "company", None)
        if engagement is not None and company is not None and engagement.company_id != company.pk:
            raise serializers.ValidationError({"engagement": "The engagement belongs to another company."})
        return attrs


def _money(source=None):
    kwargs = {"source": source} if source else {}
    return serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, **kwargs)


class GuestOrderLineSerializer(serializers.ModelSerializer):
    """A priced guest-checkout line as the storefront shows it (no costs)."""

    quantity = serializers.IntegerField(read_only=True)
    unit_price = _money("unit_price_ex_tax")
    line_total = _money("line_total_ex_tax")
    line_tax = _money()

    class Meta:
        model = OrderLineItem
        fields = ["variant", "sku", "item_name", "quantity", "unit_price", "line_total", "line_tax"]
        read_only_fields = fields


class GuestOrderConfirmationSerializer(serializers.ModelSerializer):
    """The storefront's guest-checkout confirmation (OP #3426).

    Only what the buyer needs to see: the server's priced lines and totals.
    No costs, margins, internal notes, metadata or IP address, which the full
    ``OrderDetailSerializer`` would hand to a browser. ``total_amount`` is
    ``grand_total`` under the name the storefront reads.
    """

    lines = GuestOrderLineSerializer(source="line_items", many=True, read_only=True)
    subtotal = _money("subtotal_ex_tax")
    tax_total = _money()
    shipping_total = _money("shipping_cost_ex_tax")
    discount_total = _money("discount_amount")
    grand_total = _money()
    total_amount = _money("grand_total")

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "status", "payment_status", "currency", "guest_email",
            "lines", "subtotal", "tax_total", "shipping_total", "discount_total",
            "grand_total", "total_amount", "created_at",
        ]
        read_only_fields = fields


class GuestCheckoutLineSerializer(serializers.Serializer):
    """OpenAPI only: one posted cart line. Send ``variant`` or ``product``."""

    variant = serializers.UUIDField(required=False, help_text="A sellable variant's id.")
    product = serializers.UUIDField(
        required=False,
        help_text="A product with exactly one sellable variant (else `variant_required`).",
    )
    quantity = serializers.IntegerField(min_value=1, help_text="Whole units, 1 to 9999 per variant.")


class GuestCheckoutSerializer(serializers.Serializer):
    """OpenAPI only: the storefront identity's guest-checkout body.

    Anything else in the body (prices, totals, status, discounts, company,
    customer) is ignored. Lines naming the same variant are merged.
    """

    email = serializers.EmailField(help_text="Also accepted as `guest_email`.")
    lines = GuestCheckoutLineSerializer(many=True, help_text="1 to 50 lines.")
    customer_name = serializers.CharField(required=False)
    company_name = serializers.CharField(required=False)
    shipping_address = serializers.CharField(required=False)
    payment_method = serializers.CharField(required=False, help_text="Stored as the order's payment terms.")
    po_number = serializers.CharField(required=False)
    notes = serializers.CharField(required=False)
    coupon_code = serializers.CharField(required=False, help_text="Recorded for staff; not applied.")


class GuestCheckoutLineErrorSerializer(serializers.Serializer):
    """OpenAPI only: why one posted line was refused."""

    index = serializers.IntegerField(help_text="Position of the line in the posted `lines`.")
    code = serializers.ChoiceField(choices=[
        "invalid_line", "invalid_quantity", "not_available", "variant_required",
        "price_unavailable", "out_of_stock", "insufficient_stock", "order_too_large",
    ])
    detail = serializers.CharField()
    variant = serializers.CharField(allow_null=True)
    product = serializers.CharField(allow_null=True)
    available = serializers.IntegerField(
        required=False, help_text="Units in stock (`out_of_stock`, `insufficient_stock`).",
    )


class GuestCheckoutErrorSerializer(serializers.Serializer):
    """OpenAPI only: a refused guest checkout (400). Nothing was created."""

    code = serializers.ChoiceField(
        choices=["lines_required", "too_many_lines", "invalid_lines", "order_too_large"],
    )
    detail = serializers.CharField()
    lines = GuestCheckoutLineErrorSerializer(many=True, required=False)


class SalesInvoiceSerializer(serializers.ModelSerializer):
    """Staff-facing serializer — full field set behind auth."""

    class Meta:
        model = SalesInvoice
        fields = "__all__"
        read_only_fields = [
            "id",
            "invoice_number",
            "payment_token",
            "created_at",
            "updated_at",
        ]


class _PublicInvoiceLineSerializer(serializers.ModelSerializer):
    """Line item projection for the public pay-by-link view.

    Intentionally narrow — payers see what they're being billed for and
    nothing about our internal cost, supplier, or fulfillment state.
    """

    class Meta:
        model = OrderLineItem
        fields = [
            "item_name",
            "description",
            "sku",
            "quantity",
            "selling_uom",
            "unit_price_ex_tax",
            "line_total_ex_tax",
        ]


class SalesInvoicePublicSerializer(serializers.ModelSerializer):
    """Read-only view an external payer sees on /pay/<token>.

    Excludes internal_notes, metadata, staff tracking timestamps, and
    anything about other invoices/orders on the customer record. Includes
    just enough to render a pay page: what's being billed, total owed,
    payment instructions, and our company identity for the "From" line.
    """

    order_number = serializers.CharField(source="order.order_number", read_only=True)
    currency = serializers.CharField(source="order.currency", read_only=True)
    line_items = _PublicInvoiceLineSerializer(
        source="order.line_items", many=True, read_only=True
    )
    customer_company_name = serializers.CharField(
        source="order.customer.company_name", read_only=True, default=""
    )
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.EmailField(
        source="order.customer.email", read_only=True
    )
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_website = serializers.CharField(
        source="company.website_url", read_only=True
    )

    class Meta:
        model = SalesInvoice
        fields = [
            "invoice_number",
            "status",
            "invoice_date",
            "due_date",
            "payment_terms",
            "payment_instructions",
            "subtotal_ex_tax",
            "tax_total",
            "shipping_cost",
            "grand_total",
            "amount_paid",
            "balance_due",
            "payment_token_expires_at",
            # Related projections
            "order_number",
            "currency",
            "line_items",
            "customer_company_name",
            "customer_name",
            "customer_email",
            "company_name",
            "company_website",
        ]
        read_only_fields = fields

    def get_customer_name(self, obj) -> str:
        c = obj.order.customer
        return f"{c.first_name} {c.last_name}".strip()
