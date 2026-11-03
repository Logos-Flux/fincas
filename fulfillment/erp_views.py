import json

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, UpdateView

from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin

from .forms import PickTicketForm, PickTicketLineFormSet
from .models import PickTicket, PickTicketLine

# -- Pick Ticket List ------------------------------------------------------

class PickTicketListView(ERPListView):
    model = PickTicket
    current_section = "pick_tickets"
    page_title = "Pick Tickets"
    entity_name = "Pick Ticket"
    create_url_name = "erp-pickticket-create"
    ordering = ["-created_at"]
    search_fields = ["ticket_number"]
    list_columns = [
        {"field": "ticket_number", "label": "Ticket #", "link": True, "link_url_name": "erp-pickticket-edit"},
        {"field": "order", "label": "Order"},
        {"field": "warehouse", "label": "Warehouse"},
        {"field": "status", "label": "Status", "badge": True},
        {"field": "assigned_to", "label": "Assigned To"},
    ]
    list_filters = [
        {"field": "status", "label": "Status", "choices": PickTicket.Status.choices},
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("order", "warehouse", "assigned_to")


# -- Pick Ticket Create / Edit ---------------------------------------------

class PickTicketDocMixin(ERPViewMixin):
    model = PickTicket
    form_class = PickTicketForm
    template_name = "erp/document_form.html"
    current_section = "pick_tickets"
    entity_name = "Pick Ticket"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["line_item_formset"] = PickTicketLineFormSet(
                self.request.POST,
                instance=self.object if hasattr(self, "object") else None,
            )
        else:
            context["line_item_formset"] = PickTicketLineFormSet(
                instance=self.object if hasattr(self, "object") else None,
            )
        context["doc_type_label"] = "Pick Ticket"
        context["is_edit"] = hasattr(self, "object") and self.object and self.object.pk
        if context["is_edit"]:
            context["doc_number"] = self.object.ticket_number
        context["header_fields"] = ["warehouse", "status", "assigned_to"]
        context["address_fields"] = [
            "ship_to_address", "ship_to_label", "ship_to_attention",
        ]
        context["accounting_fields"] = []
        context["currency_fields"] = []
        context["tax_fields"] = []
        context["totals_fields"] = []
        context["terms_fields"] = []
        # Line item field name mapping
        context["li_qty_field"] = "quantity"
        context["li_sku_field"] = "sku"
        context["li_rate_field"] = "unit_cost"
        context["li_total_field"] = "line_total"
        context["more_info_sections"] = [
            {
                "title": "Source",
                "fields": ["order", "shipment_group", "sub_order"],
                "collapsible": False,
            },
            {
                "title": "Notes",
                "fields": ["notes"],
                "collapsible": False,
            },
        ]

        # SubOrder tracking (if PickTicket is linked to a SubOrder)
        obj = self.object if hasattr(self, "object") and self.object else None
        if obj and obj.sub_order_id:
            from orders.models import SubOrder
            try:
                so = (
                    SubOrder.objects
                    .select_related("order__customer")
                    .get(pk=obj.sub_order_id)
                )
                context["pt_sub_order_tracking"] = {
                    "sub_order": so,
                    "has_customer_email": bool(
                        so.order and so.order.customer and so.order.customer.email
                    ),
                }
            except SubOrder.DoesNotExist:
                pass

        return context

    def form_valid(self, form):
        context = self.get_context_data()
        formset = context["line_item_formset"]
        if formset.is_valid():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            action = self.request.POST.get("_action", "save")
            if action == "save_continue":
                return redirect(reverse("erp-pickticket-edit", args=[self.object.pk]))
            return redirect(self.get_success_url())
        return self.render_to_response(self.get_context_data(form=form))

    def get_success_url(self):
        return reverse("erp-pickticket-list")


class PickTicketCreateView(PickTicketDocMixin, CreateView):
    page_title = "New Pick Ticket"

    def get_initial(self):
        initial = super().get_initial()
        # Pre-fill from URL params (?order=&shipment_group=&warehouse=)
        order_pk = self.request.GET.get("order")
        if order_pk:
            from orders.models import Order
            try:
                order = Order.objects.select_related(
                    "shipping_address",
                ).get(pk=order_pk)
                initial["order"] = order.pk
                # Copy ship-to from order's shipping address
                if order.shipping_address:
                    a = order.shipping_address
                    initial["ship_to_label"] = a.label or ""
                    initial["ship_to_attention"] = " ".join(
                        filter(None, [a.first_name, a.last_name])
                    )
                    initial["ship_to_address"] = "\n".join(filter(None, [
                        a.company_name,
                        a.address_line_1,
                        a.address_line_2,
                        f"{a.city}, {a.state_province} {a.postal_code}",
                    ]))
            except Order.DoesNotExist:
                pass

        sg_pk = self.request.GET.get("shipment_group")
        if sg_pk:
            initial["shipment_group"] = sg_pk

        wh_pk = self.request.GET.get("warehouse")
        if wh_pk:
            initial["warehouse"] = wh_pk
        else:
            from inventory.models import Warehouse
            default_wh = Warehouse.objects.filter(is_active=True).first()
            if default_wh:
                initial["warehouse"] = default_wh.pk

        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sg_pk = self.request.GET.get("shipment_group")
        order_pk = self.request.GET.get("order")
        if sg_pk and order_pk and not self.request.POST:
            from orders.models import OrderLineItem
            lines = list(OrderLineItem.objects.filter(
                order_id=order_pk, shipment_group_id=sg_pk,
            ).select_related("variant"))

            if lines:
                initial_data = []
                for li in lines:
                    initial_data.append({
                        "order_line_item": li.pk,
                        "variant": li.variant_id,
                        "item_name": li.item_name,
                        "sku": li.sku,
                        "quantity": li.quantity,
                        "unit_cost": li.unit_cost or 0,
                    })
                formset = PickTicketLineFormSet(initial=initial_data)
                formset.extra = len(initial_data) + 1
                context["line_item_formset"] = formset
        return context


class PickTicketUpdateView(PickTicketDocMixin, UpdateView):
    page_title = "Edit Pick Ticket"


# -- AJAX Create + Print ---------------------------------------------------

class CreatePickTicketAjaxView(LoginRequiredMixin, View):
    """POST-only: find-or-create a pick ticket and return its print URL."""

    login_url = "/erp/login/"

    def post(self, request):
        try:
            return self._handle(request)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return JsonResponse({"success": False, "error": str(exc)}, status=500)

    def _handle(self, request):
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

        order_id = data.get("order")
        sg_id = data.get("shipment_group")
        wh_id = data.get("warehouse")

        if not all([order_id, sg_id]):
            return JsonResponse(
                {"success": False, "error": "order and shipment_group are required"},
                status=400,
            )

        from inventory.models import WarehouseStock
        from orders.models import Order, OrderLineItem

        try:
            order = Order.objects.select_related("shipping_address").get(pk=order_id)
        except Order.DoesNotExist:
            return JsonResponse({"success": False, "error": "Order not found"}, status=404)

        # If no warehouse provided, try to find a default
        if not wh_id:
            from inventory.models import Warehouse as WH
            default_wh = WH.objects.filter(is_active=True).first()
            if default_wh:
                wh_id = default_wh.pk

        # Find existing pick ticket for this combination
        filter_kwargs = {"order_id": order_id, "shipment_group_id": sg_id}
        if wh_id:
            filter_kwargs["warehouse_id"] = wh_id
        existing = PickTicket.objects.filter(**filter_kwargs).first()

        if existing:
            return JsonResponse({
                "success": True,
                "print_url": reverse("erp-pickticket-print", args=[existing.pk]),
            })

        # Build ship-to from order's shipping address
        ship_to_address = ""
        ship_to_label = ""
        ship_to_attention = ""
        if order.shipping_address:
            a = order.shipping_address
            ship_to_label = a.label or ""
            ship_to_attention = " ".join(filter(None, [a.first_name, a.last_name]))
            ship_to_address = "\n".join(filter(None, [
                a.company_name,
                a.address_line_1,
                a.address_line_2,
                f"{a.city}, {a.state_province} {a.postal_code}",
            ]))

        pt = PickTicket.objects.create(
            order_id=order_id,
            shipment_group_id=sg_id,
            warehouse_id=wh_id or None,
            ship_to_address=ship_to_address,
            ship_to_label=ship_to_label,
            ship_to_attention=ship_to_attention,
        )

        # Create line items from order lines in this shipment group
        order_lines = list(
            OrderLineItem.objects.filter(
                order_id=order_id, shipment_group_id=sg_id,
            ).select_related("variant")
        )

        # Look up bin locations for all variants in this warehouse
        variant_ids = [li.variant_id for li in order_lines if li.variant_id]
        bin_map = {}
        if variant_ids and wh_id:
            bin_map = dict(
                WarehouseStock.objects.filter(
                    warehouse_id=wh_id, variant_id__in=variant_ids,
                ).values_list("variant_id", "bin_location")
            )

        pick_lines = []
        for i, li in enumerate(order_lines):
            pick_lines.append(PickTicketLine(
                pick_ticket=pt,
                order_line_item=li,
                variant=li.variant,
                item_name=li.item_name,
                sku=li.sku,
                quantity=li.quantity,
                unit_cost=li.unit_cost or 0,
                bin_location=bin_map.get(li.variant_id, ""),
                sort_order=i,
            ))
        if pick_lines:
            PickTicketLine.objects.bulk_create(pick_lines)

        return JsonResponse({
            "success": True,
            "print_url": reverse("erp-pickticket-print", args=[pt.pk]),
        })


class PickTicketPrintView(LoginRequiredMixin, DetailView):
    """Render a printable pick ticket."""

    login_url = "/erp/login/"
    model = PickTicket
    template_name = "erp/pick_ticket_print.html"

    def get_queryset(self):
        return PickTicket.objects.select_related(
            "order__customer", "order__shipping_address", "warehouse",
        ).prefetch_related("lines__variant")
