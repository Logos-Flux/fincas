from django.db import transaction
from rest_framework import status as http_status
from rest_framework.response import Response

from accounting.posting import NoPostingRule
from core.managers import scope_to_company
from core.views import CompanyScopedViewSet

from .models import Payment
from .serializers import PaymentDetailSerializer, PaymentListSerializer
from .services import settle_payment_documents


class PaymentViewSet(CompanyScopedViewSet):
    queryset = Payment.objects.select_related("order")
    filterset_fields = [
        "status", "payment_method", "payment_provider",
        "payment_type", "order", "company",
    ]
    search_fields = ["payment_number", "transaction_id", "order__order_number"]
    ordering_fields = ["payment_date", "amount"]

    def get_serializer_class(self):
        if self.action == "list":
            return PaymentListSerializer
        return PaymentDetailSerializer

    def perform_create(self, serializer):
        # The order (and invoice) are paid by this row: recompute their
        # amount_paid / balance_due / payment status from the settled ledger,
        # as record_payment and the Stripe reconcile do. Before 0.4.4 the API
        # stored the row and left both documents unpaid.
        with transaction.atomic():
            payment = serializer.save()
            settle_payment_documents(payment)

    def perform_update(self, serializer):
        previous_invoice = serializer.instance.invoice if serializer.instance.invoice_id else None
        with transaction.atomic():
            payment = serializer.save()
            moved_off = (
                previous_invoice
                if previous_invoice is not None and previous_invoice.pk != payment.invoice_id
                else None
            )
            settle_payment_documents(payment, previous_invoice=moved_off)

    def update(self, request, *args, **kwargs):
        try:
            return super().update(request, *args, **kwargs)
        except NoPostingRule as exc:
            return Response(
                {"code": "no_posting_rule", "detail": str(exc)},
                status=http_status.HTTP_409_CONFLICT,
            )

    def create(self, request, *args, **kwargs):
        # A settled payment posts to the ledger on save; with no payment
        # DocumentPostingRule that is a refusal the caller can act on (set
        # the rule up), not a 500.
        try:
            return self._create(request, *args, **kwargs)
        except NoPostingRule as exc:
            return Response(
                {"code": "no_posting_rule", "detail": str(exc)},
                status=http_status.HTTP_409_CONFLICT,
            )

    def _create(self, request, *args, **kwargs):
        # Recording a payment twice (F-033): the Idempotency-Key header, or
        # the body's idempotency_key, names the attempt; a retried POST with
        # the same key gets the original Payment with 200 instead of a
        # second row. Mirrors OrderViewSet.create and
        # payments.services.charge_with_idempotency; a blank key always
        # creates (back-compat path).
        body = request.data if hasattr(request.data, "get") else {}
        header_key = (request.headers.get("Idempotency-Key") or "").strip()[:64]
        body_key = str(body.get("idempotency_key") or "").strip()
        key = header_key or body_key
        if not key:
            return super().create(request, *args, **kwargs)
        with transaction.atomic():
            existing = (
                scope_to_company(Payment.objects.select_for_update(), request)
                .filter(idempotency_key=key)
                .first()
            )
            if existing is not None:
                return Response(
                    self.get_serializer(existing).data,
                    status=http_status.HTTP_200_OK,
                )
            if header_key and not body_key and isinstance(request.data, dict):
                request.data["idempotency_key"] = header_key
            return super().create(request, *args, **kwargs)
