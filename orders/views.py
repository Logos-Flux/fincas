from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, PolymorphicProxySerializer, extend_schema
from rest_framework import status as http_status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import Throttled
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.client_ip import client_ip
from core.managers import scope_to_company
from core.models import Company
from core.permissions import PrincipalPermission
from core.plugins import signals
from core.plugins.dispatch import emit, suppressed
from core.storefront import is_storefront_user
from core.throttling import GuestOrderThrottle
from core.views import CompanyScopedViewSet

from . import guest_checkout
from .convert import convert_order_to_invoice, convert_quote_to_order
from .duplicate import duplicate_order, duplicate_quote
from .models import (
    Order,
    OrderLineItem,
    Quote,
    QuoteLineItem,
    SalesInvoice,
    ShipmentGroup,
    SubOrder,
    sales_stage_q,
)
from .serializers import (
    ENGAGEMENT_DOCS,
    GuestCheckoutErrorSerializer,
    GuestCheckoutSerializer,
    GuestOrderConfirmationSerializer,
    OrderDetailSerializer,
    OrderLineItemSerializer,
    OrderListSerializer,
    QuoteDetailSerializer,
    QuoteLineItemSerializer,
    QuoteListSerializer,
    SalesInvoicePublicSerializer,
    SalesInvoiceSerializer,
    SubOrderSerializer,
)

#: Longest buyer field (name, address, ...) a guest checkout stores.
GUEST_FIELD_MAX_CHARS = 1000


class QuoteViewSet(CompanyScopedViewSet):
    queryset = Quote.objects.select_related(
        "customer", "company", "engagement__stage", "engagement__next_task",
    ).prefetch_related("line_items")
    filterset_fields = ["status", "company", "customer", "sales_rep", "engagement"]
    # A revision joins its original's engagement, so the chain links must
    # stay inside the tenant.
    parent_links = [("original_quote", "company"), ("superseded_by", "company"),
                    ("amended_from", "company")]
    search_fields = ["quote_number", "customer__email"]
    ordering_fields = ["created_at", "grand_total"]

    def get_queryset(self):
        qs = super().get_queryset()
        # ``?sales_stage=`` keeps working for one release (spec 1.6),
        # translated to the engagement in the old vocabulary.
        stage = self.request.query_params.get("sales_stage")
        if stage:
            qs = qs.filter(sales_stage_q(stage))
        return qs

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # The pipeline keys on quote payloads are read-compat (spec 1.6).
        response["Deprecation"] = "true"
        response["Link"] = f'<{ENGAGEMENT_DOCS}>; rel="deprecation"'
        return response

    def get_serializer_class(self):
        if self.action == "list":
            return QuoteListSerializer
        return QuoteDetailSerializer

    @action(detail=True, methods=["post"], url_path="duplicate")
    def duplicate(self, request, pk=None):
        """Create a draft copy of this quote with a fresh number."""
        new_quote = duplicate_quote(self.get_object())
        return Response(
            QuoteDetailSerializer(new_quote).data,
            status=http_status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="recalculate")
    def recalculate(self, request, pk=None):
        """Server-side recalculation of aggregate fields from current line items."""
        quote = self.get_object()
        quote.recalculate_totals()
        quote.refresh_from_db()
        return Response(QuoteDetailSerializer(quote).data)

    @action(detail=True, methods=["post"], url_path="convert-to-order")
    def convert_to_order(self, request, pk=None):
        """Get-or-create the Sales Order for this quote.

        Idempotent: returns the existing order (200) if this quote was
        already converted, otherwise creates one (201). The `created` flag
        in the response body lets callers distinguish.

        The new order lands in PENDING and sends no email — advancing it is
        a separate, deliberate act.
        """
        order, created = convert_quote_to_order(
            self.get_object(), user=getattr(request.user, "userprofile", None)
        )
        body = OrderDetailSerializer(order).data
        body["created"] = created
        return Response(
            body,
            status=http_status.HTTP_201_CREATED if created else http_status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        """Guarded status transition. Body: ``{"status": "<new_status>"}``.

        Legal moves (``Quote.VALID_TRANSITIONS``) apply and return 200;
        illegal moves — including any move out of a terminal state — return
        400 with the pinned field-error envelope (``{"status": [...]}``).
        """
        quote = self.get_object()
        try:
            quote.transition_to(request.data.get("status"))
        except DjangoValidationError as exc:
            raise DRFValidationError({"status": exc.messages})
        quote.refresh_from_db()
        return Response(QuoteDetailSerializer(quote).data)


class QuoteLineItemViewSet(CompanyScopedViewSet):
    queryset = QuoteLineItem.objects.select_related("quote", "variant")
    serializer_class = QuoteLineItemSerializer
    filterset_fields = ["quote", "is_optional", "is_alternative"]
    company_field = None
    parent_links = [("quote", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "quote__company"
        )


class OrderViewSet(CompanyScopedViewSet):
    queryset = Order.objects.select_related("customer", "company").prefetch_related("line_items", "sub_orders")
    filterset_fields = [
        "status", "payment_status", "fulfillment_status",
        "order_type", "company", "customer", "sales_rep",
        "stripe_payment_intent_id",
    ]
    search_fields = ["order_number", "customer__email", "external_id"]
    ordering_fields = ["order_date", "grand_total"]

    def get_serializer_class(self):
        if self.action == "list":
            return OrderListSerializer
        return OrderDetailSerializer

    @extend_schema(
        description=(
            "Create an order. Staff and integrations post the full order "
            "(`OrderDetail`). The storefront identity (guest checkout) posts "
            "`GuestCheckoutRequest`: the server prices each line from the "
            "variant's public price, computes the totals, checks stock without "
            "reserving it, and answers `GuestOrderConfirmation`. A refused "
            "guest checkout answers 400 `GuestCheckoutError` and creates "
            "nothing; branch on `code` (and each line's `code`), never on "
            "`detail`."
        ),
        request=PolymorphicProxySerializer(
            component_name="OrderCreate",
            serializers=[OrderDetailSerializer, GuestCheckoutSerializer],
            resource_type_field_name=None,
        ),
        responses={
            201: PolymorphicProxySerializer(
                component_name="OrderCreateResponse",
                serializers=[OrderDetailSerializer, GuestOrderConfirmationSerializer],
                resource_type_field_name=None,
            ),
            200: OpenApiResponse(
                OrderDetailSerializer,
                description=(
                    "An Idempotency-Key or Stripe PaymentIntent already made this order. "
                    "Guest checkout answers its narrow confirmation instead."
                ),
            ),
            409: OpenApiResponse(
                GuestCheckoutErrorSerializer,
                description=(
                    "Guest checkout: this Idempotency-Key already made an order for a "
                    "different email or cart (`code: idempotency_key_reused`)."
                ),
            ),
            400: OpenApiResponse(
                GuestCheckoutErrorSerializer,
                description=(
                    "Guest checkout refused (storefront identity); a missing email "
                    "answers `{\"guest_email\": [...]}`. Staff creates answer DRF's "
                    "field-error envelope."
                ),
            ),
            429: OpenApiResponse(
                description=(
                    "Guest checkout rate limit (per client IP, `GOLDEN_GUEST_ORDER_RATE`); "
                    "nothing was created."
                ),
            ),
            503: OpenApiResponse(
                description=(
                    "Guest checkout unavailable (`code: guest_checkout_unavailable`): "
                    "the instance does not have exactly one Company."
                ),
            ),
        },
    )
    def create(self, request, *args, **kwargs):
        # Idempotent creation (F-033): the Idempotency-Key header carries
        # the dedup key, falling back to body external_id. A blank key
        # skips dedup and always creates (back-compat path) — mirroring
        # payments.charge_with_idempotency. The lookup is company-scoped
        # under select_for_update and held through creation, so retried
        # POSTs get the original order with 200 instead of a duplicate.
        if is_storefront_user(request.user):
            # Guest checkout is anonymous: rate-limit it per client IP, before
            # any lookup or write (replays count too).
            throttle = GuestOrderThrottle()
            if not throttle.allow_request(request, self):
                raise Throttled(wait=throttle.wait())
            # Guest checkout keeps its own key handling (OP #3431): the generic
            # lookup below is company-scoped to a profile the storefront
            # identity doesn't have, so it never found a guest order.
            return self._storefront_guest_create(request)
        body = request.data if hasattr(request.data, "get") else {}
        header_key = (request.headers.get("Idempotency-Key") or "").strip()
        body_key = str(body.get("external_id") or "").strip()
        key = header_key or body_key
        if not key:
            return self._create(request, *args, **kwargs)
        with transaction.atomic():
            existing = (
                scope_to_company(Order.objects.select_for_update(), request)
                .filter(external_id=key)
                .first()
            )
            if existing is not None:
                return Response(self._replay(existing), status=http_status.HTTP_200_OK)
            if header_key and not body_key and isinstance(request.data, dict):
                # Persist the header key so the retried POST finds this order.
                request.data["external_id"] = header_key
            return self._create(request, *args, **kwargs)

    def _create(self, request, *args, **kwargs):
        # Guest checkout (storefront identity) never gets here: create()
        # hands it to _storefront_guest_create before the generic key block.
        return self._create_once_per_intent(request, *args, **kwargs)

    def _create_once_per_intent(self, request, *args, **kwargs):
        """Create, answering a duplicate Stripe PaymentIntent with its order.

        The checkout's own create and a webhook backstop race for the same
        intent, usually without an Idempotency-Key. ``uniq_order_stripe_pi``
        guarantees one order per intent; the loser gets the winner's order
        with 200, whether the winner committed before the loser started (the
        lookup below) or while it was inserting (the IntegrityError). The
        lookup is the fail-closed company-scoped queryset, so another
        company's order is never returned and its conflict still raises.
        The existing order is returned as-is; the rest of the body is not
        compared (an intent pays for exactly one order, whoever posts it).
        """
        body = request.data if hasattr(request.data, "get") else {}
        intent_id = str(body.get("stripe_payment_intent_id") or "").strip()
        if intent_id:
            existing = self._order_for_intent(intent_id)
            if existing is not None:
                return Response(self._replay(existing), status=http_status.HTTP_200_OK)
        try:
            # A savepoint, so the failed INSERT leaves the caller's transaction
            # usable for the recovery SELECT (create() may already be inside
            # the Idempotency-Key block's atomic).
            with transaction.atomic():
                return super().create(request, *args, **kwargs)
        except IntegrityError as err:
            message = str(err)
            on_intent_index = (
                "uniq_order_stripe_pi" in message or "stripe_payment_intent_id" in message
            )
            existing = self._order_for_intent(intent_id) if intent_id and on_intent_index else None
            if existing is None:
                raise
            return Response(self._replay(existing), status=http_status.HTTP_200_OK)

    def _replay(self, order):
        """An already-created order, answered to a repeated POST.

        The storefront identity only ever sees the guest confirmation, never
        the staff serializer (costs, metadata, IP), whichever path replays.
        """
        if is_storefront_user(self.request.user):
            return GuestOrderConfirmationSerializer(order).data
        return self.get_serializer(order).data

    def _order_for_intent(self, intent_id):
        return self.get_queryset().filter(stripe_payment_intent_id=intent_id).first()

    def _storefront_guest_create(self, request):
        """Create a guest order, with its cart lines, under the storefront identity.

        Accepts the storefront checkout shape (email, customer_name,
        company_name, shipping_address, payment_method, po_number, notes,
        coupon_code, ``lines``) and persists a whitelisted order header plus
        one line per posted variant (OP #3426, ``orders.guest_checkout``).
        The server prices every line and computes the totals; client-supplied
        prices, totals, status, discounts, company and customer are never
        read. Lines are checked against stock but never reserve it.

        Header, shipment group and lines land in one transaction, and every
        line is validated before anything is written, so a refused checkout
        creates nothing and burns no order number. Guest orders require
        exactly one Company in the database: with several tenants there is no
        unambiguous owner for an anonymous order, so guest checkout fails
        closed (503) instead of guessing.
        """
        companies = list(Company.objects.all()[:2])
        if len(companies) != 1:
            return Response(
                {
                    "code": "guest_checkout_unavailable",
                    "error": "guest checkout is not available",
                },
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        company = companies[0]

        body = request.data if hasattr(request.data, "get") else {}
        email = body.get("guest_email") or body.get("email") or ""
        email = email.strip() if isinstance(email, str) else ""
        if not email:
            raise DRFValidationError(
                {"guest_email": ["This field is required for guest checkout."]}
            )
        try:
            ref = guest_checkout.idempotency_ref(request.headers.get("Idempotency-Key"))
        except guest_checkout.CheckoutRefused as refused:
            return Response(refused.body, status=http_status.HTTP_400_BAD_REQUEST)
        fingerprint = guest_checkout.checkout_fingerprint(email, body)
        with transaction.atomic():
            if ref:
                # Held until this transaction ends, so a concurrent submit with
                # the same key waits here and then finds this order.
                try:
                    guest_checkout.lock_idempotency_ref(ref)
                except OperationalError as exc:
                    if getattr(getattr(exc, "__cause__", None), "pgcode", None) != "55P03":
                        raise
                    # lock_not_available: the first submit is still running.
                    # 503, not a refusal: the order may be about to exist.
                    return Response(
                        {"code": guest_checkout.CHECKOUT_IN_PROGRESS,
                         "detail": "This checkout is still being placed. Try again shortly."},
                        status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                existing = guest_checkout.order_for_ref(company, ref)
                if existing is not None:
                    if existing.external_id == ref + fingerprint:
                        return Response(GuestOrderConfirmationSerializer(existing).data,
                                        status=http_status.HTTP_200_OK)
                    return Response(guest_checkout.key_reused(),
                                    status=http_status.HTTP_409_CONFLICT)
            return self._storefront_guest_insert(request, company, body, email,
                                                 ref + fingerprint if ref else "")

    def _storefront_guest_insert(self, request, company, body, email, external_id):
        """Price the lines and write the guest order; the caller holds the atomic."""
        try:
            priced = guest_checkout.price_lines(body.get("lines"))
        except guest_checkout.CheckoutRefused as refused:
            return Response(refused.body, status=http_status.HTTP_400_BAD_REQUEST)

        # Only the whitelisted buyer fields, as short strings. A client
        # ``metadata`` object is never read: an anonymous caller must not plant
        # keys golden code owns, or park a large blob on the order.
        metadata = {}
        for key in ("customer_name", "company_name", "shipping_address", "payment_method",
                    "coupon_code"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                metadata[key] = value.strip()[:GUEST_FIELD_MAX_CHARS]

        data = {
            "company": company.pk,
            "order_type": Order.OrderType.SALES,
            "channel": "storefront",
            "external_source": "storefront",
            "guest_email": email,
            "customer_message": body.get("customer_message") or body.get("notes") or "",
            "customer_po_number": body.get("customer_po_number") or body.get("po_number") or "",
            "payment_terms": body.get("payment_method") or "",
            "metadata": metadata,
            "external_id": external_id,
        }
        # The client past the deployment's proxies, not the proxy itself.
        ip_address = client_ip(request)
        if ip_address:
            data["ip_address"] = ip_address

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            # order_placed is emitted below, once the lines and totals exist:
            # durable outbox consumers build their rows at emit time, and the
            # INSERT's own emit would hand them an order with no lines and $0.
            with suppressed():
                self.perform_create(serializer)
            order = serializer.instance
            # The order editor's default: one "Shipment 1" group, model defaults.
            group = ShipmentGroup.objects.create(order=order, name="Shipment 1", sort_order=0)
            # bulk_create, then one recalculation (as convert_quote_to_order):
            # a per-line save() would recalculate the order once per line.
            OrderLineItem.objects.bulk_create([
                OrderLineItem(
                    order=order,
                    shipment_group=group,
                    sort_order=position,
                    variant=line.variant,
                    item_name=guest_checkout.item_name(line.variant),
                    sku=line.variant.sku,
                    quantity=line.quantity,
                    selling_uom=line.variant.selling_uom or "each",
                    unit_price_ex_tax=line.unit_price,
                    unit_price_inc_tax=line.unit_price,
                    list_price=line.variant.retail_price or line.unit_price,
                    line_total_ex_tax=line.line_total,
                    line_total_inc_tax=line.line_total,
                    lead_time_days=line.variant.lead_time_days,
                )
                for position, line in enumerate(priced)
            ])
            order.recalculate_totals()
            emit(signals.order_placed, Order, order, order=order)
        return Response(
            GuestOrderConfirmationSerializer(order).data,
            status=http_status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="duplicate")
    def duplicate(self, request, pk=None):
        """Create a draft copy of this order with a fresh number."""
        new_order = duplicate_order(self.get_object())
        return Response(
            OrderDetailSerializer(new_order).data,
            status=http_status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="recalculate")
    def recalculate(self, request, pk=None):
        """Server-side recalculation of aggregate fields from current line items."""
        order = self.get_object()
        order.recalculate_totals()
        order.refresh_from_db()
        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=["post"], url_path="convert-to-invoice")
    def convert_to_invoice(self, request, pk=None):
        """Get-or-create the SalesInvoice for this order.

        Idempotent: returns the existing invoice (200) if one already
        exists, otherwise creates a draft invoice (201). The `created`
        flag in the response body lets callers distinguish.
        """
        invoice, created = convert_order_to_invoice(self.get_object())
        body = SalesInvoiceSerializer(invoice).data
        body["created"] = created
        return Response(
            body,
            status=http_status.HTTP_201_CREATED if created else http_status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="send-confirmation")
    def send_confirmation(self, request, pk=None):
        """Send order confirmation email. Called by worker after line items are created."""
        from communications.services import send_order_confirmation

        order = self.get_object()
        if order.confirmation_sent_at:
            return Response({"status": "already_sent", "order_number": order.order_number})
        if not order.customer or not order.customer.email:
            return Response({"status": "no_email"}, status=400)

        log = send_order_confirmation(
            order=order,
            recipient_email=order.customer.email,
            triggered_by="auto",
            user=request.user,
        )
        if log and log.status == "sent":
            return Response({"status": "sent", "order_number": order.order_number})
        return Response(
            {"status": "failed", "error": log.error_message if log else "blocked"},
            status=500,
        )

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        """Guarded status transition. Body: ``{"status": "<new_status>"}``.

        Legal moves (``Order.VALID_TRANSITIONS``) apply and return 200;
        illegal moves — including any move out of a terminal state — return
        400 with the pinned field-error envelope (``{"status": [...]}``).
        Derived writers (sub-order roll-up, payment reconcile) and the ERP
        staff form bypass this map deliberately.
        """
        order = self.get_object()
        try:
            order.transition_to(request.data.get("status"))
        except DjangoValidationError as exc:
            raise DRFValidationError({"status": exc.messages})
        order.refresh_from_db()
        return Response(OrderDetailSerializer(order).data)


class OrderLineItemViewSet(CompanyScopedViewSet):
    queryset = OrderLineItem.objects.select_related("order", "variant", "sub_order")
    serializer_class = OrderLineItemSerializer
    filterset_fields = ["order", "fulfillment_status", "sub_order"]
    search_fields = ["sku", "item_name"]
    company_field = None
    parent_links = [("order", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "order__company"
        )


class SubOrderViewSet(CompanyScopedViewSet):
    queryset = SubOrder.objects.select_related("order", "warehouse", "supplier")
    serializer_class = SubOrderSerializer
    filterset_fields = ["order", "status", "is_drop_ship"]
    search_fields = ["sub_order_number", "tracking_number"]
    company_field = None
    parent_links = [("order", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "order__company"
        )

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        """Guarded status transition. Body: ``{"status": "<new_status>"}``.

        Legal moves (``SubOrder.VALID_TRANSITIONS``) apply and return 200 —
        a legal move still rolls up to the parent order via the post_save
        signal, and the terminal-status guard there still holds. Illegal
        moves — including any move out of ``returned``/``cancelled`` —
        return 400 with the pinned field-error envelope
        (``{"status": [...]}``).
        """
        sub_order = self.get_object()
        try:
            sub_order.transition_to(request.data.get("status"))
        except DjangoValidationError as exc:
            raise DRFValidationError({"status": exc.messages})
        sub_order.refresh_from_db()
        return Response(SubOrderSerializer(sub_order).data)


class SalesInvoiceViewSet(CompanyScopedViewSet):
    """Staff-facing CRUD on SalesInvoice. Authenticated."""

    queryset = SalesInvoice.objects.select_related(
        "order", "order__customer", "company"
    )
    serializer_class = SalesInvoiceSerializer
    filterset_fields = ["status", "company", "order", "payment_terms"]
    search_fields = ["invoice_number", "order__order_number"]
    ordering_fields = ["invoice_date", "grand_total", "due_date"]

    def perform_update(self, serializer):
        # A status change out of draft posts the invoice (SalesInvoice.save):
        # a closed fiscal period refuses with a Django ValidationError, which
        # is the caller's error (400 with the period named), not a 500.
        try:
            super().perform_update(serializer)
        except DjangoValidationError as exc:
            raise DRFValidationError({"status": exc.messages})

    def perform_create(self, serializer):
        try:
            super().perform_create(serializer)
        except DjangoValidationError as exc:
            raise DRFValidationError({"status": exc.messages})

    @action(detail=True, methods=["post"], url_path="recalculate")
    def recalculate(self, request, pk=None):
        """Re-snapshot invoice financials from the parent Order. Skips PAID/VOID/CANCELLED."""
        invoice = self.get_object()
        invoice.recalculate_totals()
        invoice.refresh_from_db()
        return Response(SalesInvoiceSerializer(invoice).data)


@api_view(["POST"])
@permission_classes([PrincipalPermission])
def sales_invoice_reconcile(request, token):
    """Reconcile a Stripe payment back to SalesInvoice + Order + Payment.

    Called by the Qwik webhook after verifying the Stripe signature.
    Authenticated via the standard DRF API key middleware (staff-level).
    Idempotent: keyed on `stripe_payment_intent_id` — duplicate webhook
    deliveries won't double-record the Payment row. amount_paid/balance_due
    are recomputed from the captured/settled payment ledger, so partial
    payments accumulate (PARTIAL until balance_due <= 0, then PAID). A PAID
    invoice is never downgraded by a late "processing"/"failed" event.

    Expected body:
        {
            "event": "paid" | "processing" | "failed",
            "amount_cents": int,              # gross amount charged
            "convenience_fee_cents": int,     # our surcharge (0 for ACH)
            "stripe_payment_intent_id": str,
            "stripe_charge_id": str,          # optional, card only
            "method": "card" | "ach",
            "transaction_id": str,            # Stripe session or PI id
        }
    """
    from datetime import date
    from decimal import Decimal

    from django.db import transaction
    from django.db.models import F, Sum

    from payments.models import Payment

    body = request.data
    event = body.get("event")
    if event not in ("paid", "processing", "failed"):
        return Response(
            {"error": "invalid_event", "event": event},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    pi_id = body.get("stripe_payment_intent_id", "")
    if not pi_id:
        return Response(
            {"error": "stripe_payment_intent_id required"},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    try:
        invoice = SalesInvoice.objects.select_related("order").get(payment_token=token)
    except SalesInvoice.DoesNotExist:
        raise Http404("invoice not found")

    amount_cents = int(body.get("amount_cents") or 0)
    fee_cents = int(body.get("convenience_fee_cents") or 0)
    amount = Decimal(amount_cents) / Decimal(100)
    convenience_fee = Decimal(fee_cents) / Decimal(100)
    method = body.get("method") or "card"

    with transaction.atomic():
        order = invoice.order

        # No-downgrade guard: Stripe webhook deliveries can arrive out of
        # order (or twice) — a late "processing"/"failed" must never demote
        # an invoice that already reconciled as PAID.
        if invoice.status == SalesInvoice.Status.PAID and event in (
            "processing",
            "failed",
        ):
            return Response({"ok": True, "status": invoice.status})

        if event == "failed":
            invoice.status = SalesInvoice.Status.OVERDUE
            invoice.save(update_fields=["status", "updated_at"])
            return Response({"ok": True, "status": invoice.status})

        if event == "processing":
            # Idempotency — one Payment row per PI.
            existing = Payment.objects.filter(
                transaction_id=pi_id, payment_type=Payment.PaymentType.PAYMENT
            ).first()
            invoice.status = SalesInvoice.Status.PROCESSING
            invoice.save(update_fields=["status", "updated_at"])
            if not existing:
                Payment.objects.create(
                    order=order,
                    invoice=invoice,
                    payment_method=(
                        Payment.PaymentMethod.ACH
                        if method == "ach"
                        else Payment.PaymentMethod.CREDIT_CARD
                    ),
                    payment_provider=Payment.PaymentProvider.STRIPE,
                    transaction_id=pi_id,
                    amount=amount,
                    net_amount=amount,
                    convenience_fee=convenience_fee,
                    status=Payment.Status.PENDING,
                    payment_date=None,
                    company=invoice.company,
                )
            order.stripe_payment_intent_id = pi_id
            order.save(update_fields=["stripe_payment_intent_id", "updated_at"])
            return Response({"ok": True, "status": invoice.status})

        # event == "paid"
        from django.utils import timezone

        now = timezone.now()
        settled_states = (Payment.Status.CAPTURED, Payment.Status.SETTLED)

        # Idempotent on transaction_id — a duplicate webhook delivery finds
        # the existing row instead of double-recording the payment.
        payment, created = Payment.objects.get_or_create(
            transaction_id=pi_id,
            payment_type=Payment.PaymentType.PAYMENT,
            defaults={
                "order": order,
                "invoice": invoice,
                "payment_method": (
                    Payment.PaymentMethod.ACH
                    if method == "ach"
                    else Payment.PaymentMethod.CREDIT_CARD
                ),
                "payment_provider": Payment.PaymentProvider.STRIPE,
                "amount": amount,
                "net_amount": amount,
                "convenience_fee": convenience_fee,
                "status": Payment.Status.CAPTURED,
                "payment_date": now,
                "settled_date": now,
                "company": invoice.company,
            },
        )
        if not created and payment.status not in settled_states:
            payment.status = Payment.Status.CAPTURED
            payment.settled_date = now
            if not payment.payment_date:
                payment.payment_date = now
            payment.save(
                update_fields=["status", "settled_date", "payment_date", "updated_at"]
            )

        # Recompute from the payment ledger rather than trusting this one
        # event — partial payments accumulate; amount_paid is what actually
        # satisfied the invoice (gross amount minus our convenience fee).
        invoice_applied = invoice.payments.filter(status__in=settled_states).aggregate(
            total=Sum(F("amount") - F("convenience_fee"))
        )["total"] or Decimal("0.00")

        invoice.amount_paid = invoice_applied
        invoice.balance_due = invoice.grand_total - invoice_applied
        if invoice.balance_due <= 0:
            invoice.status = SalesInvoice.Status.PAID
            if not invoice.paid_date:
                invoice.paid_date = date.today()
        else:
            invoice.status = SalesInvoice.Status.PARTIAL
            invoice.paid_date = None
        invoice.save(
            update_fields=[
                "status",
                "amount_paid",
                "balance_due",
                "paid_date",
                "updated_at",
            ]
        )

        order_applied = order.payments.filter(status__in=settled_states).aggregate(
            total=Sum(F("amount") - F("convenience_fee"))
        )["total"] or Decimal("0.00")

        order.amount_paid = order_applied
        order.balance_due = order.grand_total - order_applied
        order.payment_status = (
            Order.PaymentStatus.PAID
            if order.balance_due <= 0
            else Order.PaymentStatus.PARTIAL
        )
        order.stripe_payment_intent_id = pi_id
        if (
            order.payment_status == Order.PaymentStatus.PAID
            and order.status == Order.Status.AWAITING_PAYMENT
        ):
            order.status = Order.Status.AWAITING_FULFILLMENT
        order.save(
            update_fields=[
                "payment_status",
                "amount_paid",
                "balance_due",
                "stripe_payment_intent_id",
                "status",
                "updated_at",
            ]
        )

    return Response({"ok": True, "status": invoice.status})


@api_view(["GET"])
@permission_classes([AllowAny])
def sales_invoice_by_token(request, token):
    """Public read of a SalesInvoice via its payment_token.

    Used by the storefront's /pay/<token> page. Returns the narrow
    SalesInvoicePublicSerializer shape — no internal notes, no staff
    timestamps, no PII beyond what the payer already knows. Returns 410
    (Gone) if payment_token_expires_at is set and past, 404 on unknown
    token, 200 with the invoice otherwise.
    """
    try:
        invoice = SalesInvoice.objects.select_related(
            "order", "order__customer", "company"
        ).prefetch_related("order__line_items").get(payment_token=token)
    except SalesInvoice.DoesNotExist:
        raise Http404("invoice not found")

    if (
        invoice.payment_token_expires_at is not None
        and invoice.payment_token_expires_at < timezone.now()
    ):
        return Response(
            {"error": "link_expired", "expired_at": invoice.payment_token_expires_at},
            status=http_status.HTTP_410_GONE,
        )

    return Response(SalesInvoicePublicSerializer(invoice).data)
