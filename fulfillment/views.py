from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from core.managers import scope_to_company
from core.views import CompanyScopedViewSet

from .models import PickTicket, PickTicketLine
from .serializers import PickTicketLineSerializer, PickTicketSerializer
from .services import PickTicketRefused, pick_ticket_for_shipment


class PickTicketViewSet(CompanyScopedViewSet):
    queryset = PickTicket.objects.select_related("sub_order", "warehouse").prefetch_related("lines")
    serializer_class = PickTicketSerializer
    filterset_fields = ["status", "warehouse", "assigned_to", "order", "shipment_group"]
    search_fields = ["ticket_number"]
    # for_shipment writes the ticket and its lines from an order, and answers
    # with the ticket (a new one or the one that exists) and the stock's bins.
    service_action_perms = {
        "transition": "change",
        "for_shipment": (
            "view", "add", "fulfillment.add_pickticketline", "orders.view_order",
            "inventory.view_warehousestock",
        ),
    }
    company_field = None
    parent_links = [("order", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "order__company"
        )

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        """Guarded status transition. Body: ``{"status": "<new_status>"}``.

        Legal moves (``PickTicket.VALID_TRANSITIONS``) apply and return 200;
        illegal moves — including any move out of the terminal ``shipped``
        state — return 400 with the pinned field-error envelope
        (``{"status": [...]}``).
        """
        ticket = self.get_object()
        try:
            ticket.transition_to(request.data.get("status"))
        except DjangoValidationError as exc:
            raise DRFValidationError({"status": exc.messages})
        ticket.refresh_from_db()
        return Response(PickTicketSerializer(ticket).data)

    @action(detail=False, methods=["post"], url_path="for-shipment")
    def for_shipment(self, request):
        """Find or create the pick ticket for one shipment group of an order.

        Body: ``{"order": <uuid>, "shipment_group": <uuid>, "warehouse": <uuid>}``
        (``warehouse`` optional: the group's own, else the first active one).
        A new ticket (201) carries the order's ship-to and one line per order
        line in the group; an existing ticket for the same order, group and
        warehouse is returned as it is (200), never rebuilt.
        """
        from inventory.models import Warehouse
        from orders.models import Order, ShipmentGroup

        if not isinstance(request.data, dict):
            raise DRFValidationError({"detail": "Send a JSON object."})

        def one(queryset, field, label):
            value = request.data.get(field)
            if not value:
                return None
            try:
                found = queryset.filter(pk=value).first()
            except (DjangoValidationError, ValueError):
                found = None
            if found is None:
                raise DRFValidationError({field: [f"{label} not found."]})
            return found

        missing = {
            field: ["This field is required."]
            for field in ("order", "shipment_group") if not request.data.get(field)
        }
        if missing:
            raise DRFValidationError(missing)
        order = one(
            scope_to_company(Order.objects.select_related("shipping_address"), request),
            "order", "Order",
        )
        group = one(ShipmentGroup.objects.filter(order=order), "shipment_group", "Shipment")
        warehouse = one(scope_to_company(Warehouse.objects.all(), request), "warehouse", "Warehouse")

        try:
            ticket, created = pick_ticket_for_shipment(order, group, warehouse)
        except PickTicketRefused as exc:
            raise DRFValidationError({exc.field: [exc.message]})
        return Response(
            PickTicketSerializer(ticket).data,
            status=http_status.HTTP_201_CREATED if created else http_status.HTTP_200_OK,
        )


class PickTicketLineViewSet(CompanyScopedViewSet):
    queryset = PickTicketLine.objects.select_related("pick_ticket", "variant")
    serializer_class = PickTicketLineSerializer
    filterset_fields = ["pick_ticket", "status"]
    search_fields = ["variant__sku"]
    company_field = None
    parent_links = [("pick_ticket", "order__company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "pick_ticket__order__company"
        )
