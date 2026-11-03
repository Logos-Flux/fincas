from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from core.managers import scope_to_company
from core.views import CompanyScopedViewSet

from .duplicate import duplicate_purchase_order
from .models import POLineItem, POReceipt, PurchaseOrder, Supplier
from .serializers import (
    POLineItemSerializer,
    POReceiptSerializer,
    PurchaseOrderDetailSerializer,
    PurchaseOrderListSerializer,
    SupplierDetailSerializer,
    SupplierListSerializer,
)


class SupplierViewSet(CompanyScopedViewSet):
    queryset = Supplier.objects.all()
    filterset_fields = ["supplier_type", "is_active", "is_approved", "company"]
    search_fields = ["supplier_code", "company_name"]
    ordering_fields = ["company_name", "created_at"]

    def get_serializer_class(self):
        if self.action == "list":
            return SupplierListSerializer
        return SupplierDetailSerializer


class PurchaseOrderViewSet(CompanyScopedViewSet):
    queryset = PurchaseOrder.objects.select_related("supplier", "company").prefetch_related("line_items")
    filterset_fields = ["status", "po_type", "supplier", "company", "receiving_status"]
    search_fields = ["po_number", "supplier__company_name", "tracking_number"]
    ordering_fields = ["po_date", "grand_total"]

    def get_serializer_class(self):
        if self.action == "list":
            return PurchaseOrderListSerializer
        return PurchaseOrderDetailSerializer

    @action(detail=True, methods=["post"], url_path="duplicate")
    def duplicate(self, request, pk=None):
        """Create a draft copy of this PO with a fresh number."""
        new_po = duplicate_purchase_order(self.get_object())
        return Response(
            PurchaseOrderDetailSerializer(new_po).data,
            status=http_status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="recalculate")
    def recalculate(self, request, pk=None):
        """Server-side recalculation of aggregate fields from current line items."""
        po = self.get_object()
        po.recalculate_totals()
        po.refresh_from_db()
        return Response(PurchaseOrderDetailSerializer(po).data)

    @action(detail=True, methods=["post"], url_path="receive")
    def receive(self, request, pk=None):
        """Receive goods against the purchase order (R4.03), through
        ``purchasing.services.receive_purchase_order`` (the back office's
        "Receive" uses the same service). Body::

            {"lines": [{"po_line_item": "<uuid>", "quantity_received": "4"}],
             "warehouse": "<uuid>" (optional), "notes": "", "carrier": "",
             "tracking_number": "", "close_when_complete": false,
             "idempotency_key": "<caller's key>" (optional)}

        Answers 201 with the receipt; a refusal is 400 with the reason.
        """
        from inventory.models import Warehouse

        from .services import ReceiptRefused, receive_purchase_order

        po = self.get_object()
        body = request.data if isinstance(request.data, dict) else {}
        lines = body.get("lines") or []
        if not isinstance(lines, list):
            raise DRFValidationError({"lines": ["A list of {po_line_item, quantity_received}."]})
        quantities = []
        for item in lines:
            if not isinstance(item, dict) or "po_line_item" not in item:
                raise DRFValidationError({"lines": ["Each line needs po_line_item and quantity_received."]})
            try:
                quantities.append((str(item["po_line_item"]), Decimal(str(item.get("quantity_received", 0)))))
            except (InvalidOperation, ValueError):
                raise DRFValidationError({"lines": ["quantity_received must be a number."]})
        warehouse = None
        if body.get("warehouse"):
            warehouse = Warehouse.objects.filter(pk=body["warehouse"]).first()
            if warehouse is None:
                raise DRFValidationError({"warehouse": ["Unknown warehouse."]})
        try:
            receipt = receive_purchase_order(
                po, quantities, warehouse=warehouse,
                received_by=getattr(request.user, "userprofile", None),
                notes=str(body.get("notes") or ""), carrier=str(body.get("carrier") or ""),
                tracking_number=str(body.get("tracking_number") or ""),
                close_when_complete=bool(body.get("close_when_complete")),
                idempotency_key=str(body.get("idempotency_key") or request.headers.get("Idempotency-Key") or "")[:64],
            )
        except ReceiptRefused as exc:
            raise DRFValidationError({"lines": [str(exc)]})
        except DjangoValidationError as exc:
            raise DRFValidationError({"detail": exc.messages})
        return Response(POReceiptSerializer(receipt).data, status=http_status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        """Guarded status transition. Body: ``{"status": "<new_status>"}``.

        Legal moves (``PurchaseOrder.VALID_TRANSITIONS``) apply and return
        200; illegal moves — including any move out of the terminal
        ``closed``/``cancelled`` states — return 400 with the pinned
        field-error envelope (``{"status": [...]}``).
        """
        po = self.get_object()
        try:
            po.transition_to(request.data.get("status"))
        except DjangoValidationError as exc:
            raise DRFValidationError({"status": exc.messages})
        po.refresh_from_db()
        return Response(PurchaseOrderDetailSerializer(po).data)


class POLineItemViewSet(CompanyScopedViewSet):
    queryset = POLineItem.objects.select_related("purchase_order", "variant")
    serializer_class = POLineItemSerializer
    filterset_fields = ["purchase_order", "receiving_status"]
    search_fields = ["our_sku", "supplier_sku"]
    company_field = None
    parent_links = [("purchase_order", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "purchase_order__company"
        )


class POReceiptViewSet(CompanyScopedViewSet):
    queryset = POReceipt.objects.select_related("purchase_order").prefetch_related("lines")
    serializer_class = POReceiptSerializer
    filterset_fields = ["purchase_order", "status", "condition_on_arrival"]
    search_fields = ["receipt_number"]
    company_field = None
    parent_links = [("purchase_order", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "purchase_order__company"
        )
