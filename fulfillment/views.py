from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from core.managers import scope_to_company
from core.views import CompanyScopedViewSet

from .models import PickTicket, PickTicketLine
from .serializers import PickTicketLineSerializer, PickTicketSerializer


class PickTicketViewSet(CompanyScopedViewSet):
    queryset = PickTicket.objects.select_related("sub_order", "warehouse").prefetch_related("lines")
    serializer_class = PickTicketSerializer
    filterset_fields = ["status", "warehouse", "assigned_to"]
    search_fields = ["ticket_number"]
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
