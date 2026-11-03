import json
import re
import uuid
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, UpdateView

from communications.models import EmailLog
from communications.services import send_order_confirmation
from core import once
from core.html_safety import display_html as safe_display_html
from core.html_safety import dumps_script_safe
from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin
from crm.models import Engagement
from inventory.models import Warehouse

from . import people
from .convert import convert_order_to_invoice, convert_quote_to_order
from .forms import OrderForm, OrderLineItemFormSet, QuoteForm, QuoteLineItemFormSet
from .line_locks import order_line_move_locks  # noqa: F401  (tests import it from here)
from .models import Order, OrderLineItem, Quote, QuoteLineItem, ShipmentGroup, SubOrder


def _ajax_fk_value_label(request, obj, field_name, lookup):
    """Resolve (value, label) for an AJAX-search FK field.

    Priority: saved instance attribute → POSTed pk (failed-validation
    re-render — without this branch, picked customer/address values vanish
    visually on a Create error). Returns ("", "") if neither is available.

    `lookup` is a callable taking the pk and returning a model instance
    (or None) so we can render its __str__ as the search label.
    """
    if obj and getattr(obj, f"{field_name}_id", None):
        return str(getattr(obj, f"{field_name}_id")), str(getattr(obj, field_name))
    posted = request.POST.get(field_name) if request.method == "POST" else ""
    if posted:
        instance = lookup(posted)
        if instance is not None:
            return str(posted), str(instance)
        return str(posted), ""
    return "", ""


def _build_ajax_fk_config(request, obj, initial=None):
    """Build the JSON-serializable config for the FK search widgets.

    Shared by Order and Quote views. On a re-render after invalid POST,
    falls back to the POSTed pk + DB lookup so the user keeps the
    customer/address they picked instead of seeing blank inputs. A
    customer in ``initial`` (a quote started from an engagement) is shown
    the same way.
    """
    from customers.models import Customer, CustomerAddress, CustomerContact

    def _get(model, pk):
        try:
            return model.objects.get(pk=pk)
        except (model.DoesNotExist, ValueError, TypeError, ValidationError):
            return None

    cust_v, cust_l = _ajax_fk_value_label(
        request, obj, "customer", lambda pk: _get(Customer, pk)
    )
    if not cust_v and initial and initial.get("customer"):
        customer = _get(Customer, initial["customer"])
        if customer is not None:
            cust_v, cust_l = str(customer.pk), str(customer)
    bill_v, bill_l = _ajax_fk_value_label(
        request, obj, "billing_address", lambda pk: _get(CustomerAddress, pk)
    )
    ship_v, ship_l = _ajax_fk_value_label(
        request, obj, "shipping_address", lambda pk: _get(CustomerAddress, pk)
    )
    cont_v, cont_l = _ajax_fk_value_label(
        request, obj, "contact", lambda pk: _get(CustomerContact, pk)
    )
    return {
        "customer": {
            "search_url": reverse("erp-customer-search-ajax"),
            "create_modal": "customerModal",
            "placeholder": "Search customers...",
            "value": cust_v,
            "label": cust_l,
        },
        "billing_address": {
            "search_url": reverse("erp-address-search-ajax"),
            "depends_on": "customer",
            "create_modal": "addressModal",
            "placeholder": "Search addresses...",
            "value": bill_v,
            "label": bill_l,
        },
        "shipping_address": {
            "search_url": reverse("erp-address-search-ajax"),
            "depends_on": "customer",
            "create_modal": "addressModal",
            "placeholder": "Search addresses...",
            "value": ship_v,
            "label": ship_l,
        },
        "contact": {
            "search_url": reverse("erp-contact-search-ajax"),
            "depends_on": "customer",
            "create_modal": "contactModal",
            "placeholder": "Search contacts...",
            "value": cont_v,
            "label": cont_l,
        },
    }


# ── Shipment Group Helpers ────────────────────────────────


def build_shipment_groups_context(parent_obj, formset, parent_field):
    """
    Build grouped line-item data for the template.

    Returns (shipment_groups_data, warehouses) where shipment_groups_data is a
    list of dicts: {index, id, name, warehouse_id, forms: [form, ...]}.
    """
    warehouses = Warehouse.objects.filter(is_active=True).order_by("name")

    if parent_obj and parent_obj.pk:
        groups = list(
            ShipmentGroup.objects.filter(**{parent_field: parent_obj})
            .select_related("supplier")
            .order_by("sort_order")
        )
    else:
        groups = []

    # Look up existing POs and PTs for these groups
    existing_pos = {}
    existing_pts = {}
    if groups and parent_field == "order":
        from fulfillment.models import PickTicket
        from purchasing.models import PurchaseOrder
        group_pks = [sg.pk for sg in groups]
        for po in PurchaseOrder.objects.filter(shipment_group_id__in=group_pks).only("pk", "po_number", "shipment_group_id"):
            existing_pos[po.shipment_group_id] = {"pk": str(po.pk), "number": po.po_number}
        for pt in PickTicket.objects.filter(shipment_group_id__in=group_pks).only("pk", "ticket_number", "shipment_group_id"):
            existing_pts[pt.shipment_group_id] = {"pk": str(pt.pk), "number": pt.ticket_number}

    # Build a mapping from group pk → group info
    if groups:
        sg_data = []
        group_pk_to_idx = {}
        for i, sg in enumerate(groups):
            group_pk_to_idx[sg.pk] = i
            sg_entry = {
                "index": i,
                "id": str(sg.pk),
                "name": sg.name,
                "warehouse_id": str(sg.warehouse_id) if sg.warehouse_id else "",
                "fulfillment_type": sg.fulfillment_type,
                "supplier_id": str(sg.supplier_id) if sg.supplier_id else "",
                "supplier_name": str(sg.supplier) if sg.supplier_id else "",
                "forms": [],
            }
            if sg.pk in existing_pos:
                sg_entry["existing_po"] = existing_pos[sg.pk]
            if sg.pk in existing_pts:
                sg_entry["existing_pt"] = existing_pts[sg.pk]
            sg_data.append(sg_entry)

        # Lines that may not change shipment (orders only): the template
        # shows a lock with the reason instead of a drag handle.
        locks = order_line_move_locks(parent_obj) if parent_field == "order" else {}

        # Assign forms to groups and set sg_index (avoids N+1 on shipment_group FK)
        for form in formset:
            lock = locks.get(form.instance.pk)
            form.move_lock = lock["reason"] if lock else ""
            sg_id = None
            if form.instance.pk and form.instance.shipment_group_id:
                sg_id = form.instance.shipment_group_id
            if sg_id and sg_id in group_pk_to_idx:
                form.initial["sg_index"] = group_pk_to_idx[sg_id]
                sg_data[group_pk_to_idx[sg_id]]["forms"].append(form)
            else:
                form.initial["sg_index"] = 0
                # Assign to first group if unassigned
                sg_data[0]["forms"].append(form)
    else:
        # New document or no groups yet — single virtual group
        sg_data = [{
            "index": 0,
            "id": "",
            "name": "Shipment 1",
            "warehouse_id": "",
            "fulfillment_type": "drop_ship",
            "supplier_id": "",
            "supplier_name": "",
            "forms": list(formset),
        }]

    return sg_data, warehouses


#: Shipment groups one POST may carry. The editor's Split Ship adds one at
#: a time; anything beyond this is a crafted request, clamped rather than
#: written row by row.
MAX_SHIPMENT_GROUPS = 50


def _as_uuid(value):
    """The UUID in ``value``, or None for anything that is not one."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _post_int(post_data, key, default):
    """An integer from the POST, or ``default`` when missing or malformed."""
    try:
        return int(post_data.get(key, default))
    except (TypeError, ValueError):
        return default


def _save_shipment_groups(post_data, parent_obj, parent_field, keep_ids=()):
    """
    Read sg_count / sg_{i}_* from POST data, create/update/delete ShipmentGroups.
    Returns dict mapping {sort_index: ShipmentGroup}.

    A posted ``sg_{i}_id`` is honoured only when that group belongs to
    ``parent_obj``. Any other id (another document's group, or garbage) is
    treated as a new group, so a crafted POST can never rename, reorder or
    pull lines into another document's shipment. Groups in ``keep_ids`` are
    never deleted, even when the POST leaves them out: they hold locked lines.
    """
    sg_count = min(max(_post_int(post_data, "sg_count", 1), 1), MAX_SHIPMENT_GROUPS)
    kept_ids = set(keep_ids)
    result = {}

    for i in range(sg_count):
        sg_id = post_data.get(f"sg_{i}_id", "").strip()
        sg_name = post_data.get(f"sg_{i}_name", f"Shipment {i + 1}").strip() or f"Shipment {i + 1}"
        sg_fulfillment_type = post_data.get(f"sg_{i}_fulfillment_type", "drop_ship").strip()

        # The warehouse and supplier are this document's company's own, or
        # nothing: a UUID from another tenant (or garbage) is dropped, never
        # attached and never a 500.
        warehouse = None
        warehouse_id = _as_uuid(post_data.get(f"sg_{i}_warehouse", "").strip())
        if warehouse_id:
            warehouse = Warehouse.objects.filter(pk=warehouse_id, company_id=parent_obj.company_id).first()

        supplier = None
        supplier_id = _as_uuid(post_data.get(f"sg_{i}_supplier", "").strip())
        if supplier_id:
            from purchasing.models import Supplier
            supplier = Supplier.objects.filter(pk=supplier_id, company_id=parent_obj.company_id).first()

        existing = None
        sg_uuid = _as_uuid(sg_id) if sg_id else None
        if sg_uuid:
            existing = ShipmentGroup.objects.filter(
                pk=sg_uuid, **{parent_field: parent_obj}
            ).first()
        if existing is not None:
            # Update existing (only ever one of this document's own groups)
            sg = existing
            sg.name = sg_name
            sg.warehouse = warehouse
            sg.fulfillment_type = sg_fulfillment_type
            sg.supplier = supplier
            sg.sort_order = i
            sg.save()
            kept_ids.add(sg.pk)
            result[i] = sg
        else:
            # Create new
            sg = ShipmentGroup.objects.create(
                **{parent_field: parent_obj},
                name=sg_name,
                warehouse=warehouse,
                fulfillment_type=sg_fulfillment_type,
                supplier=supplier,
                sort_order=i,
            )
            kept_ids.add(sg.pk)
            result[i] = sg

    # Delete removed groups — orphan their line items to group 0
    old_groups = ShipmentGroup.objects.filter(**{parent_field: parent_obj}).exclude(pk__in=kept_ids)
    if old_groups.exists() and result:
        first_group = result[0]
        for og in old_groups:
            og.order_line_items.update(shipment_group=first_group)
            og.quote_line_items.update(shipment_group=first_group)
        old_groups.delete()

    return result


def _formset_has_entered_line(formset):
    """True when the document will still have at least one line after save.

    The blank extra row must stay valid at the formset level (see
    orders/tests/test_line_item_blank_row.py), so a document with zero
    lines is rejected here, at the view, instead: saving a customerless,
    lineless order/quote is never what the user meant.
    has_changed() already excludes UI-only bookkeeping (sg_index).
    A persisted line kept as-is (not deleted, untouched) also counts —
    otherwise saving an edit without touching line items would be
    rejected even though the document keeps its lines. Membership in
    initial_forms is the proof of persistence: bound initial forms
    resolve their instances from the saved queryset during validation,
    while instance.pk alone proves nothing (UUID-default PKs are
    assigned on construction, even to the blank extra row).
    """
    for li_form in formset.forms:
        if li_form in formset.deleted_forms:
            continue
        if li_form.has_changed():
            return True
        if li_form in formset.initial_forms:
            return True
    return False


def _action_needs_lines(action):
    """Lifecycle actions are not content saves.

    Status transitions (set_status_*) and cancel_order must work even
    when no line data was (re-)entered — e.g. booking or cancelling a
    document whose lines were saved in an earlier visit. Only plain
    saves require entered lines (ISSUE-001: never create a lineless
    document).
    """
    return not (
        action == "cancel_order" or action.startswith("set_status_")
    )


def _posted_line_positions(post_data, prefix):
    """``{form index: position}`` in the order the rows were on screen.

    A browser posts form fields in document order, so the order of the
    ``{prefix}-{i}-sg_index`` keys is the order of the rows after any drag:
    shipment by shipment, top to bottom. Form indexes never change when a
    row moves, so they alone cannot say where it went.
    """
    pattern = re.compile(rf"{re.escape(prefix)}-(\d+)-sg_index")
    positions = {}
    for key in post_data:
        match = pattern.fullmatch(key)
        if match:
            positions.setdefault(int(match.group(1)), len(positions))
    return positions


def _save_with_shipment_groups(view, form, formset, parent_field, line_item_model):
    """Shared save logic for Order/Quote with shipment groups.

    A line's shipment is its row's ``sg_index``: the position of a group in
    the posted ``sg_{i}_*`` list, which ``_save_shipment_groups`` resolves to
    one of this document's own groups. Moving a line (drag and drop, or the
    row's shipment menu) only rewrites that index, so a moved line lands in
    its new group here. Order lines that are locked (``order_line_move_locks``)
    keep the group they had, whatever the POST says.

    Every line's ``sort_order`` becomes its row's position on screen
    (``_posted_line_positions``), so lines dragged up or down within a
    shipment keep that order. A locked line keeps its group, not its place.

    One transaction: the document, its groups, its lines and their places
    land together or not at all (``ATOMIC_REQUESTS`` is off).
    """
    with transaction.atomic():
        # Read the locks before anything is written: they describe the saved state.
        locks = order_line_move_locks(view.object) if parent_field == "order" else {}
        deleted = {f.instance.pk for f in formset.deleted_forms}
        pinned_groups = {
            lock["group_id"] for pk, lock in locks.items()
            if lock["group_id"] and pk not in deleted
        }

        view.object = form.save()
        groups = _save_shipment_groups(
            view.request.POST, view.object, parent_field, keep_ids=pinned_groups,
        )
        formset.instance = view.object
        formset.save()

        # Post-process: assign each saved line item to its shipment group and
        # its place on screen
        prefix = formset.prefix
        positions = _posted_line_positions(view.request.POST, prefix)
        for i, li_form in enumerate(formset):
            if li_form in formset.deleted_forms:
                continue
            if li_form.instance._state.adding:
                continue
            changes = {"sort_order": positions.get(i, len(positions) + i)}
            lock = locks.get(li_form.instance.pk)
            if lock and lock["group_id"]:
                changes["shipment_group_id"] = lock["group_id"]
            else:
                # A groupless locked line (legacy data) takes its posted group
                # like any other: it has no shipment to stay in yet.
                sg_idx = _post_int(view.request.POST, f"{prefix}-{i}-sg_index", 0)
                sg = groups.get(sg_idx, groups.get(0))
                if sg:
                    changes["shipment_group"] = sg
            line_item_model.objects.filter(pk=li_form.instance.pk).update(**changes)


# ── Sales Order List ──────────────────────────────────────

class SalesOrderListView(ERPListView):
    model = Order
    current_section = "orders"
    page_title = "Sales Orders"
    entity_name = "Sales Order"
    create_url_name = "erp-order-create"
    ordering = ["-created_at"]
    search_fields = ["order_number", "customer__first_name", "customer__last_name", "customer__email"]
    list_columns = [
        {"field": "order_number", "label": "Order #", "link": True, "link_url_name": "erp-order-edit"},
        {"field": "order_date", "label": "Date", "date_only": True},
        {"field": "customer", "label": "Customer"},
        {"field": "status", "label": "Status", "badge": True},
        {"field": "payment_status", "label": "Payment", "badge": True},
        {"field": "grand_total", "label": "Total", "money": True, "align": "end"},
    ]
    list_filters = [
        {"field": "status", "label": "Status", "choices": Order.Status.choices},
        {"field": "payment_status", "label": "Payment", "choices": Order.PaymentStatus.choices},
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("customer")


# ── Sales Order Status Transitions ────────────────────────

# Maps current status → list of valid next statuses
ORDER_STATUS_TRANSITIONS = {
    Order.Status.INCOMPLETE: [Order.Status.PENDING, Order.Status.CANCELLED],
    Order.Status.PENDING: [Order.Status.AWAITING_PAYMENT, Order.Status.AWAITING_FULFILLMENT, Order.Status.CANCELLED],
    Order.Status.AWAITING_PAYMENT: [Order.Status.AWAITING_FULFILLMENT, Order.Status.CANCELLED, Order.Status.DECLINED],
    Order.Status.AWAITING_FULFILLMENT: [Order.Status.AWAITING_SHIPMENT, Order.Status.BACKORDERED, Order.Status.ON_HOLD, Order.Status.CANCELLED],
    Order.Status.AWAITING_SHIPMENT: [Order.Status.PARTIALLY_SHIPPED, Order.Status.SHIPPED, Order.Status.ON_HOLD],
    Order.Status.PARTIALLY_SHIPPED: [Order.Status.SHIPPED, Order.Status.ON_HOLD],
    Order.Status.SHIPPED: [Order.Status.COMPLETED],
    Order.Status.COMPLETED: [Order.Status.PARTIALLY_REFUNDED, Order.Status.DISPUTED],
    Order.Status.ON_HOLD: [Order.Status.PENDING, Order.Status.AWAITING_FULFILLMENT, Order.Status.AWAITING_SHIPMENT],
    Order.Status.BACKORDERED: [Order.Status.AWAITING_FULFILLMENT, Order.Status.ON_HOLD, Order.Status.CANCELLED],
    Order.Status.DECLINED: [],
    Order.Status.DISPUTED: [Order.Status.COMPLETED, Order.Status.CANCELLED],
    Order.Status.MANUAL_VERIFICATION_REQUIRED: [Order.Status.PENDING, Order.Status.CANCELLED],
    Order.Status.PARTIALLY_REFUNDED: [],
    Order.Status.CANCELLED: [],
}


# ── Sales Order Create / Edit ─────────────────────────────

class SalesOrderDocMixin(ERPViewMixin):
    model = Order
    form_class = OrderForm
    template_name = "erp/document_form.html"
    current_section = "orders"
    entity_name = "Sales Order"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["line_item_formset"] = OrderLineItemFormSet(self.request.POST, instance=self.object if hasattr(self, "object") else None)
        else:
            context["line_item_formset"] = OrderLineItemFormSet(instance=self.object if hasattr(self, "object") else None)
        context["doc_type_label"] = "Sales Order"
        context["can_cancel"] = True
        context["is_edit"] = hasattr(self, "object") and self.object and self.object.pk
        if context["is_edit"]:
            context["doc_number"] = self.object.order_number
            context["object_pk"] = str(self.object.pk)
            context.update(people_context(self.object, OrderPeopleView.url_name))
        context["header_fields"] = ["order_date", "customer_po_number", "customer", "requested_ship_date", "order_type"]
        context["address_fields"] = ["billing_address", "contact", "shipping_address", "customer_message"]
        context["accounting_fields"] = []
        context["currency_fields"] = []
        context["tax_fields"] = ["tax_rate", "tax_exempt", "tax_exempt_certificate"]
        context["totals_fields"] = ["subtotal_ex_tax", "discount_amount", "discount_percent", "tax_total", "shipping_cost_ex_tax", "handling_fee", "grand_total"]
        context["terms_fields"] = ["payment_terms"]
        context["more_info_sections"] = [
            {"title": "Additional Information", "fields": ["company", "campaign", "lead_source"], "collapsible": False},
            {"title": "Currency & Price List", "fields": ["currency", "exchange_rate", "price_list"], "collapsible": False},
            {"title": "Payment Tracking", "fields": ["payment_status", "amount_paid", "balance_due"], "collapsible": False},
            {"title": "Sales", "fields": ["sales_rep", "commission_rate"], "collapsible": False},
            {"title": "Source", "fields": ["channel", "external_source", "external_id", "quote"], "collapsible": False},
            {"title": "Status Tracking", "fields": ["status", "fulfillment_status", "status_note"], "collapsible": False},
            {"title": "Options", "fields": ["include_standard_terms"], "collapsible": False},
            {"title": "Internal", "fields": ["internal_notes"], "collapsible": False},
        ]
        # Line item field name mapping (Order uses these names)
        context["li_qty_field"] = "quantity"
        context["li_sku_field"] = "sku"
        context["li_rate_field"] = "unit_price_ex_tax"
        context["li_total_field"] = "line_total_ex_tax"
        # Base URLs for Create PO / Create PT buttons (per-group URLs built client-side)
        if context["is_edit"]:
            context["create_po_url"] = reverse("erp-purchaseorder-create")
            context["create_pt_url"] = reverse("erp-pickticket-create")
        # Shipment groups
        context["show_shipment_groups"] = True
        sg_data, wh_list = build_shipment_groups_context(
            self.object if hasattr(self, "object") else None,
            context["line_item_formset"],
            "order",
        )
        context["shipment_groups_data"] = sg_data
        context["warehouses"] = wh_list
        # SubOrder tracking data
        if context["is_edit"]:
            sub_orders = list(
                self.object.sub_orders
                .select_related("order__customer", "purchase_order", "pick_ticket")
                .order_by("sort_order", "created_at")
            )
            context["sub_orders_tracking"] = [
                {
                    "sub_order": so,
                    "has_customer_email": bool(
                        self.object.customer and self.object.customer.email
                    ),
                }
                for so in sub_orders
            ]
        # Pre-load address details for display under dropdowns
        context["address_details_json"] = self._build_address_details_json()
        # Status choices for action bar dropdown (all statuses except current)
        if context["is_edit"]:
            context["status_transitions"] = [
                (s.value, s.label) for s in Order.Status if s.value != self.object.status
            ]
        # AJAX search config for FK fields
        context["ajax_search_fields"] = self._build_ajax_search_config()
        # Last email log for resend modal
        if context["is_edit"]:
            context["last_email_log"] = (
                EmailLog.objects.filter(
                    document_type=EmailLog.DocumentType.SALES_ORDER,
                    document_id=self.object.pk,
                )
                .order_by("-created_at")
                .first()
            )
            context["customer_email"] = (
                self.object.customer.display_email if self.object.customer_id else ""
            )
            context["existing_invoice"] = (
                self.object.invoices.order_by("-invoice_date", "-created_at").first()
            )
        return context

    def _build_ajax_search_config(self):
        """Build JSON config for AJAX-powered FK search fields."""
        obj = self.object if hasattr(self, "object") and self.object else None
        return dumps_script_safe(_build_ajax_fk_config(self.request, obj))

    def _build_address_details_json(self):
        """Build a JSON map of address ID -> display HTML for billing/shipping.

        Values are HTML-escaped (the fragment lands in innerHTML) and the
        JSON itself is script-safe (it is embedded in a <script> block).
        """
        from customers.models import CustomerAddress

        obj = self.object if hasattr(self, "object") and self.object else None
        if not obj or not obj.customer_id:
            return "{}"
        # Single query: all addresses for this customer covers billing + shipping
        addrs = CustomerAddress.objects.filter(customer_id=obj.customer_id).only(
            "id", "first_name", "last_name", "company_name",
            "address_line_1", "address_line_2", "city", "state_province",
            "postal_code", "phone",
        )
        result = {}
        for a in addrs:
            name = " ".join(filter(None, [a.first_name, a.last_name]))
            lines = list(filter(None, [
                name,
                a.company_name,
                a.address_line_1,
                a.address_line_2,
                f"{a.city}, {a.state_province} {a.postal_code}",
                a.phone,
            ]))
            result[str(a.pk)] = safe_display_html(lines)
        return dumps_script_safe(result)

    def form_valid(self, form):
        context = self.get_context_data()
        formset = context["line_item_formset"]
        if formset.is_valid():
            action = self.request.POST.get("_action", "save")
            if _action_needs_lines(action) and not _formset_has_entered_line(formset):
                messages.error(
                    self.request,
                    "Not saved — add at least one line item before saving this order.",
                )
                return self.render_to_response(self.get_context_data(form=form))
            _save_with_shipment_groups(self, form, formset, "order", OrderLineItem)
            if "customer" in form.changed_data:
                people.prune(self.object)
            # Handle status transitions
            if action and action.startswith("set_status_"):
                new_status = action[len("set_status_"):]
                valid_values = [s.value for s in Order.Status]
                if new_status in valid_values:
                    was_pending = self.object.status == Order.Status.PENDING
                    self.object.status = new_status
                    if new_status == Order.Status.AWAITING_FULFILLMENT and not self.object.booked_at:
                        from django.utils import timezone
                        self.object.booked_at = timezone.now()
                    self.object.save(update_fields=["status", "booked_at"])
                    # Auto-send order confirmation on first fulfillment transition
                    if (
                        was_pending
                        and new_status == Order.Status.AWAITING_FULFILLMENT
                        and not self.object.confirmation_sent_at
                        and self.object.customer_id
                        and self.object.customer.email
                    ):
                        send_order_confirmation(
                            order=self.object,
                            recipient_email=self.object.customer.email,
                            triggered_by="auto",
                            user=self.request.user,
                        )
                return redirect(reverse("erp-order-edit", args=[self.object.pk]))
            elif action == "cancel_order":
                self.object.status = Order.Status.CANCELLED
                self.object.save(update_fields=["status"])
                return redirect(reverse("erp-order-edit", args=[self.object.pk]))
            elif action == "create_invoice":
                invoice, created = convert_order_to_invoice(self.object)
                if created:
                    messages.success(
                        self.request,
                        f"Invoice {invoice.invoice_number} created. Use Email Invoice to send the PDF + payment link.",
                    )
                else:
                    messages.info(
                        self.request,
                        f"Invoice {invoice.invoice_number} already exists for this order.",
                    )
                return redirect(reverse("erp-invoice-detail", args=[invoice.pk]))
            elif action == "save_continue":
                return redirect(reverse("erp-order-edit", args=[self.object.pk]))
            return redirect(self.get_success_url())
        # Formset is invalid — surface the errors. Without this banner the
        # page silently re-renders with the user's POST values still in the
        # fields, which looks indistinguishable from a successful save.
        for line_idx, errs in enumerate(formset.errors):
            for field, msgs in errs.items():
                for msg in msgs:
                    messages.error(self.request, f"Line {line_idx + 1} — {field}: {msg}")
        for non_form_err in formset.non_form_errors():
            messages.error(self.request, f"Line items: {non_form_err}")
        if not formset.errors and not formset.non_form_errors():
            messages.error(self.request, "Save failed — line items did not validate.")
        return self.render_to_response(self.get_context_data(form=form))

    def get_success_url(self):
        return reverse("erp-order-list")


class SalesOrderCreateView(SalesOrderDocMixin, CreateView):
    page_title = "New Sales Order"


class SalesOrderUpdateView(SalesOrderDocMixin, UpdateView):
    page_title = "Edit Sales Order"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("customer", "billing_address", "shipping_address", "contact")
            .prefetch_related("shipment_groups__warehouse")
        )


class SendOrderEmailView(LoginRequiredMixin, View):
    """POST-only AJAX endpoint to send/resend order confirmation email."""

    login_url = "/erp/login/"

    def post(self, request, pk):
        try:
            order = (
                Order.objects.select_related("customer", "shipping_address", "billing_address")
                .prefetch_related("line_items__variant")
                .get(pk=pk)
            )
        except Order.DoesNotExist:
            return JsonResponse({"success": False, "error": "Order not found"}, status=404)

        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

        recipient_email = data.get("recipient_email", "").strip()
        if not recipient_email:
            return JsonResponse({"success": False, "error": "Recipient email is required"}, status=400)

        # Warn about old documents unless user confirmed
        from communications.services import is_historical_document
        confirmed = data.get("confirmed_old_date", False)
        if is_historical_document(order) and not confirmed:
            order_date = order.order_date
            return JsonResponse({
                "success": False,
                "confirm_old_date": True,
                "message": f"This order is from {order_date.strftime('%b %d, %Y')}. Are you sure you want to send a confirmation email?",
            })

        # F-033: the modal's _once token is claimed before sending; the same
        # token posted again sends nothing and reads as already sent.
        token = once.token_from(data)
        if not once.claim(request, "send_order", order, token):
            return JsonResponse({
                "success": True,
                "already_sent": True,
                "message": f"Order {order.order_number} was already sent to {recipient_email}.",
            })

        log = send_order_confirmation(
            order=order,
            recipient_email=recipient_email,
            triggered_by="manual",
            user=request.user,
        )

        if log.status != EmailLog.Status.SENT:
            once.release(request, "send_order", order, token)
        if log.status == EmailLog.Status.SENT:
            return JsonResponse({
                "success": True,
                "message": f"Order confirmation sent to {recipient_email}",
            })
        return JsonResponse({
            "success": False,
            "error": f"Failed to send: {log.error_message}",
        })


# ── Quote List ────────────────────────────────────────────

class QuoteListView(ERPListView):
    model = Quote
    current_section = "quotes"
    page_title = "Quotes"
    entity_name = "Quote"
    entity_label_key = "orders.quote"
    page_title_format = "{plural}"
    create_url_name = "erp-quote-create"
    ordering = ["-created_at"]
    search_fields = ["quote_number", "customer__first_name", "customer__last_name"]
    list_columns = [
        {"field": "quote_number", "label": "Quote #", "link": True, "link_url_name": "erp-quote-edit"},
        {"field": "created_at", "label": "Date", "date_only": True},
        {"field": "customer", "label": "Customer"},
        {"field": "engagement", "label": "Engagement", "link": True,
         "link_url_name": "erp-crm-engagement-detail", "link_pk": "engagement_id"},
        {"field": "status", "label": "Status", "badge": True},
        {"field": "engagement__stage__name", "label": "Stage"},
        {"field": "engagement__status", "label": "Deal", "badge": True},
        {"field": "grand_total", "label": "Total", "money": True, "align": "end"},
    ]
    list_filters = [
        {"field": "status", "label": "Status", "choices": Quote.Status.choices},
        {"field": "engagement__status", "label": "Deal", "choices": Engagement.Status.choices},
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "engagement__stage")


# ── Quote Create / Edit ───────────────────────────────────

class QuoteDocMixin(ERPViewMixin):
    model = Quote
    form_class = QuoteForm
    template_name = "erp/document_form.html"
    current_section = "quotes"
    entity_name = "Quote"
    entity_label_key = "orders.quote"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["line_item_formset"] = QuoteLineItemFormSet(self.request.POST, instance=self.object if hasattr(self, "object") else None)
        else:
            context["line_item_formset"] = QuoteLineItemFormSet(instance=self.object if hasattr(self, "object") else None)
        context["doc_type_label"] = "Quote"
        context["can_cancel"] = True
        context["is_edit"] = hasattr(self, "object") and self.object and self.object.pk
        if context["is_edit"]:
            context["doc_number"] = self.object.quote_number
            context.update(people_context(self.object, QuotePeopleView.url_name))
            # Shown in the Email Quote confirm dialog. Prefer the named
            # contact over the customer's default address — a quote usually
            # goes to the person who asked for it.
            context["customer_email"] = (
                (self.object.contact.email if self.object.contact_id else "")
                or (self.object.customer.display_email if self.object.customer_id else "")
            )
        context["header_fields"] = ["customer", "quote_expiration_date", "validity_period_days"]
        if not context["is_edit"]:
            # The deal a new quote joins (R1.06); fixed once the quote exists.
            context["header_fields"].append("engagement")
        context["address_fields"] = ["billing_address", "contact", "shipping_address"]
        context["accounting_fields"] = []
        context["currency_fields"] = []
        context["tax_fields"] = ["tax_rate", "tax_exempt"]
        context["totals_fields"] = ["subtotal_ex_tax", "discount_amount", "tax_total", "shipping_cost_ex_tax", "grand_total"]
        context["terms_fields"] = ["payment_terms_proposed", "shipping_terms_proposed"]
        context["more_info_sections"] = [
            {"title": "Additional Information", "fields": ["company"], "collapsible": False},
            {"title": "Status", "fields": ["status"], "collapsible": False},
            {"title": "Currency & Price List", "fields": ["currency", "exchange_rate", "price_list"], "collapsible": False},
            # Stage, probability and follow-up live on the engagement (spec 1.6).
            {"title": "Sales", "fields": ["sales_rep"], "collapsible": False},
            {"title": "Conditions", "fields": ["special_conditions"], "collapsible": False},
            {"title": "Options", "fields": ["include_standard_terms"], "collapsible": False},
            {"title": "Internal", "fields": ["internal_notes"], "collapsible": False},
        ]
        # Line item field name mapping (Quote uses same names as Order)
        context["li_qty_field"] = "quantity"
        context["li_sku_field"] = "sku"
        context["li_rate_field"] = "unit_price_ex_tax"
        context["li_total_field"] = "line_total_ex_tax"
        # Shipment groups
        context["show_shipment_groups"] = True
        sg_data, wh_list = build_shipment_groups_context(
            self.object if hasattr(self, "object") else None,
            context["line_item_formset"],
            "quote",
        )
        context["shipment_groups_data"] = sg_data
        context["warehouses"] = wh_list
        # AJAX search config for FK fields
        obj = self.object if hasattr(self, "object") and self.object else None
        context["ajax_search_fields"] = dumps_script_safe(
            _build_ajax_fk_config(self.request, obj, initial=self.get_initial() if not obj else None)
        )
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        formset = context["line_item_formset"]
        if formset.is_valid():
            action = self.request.POST.get("_action", "save")
            if _action_needs_lines(action) and not _formset_has_entered_line(formset):
                messages.error(
                    self.request,
                    "Not saved — add at least one line item before saving this quote.",
                )
                return self.render_to_response(self.get_context_data(form=form))
            # A new quote put on an existing deal (R1.06): the form validated
            # the engagement (company, party, not lost); Quote.save() keeps
            # a set engagement and opens a new one otherwise.
            joined = None
            if form.instance._state.adding and form.cleaned_data.get("engagement") is not None:
                joined = form.cleaned_data["engagement"]
                form.instance.engagement = joined
            _save_with_shipment_groups(self, form, formset, "quote", QuoteLineItem)
            if joined is not None:
                from crm.engagements import record_quote_added

                record_quote_added(self.object, by=getattr(self.request.user, "userprofile", None))
            if "customer" in form.changed_data:
                people.prune(self.object)
            if action == "cancel_order":
                self.object.status = Quote.Status.CANCELLED
                self.object.save(update_fields=["status", "updated_at"])
                messages.success(
                    self.request, f"Quote {self.object.quote_number} canceled."
                )
                return redirect(reverse("erp-quote-edit", args=[self.object.pk]))
            if action == "convert_to_order":
                return self._convert_to_order()
            if action == "save_continue":
                return redirect(reverse("erp-quote-edit", args=[self.object.pk]))
            return redirect(self.get_success_url())
        # Surface formset errors — see SalesOrderDocMixin.form_valid for rationale.
        for line_idx, errs in enumerate(formset.errors):
            for field, msgs in errs.items():
                for msg in msgs:
                    messages.error(self.request, f"Line {line_idx + 1} — {field}: {msg}")
        for non_form_err in formset.non_form_errors():
            messages.error(self.request, f"Line items: {non_form_err}")
        if not formset.errors and not formset.non_form_errors():
            messages.error(self.request, "Save failed — line items did not validate.")
        return self.render_to_response(self.get_context_data(form=form))

    def _convert_to_order(self):
        """Turn the saved quote into a Sales Order and go to it.

        Saving first (the caller already did) means the conversion copies
        what is on screen, not a stale row — otherwise edits made in the
        same visit would be silently left behind on the quote.
        """
        profile = getattr(self.request.user, "userprofile", None)
        order, created = convert_quote_to_order(self.object, user=profile)
        if created:
            messages.success(
                self.request,
                f"Sales Order {order.order_number} created from "
                f"{self.object.quote_number}. It is Pending — no confirmation "
                f"email has been sent.",
            )
        else:
            messages.info(
                self.request,
                f"{self.object.quote_number} was already converted to "
                f"{order.order_number}.",
            )
        return redirect(reverse("erp-order-edit", args=[order.pk]))

    def get_success_url(self):
        return reverse("erp-quote-list")


class QuoteCreateView(QuoteDocMixin, CreateView):
    page_title = "New Quote"
    page_title_format = "New {singular}"

    def get_initial(self):
        initial = super().get_initial()
        initial["quote_expiration_date"] = date.today() + timedelta(days=7)
        # "New quote" on an engagement (R1.06): the quote joins that deal and
        # starts with the party's customer when one exists (the deal may
        # still be a lead's party with no customer; the editor adds one).
        engagement = self._engagement_from_query()
        if engagement is not None:
            initial["engagement"] = engagement.pk
            initial["company"] = engagement.company_id
            if engagement.party_id:
                from customers.models import Customer

                customer = (
                    Customer.objects.filter(party_id=engagement.party_id, company_id=engagement.company_id)
                    .order_by("-created_at").first()
                )
                if customer is not None:
                    initial["customer"] = customer.pk
        return initial

    def _engagement_from_query(self):
        pk = (self.request.GET.get("engagement") or "").strip()
        if not pk:
            return None
        try:
            return Engagement.objects.for_request(self.request).select_related("party").get(pk=pk)
        except (Engagement.DoesNotExist, ValueError, ValidationError):
            return None


class QuoteEngagementOptionsView(LoginRequiredMixin, View):
    """GET ?customer=<pk>: the engagements a new quote for that customer may
    join (``crm.engagements.engagements_for_customer``), for the editor's
    Engagement select when the customer changes."""

    login_url = "/erp/login/"

    def get(self, request):
        from customers.models import Customer

        pk = (request.GET.get("customer") or "").strip()
        customer = None
        if pk:
            try:
                customer = Customer.objects.for_request(request).get(pk=pk)
            except (Customer.DoesNotExist, ValueError, ValidationError):
                customer = None
        if customer is None or customer.company_id is None:
            return JsonResponse({"results": []})
        from crm.engagements import engagements_for_customer

        rows = engagements_for_customer(customer.company, customer)
        rows = Engagement.objects.for_request(request).filter(pk__in=rows.values("pk")).select_related("stage")
        rows = sorted(rows, key=lambda e: (e.last_activity_at or e.created_at), reverse=True)
        return JsonResponse({"results": [
            {"id": str(e.pk), "label": f"{e.number} {e.title}".strip() + (f" ({e.stage.name})" if e.stage_id else "")}
            for e in rows
        ]})


class QuoteUpdateView(QuoteDocMixin, UpdateView):
    page_title = "Edit Quote"
    page_title_format = "Edit {singular}"


# ── Shipping Rates AJAX ──────────────────────────────────

class ShippingRatesView(LoginRequiredMixin, View):
    """
    POST /erp/orders/shipping-rates/

    Accepts either:
    - {"order_id": "<uuid>"} to calculate from a saved order
    - {"items": [{"variant_id": "<uuid>", "quantity": N}, ...],
       "ship_to_zip": "01720", "ship_to_state": "MA", "ship_to_city": "Acton",
       "ship_to_address": "123 Main St",
       "warehouse_id": "<uuid>" (optional)}
      to calculate ad-hoc (for unsaved orders/quotes)
    """
    login_url = "/erp/login/"

    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        order_id = data.get("order_id")
        if order_id:
            return self._rates_from_order(order_id, data)
        return self._rates_from_items(data)

    def _rates_from_order(self, order_id, data):
        from inventory.models import Warehouse

        from .shipping_service import get_shipping_rates

        try:
            order = Order.objects.get(pk=order_id)
        except Order.DoesNotExist:
            return JsonResponse({"error": "Order not found"}, status=404)

        warehouse = None
        wh_id = data.get("warehouse_id")
        if wh_id:
            warehouse = Warehouse.objects.filter(pk=wh_id).first()

        result = get_shipping_rates(order, warehouse=warehouse)
        return JsonResponse(result)

    def _rates_from_items(self, data):
        """Ad-hoc rate calculation from variant IDs + destination."""
        from customers.models import CustomerAddress
        from inventory.models import Warehouse
        from products.models import ProductVariant

        from .shipping_service import (
            ShippingItem,
            ShippingRate,
            _f,
            _fetch_ltl_rates,
            _fetch_parcel_rates,
            calculate_shipping_profile,
        )

        items_data = data.get("items", [])
        if not items_data:
            return JsonResponse({"error": "No items provided"}, status=400)

        # Resolve address from address_id if provided
        address_id = data.get("address_id")
        if address_id:
            try:
                addr = CustomerAddress.objects.get(pk=address_id)
                data.setdefault("ship_to_zip", addr.postal_code or "")
                data.setdefault("ship_to_state", addr.state_province or "")
                data.setdefault("ship_to_city", addr.city or "")
                data.setdefault("ship_to_address", addr.address_line_1 or "")
            except CustomerAddress.DoesNotExist:
                pass

        ship_to_zip = data.get("ship_to_zip", "")
        if not ship_to_zip:
            return JsonResponse({"error": "ship_to_zip is required"}, status=400)

        # Resolve warehouse
        warehouse = None
        wh_id = data.get("warehouse_id")
        if wh_id:
            warehouse = Warehouse.objects.filter(pk=wh_id).first()
        if not warehouse:
            warehouse = Warehouse.objects.filter(
                can_fulfill=True, is_active=True,
            ).order_by("-default_for_company").first()

        if not warehouse:
            return JsonResponse({"error": "No fulfillment warehouse configured"}, status=400)

        # Build shipping items from variant IDs
        variant_ids = [i["variant_id"] for i in items_data if i.get("variant_id")]
        variants = {
            str(v.pk): v
            for v in ProductVariant.objects.select_related("product").filter(pk__in=variant_ids)
        }

        shipping_items = []
        for item_data in items_data:
            vid = item_data.get("variant_id")
            variant = variants.get(vid)
            if not variant:
                continue
            product = variant.product
            qty = int(item_data.get("quantity", 1))

            def _flag(vv, pv):
                return vv if vv is not None else pv

            shipping_items.append(ShippingItem(
                sku=variant.sku,
                title=product.title,
                quantity=qty,
                weight_lbs=_f(variant.shipping_weight) or _f(product.shipping_weight),
                length_in=_f(variant.shipping_length) or _f(product.shipping_length),
                width_in=_f(variant.shipping_width) or _f(product.shipping_width),
                height_in=_f(variant.shipping_height) or _f(product.shipping_height),
                ships_usps=_flag(variant.ships_usps, product.ships_usps),
                ships_ups=_flag(variant.ships_ups, product.ships_ups),
                ships_ltl=_flag(variant.ships_ltl, product.ships_ltl),
                ships_pickup=_flag(variant.ships_pickup, product.ships_pickup),
                hazmat_flag=_flag(variant.hazmat_flag, product.hazmat_flag),
                oversized_flag=_flag(variant.oversized_flag, product.oversized_flag),
            ))

        if not shipping_items:
            return JsonResponse({"error": "No valid variants found"}, status=400)

        profile = calculate_shipping_profile(shipping_items)
        warnings = []
        if profile.items_missing_dims:
            warnings.append(f"Missing shipping dims for: {', '.join(profile.items_missing_dims)}")

        from_addr = {
            "street1": warehouse.address_line_1,
            "city": warehouse.city,
            "state": warehouse.state_province,
            "zip": warehouse.postal_code,
            "country": warehouse.country or "US",
        }
        to_addr = {
            "street1": data.get("ship_to_address", ""),
            "city": data.get("ship_to_city", ""),
            "state": data.get("ship_to_state", ""),
            "zip": ship_to_zip,
            "country": "US",
        }

        rates = []

        should_get_parcel = (
            (profile.usps_eligible or profile.ups_eligible)
            and not profile.requires_ltl
            and not profile.has_hazmat
        )
        if should_get_parcel:
            rates.extend(_fetch_parcel_rates(from_addr, to_addr, profile))

        if profile.requires_ltl or profile.ltl_eligible:
            rates.extend(_fetch_ltl_rates(from_addr, to_addr, profile))

        if profile.pickup_available or not rates:
            rates.append(ShippingRate(
                carrier="Will Call",
                service="Customer Pickup",
                rate=0.0,
                source="pickup",
            ))

        rates.sort(key=lambda r: (r.source == "pickup", r.rate))

        return JsonResponse({
            "profile": {
                "total_weight_lbs": round(profile.total_weight_lbs, 1),
                "max_length_in": round(profile.max_length_in, 1),
                "max_width_in": round(profile.max_width_in, 1),
                "total_height_in": round(profile.total_height_in, 1),
                "total_items": profile.total_items,
                "requires_ltl": profile.requires_ltl,
                "has_hazmat": profile.has_hazmat,
                "has_oversized": profile.has_oversized,
                "usps_eligible": profile.usps_eligible,
                "ups_eligible": profile.ups_eligible,
            },
            "ship_from": {
                "name": warehouse.ship_from_label or warehouse.name,
                "city": warehouse.city,
                "state": warehouse.state_province,
                "zip": warehouse.postal_code,
            },
            "rates": [
                {
                    "carrier": r.carrier,
                    "service": r.service,
                    "rate": r.rate,
                    "transit_days": r.transit_days,
                    "delivery_date": r.delivery_date,
                    "guaranteed": r.guaranteed,
                    "source": r.source,
                }
                for r in rates
            ],
            "warnings": warnings,
        })


# ── Shipping Notification Email ───────────────────────────


class SendShippingEmailView(LoginRequiredMixin, View):
    """POST-only AJAX endpoint to send shipping notification for a SubOrder."""

    login_url = "/erp/login/"

    def post(self, request, pk):
        from django.shortcuts import get_object_or_404

        from communications.services import send_shipping_notification

        sub_order = get_object_or_404(
            SubOrder.objects.select_related("order__customer", "order__shipping_address"),
            pk=pk,
        )

        log = send_shipping_notification(
            sub_order=sub_order,
            triggered_by="manual",
            user=request.user,
        )

        if log is None:
            return JsonResponse(
                {"success": False, "error": "No customer email on file"},
                status=400,
            )

        if log.status == EmailLog.Status.SENT:
            return JsonResponse({
                "success": True,
                "message": f"Shipping notification sent to {log.recipient_email}",
            })
        return JsonResponse({
            "success": False,
            "error": f"Failed to send: {log.error_message}",
        })


class UpdateSubOrderTrackingView(LoginRequiredMixin, View):
    """POST-only AJAX endpoint to update tracking info on a SubOrder."""

    login_url = "/erp/login/"

    def post(self, request, pk):
        from django.shortcuts import get_object_or_404

        from communications.services import build_tracking_url

        sub_order = get_object_or_404(SubOrder, pk=pk)

        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

        carrier = data.get("carrier", "").strip()
        tracking_number = data.get("tracking_number", "").strip()

        sub_order.carrier = carrier
        sub_order.tracking_number = tracking_number

        if carrier and carrier != "other" and tracking_number:
            sub_order.tracking_url = build_tracking_url(carrier, tracking_number)
        elif carrier == "other":
            sub_order.tracking_url = data.get("tracking_url", "").strip()
        else:
            sub_order.tracking_url = ""

        sub_order.save(update_fields=["carrier", "tracking_number", "tracking_url"])

        return JsonResponse({
            "success": True,
            "carrier": sub_order.carrier,
            "tracking_number": sub_order.tracking_number,
            "tracking_url": sub_order.tracking_url,
        })


# ── Quote documents (preview / PDF / email) ───────────────
#
# Mirrors the invoice trio in payments/erp_views.py: LoginRequiredMixin and
# no per-object company scoping.
#
# Scoping via Quote.objects.for_request() was tried and reverted. It fails
# CLOSED when it cannot resolve a UserProfile (core/managers.py), and the
# ERP staff UI authenticates by Django session, where request.user is a
# plain auth.User -- UserProfile has no FK to it, so `user.userprofile`
# does not exist and every lookup returned .none(). That 404s the PDF for
# every real staff user. The staff UI is single-tenant in practice and the
# invoice views make the same trade; tightening this belongs in one pass
# across all of /erp/, not smuggled in here.


def _get_quote_for_request(request, pk):
    return get_object_or_404(
        Quote.objects.select_related(
            "customer", "contact", "billing_address", "shipping_address", "company"
        ),
        pk=pk,
    )


class QuoteDetailView(LoginRequiredMixin, View):
    """HTML preview of the quote document — the same render as the PDF."""

    login_url = "/erp/login/"

    def get(self, request, pk):
        from documents.services.pdf import render_quote_html

        return HttpResponse(render_quote_html(_get_quote_for_request(request, pk)))


class QuotePDFView(LoginRequiredMixin, View):
    """Download / open the quote as a PDF."""

    login_url = "/erp/login/"

    def get(self, request, pk):
        from documents.services.pdf import render_quote_pdf

        quote = _get_quote_for_request(request, pk)
        response = HttpResponse(render_quote_pdf(quote), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{quote.quote_number}.pdf"'
        return response


def people_context(document, url_name, error=""):
    """Context for ``erp/_doc_people.html``: the document's people and the
    customer's people not on it yet."""
    on_doc, addable = people.card(document)
    return {
        "doc_people": on_doc,
        "people_candidates": addable,
        "people_url": reverse(url_name, args=[document.pk]),
        "people_error": error,
    }


class DocumentPeopleView(LoginRequiredMixin, View):
    """GET: the People card. POST ``add`` + ``party``, or ``remove`` (a
    DocumentContact id). A fetch request (``X-Requested-With: fetch``) gets
    the re-rendered card back; a plain form post redirects to the editor."""

    login_url = "/erp/login/"
    model = None
    url_name = ""
    edit_url_name = ""

    def _document(self, request, pk):
        return get_object_or_404(self.model.objects.for_request(request).select_related("customer"), pk=pk)

    def _card(self, request, document, error=""):
        from django.shortcuts import render

        return render(request, "erp/_doc_people.html", people_context(document, self.url_name, error))

    def get(self, request, pk):
        return self._card(request, self._document(request, pk))

    def post(self, request, pk):
        from django.core.exceptions import ValidationError

        document = self._document(request, pk)
        error = ""
        try:
            if request.POST.get("remove"):
                people.remove(document, request.POST["remove"])
            elif request.POST.get("party"):
                people.add(document, request.POST["party"])
        except ValidationError as exc:
            error = exc.messages[0] if exc.messages else "That person could not be added."
        if request.headers.get("X-Requested-With") == "fetch":
            return self._card(request, document, error)
        if error:
            messages.error(request, error)
        return redirect(reverse(self.edit_url_name, args=[document.pk]))


class QuotePeopleView(DocumentPeopleView):
    model = Quote
    url_name = "erp-quote-people"
    edit_url_name = "erp-quote-edit"


class OrderPeopleView(DocumentPeopleView):
    model = Order
    url_name = "erp-order-people"
    edit_url_name = "erp-order-edit"


class QuoteReviseView(LoginRequiredMixin, View):
    """POST: the next revision of the quote, on the same deal (R2.06).

    ``orders.revisions.revise_quote`` copies the quote into the chain
    (``original_quote``, ``revision_number`` + 1, the source
    ``superseded_by`` it) and keeps the engagement. The form's ``_once``
    token is claimed first, so a double click makes one revision.
    """

    login_url = "/erp/login/"

    def post(self, request, pk):
        from .revisions import revise_quote

        quote = _get_quote_for_request(request, pk)
        back = reverse("erp-quote-edit", args=[quote.pk])
        if quote.superseded_by_id is not None:
            messages.info(request, f"{quote.quote_number} already has a revision.")
            return redirect(reverse("erp-quote-edit", args=[quote.superseded_by_id]))
        if not once.claim(request, "revise_quote", quote):
            messages.info(request, f"{quote.quote_number} was already revised from this form.")
            return redirect(back)
        try:
            revision = revise_quote(
                quote,
                reason=(request.POST.get("reason") or "").strip(),
                by=getattr(request.user, "userprofile", None),
            )
        except ValidationError as exc:
            once.release(request, "revise_quote", quote)
            messages.error(request, " ".join(exc.messages))
            return redirect(back)
        messages.success(
            request,
            f"Revision {revision.revision_number} of {quote.quote_number} created as "
            f"{revision.quote_number}; {quote.quote_number} is superseded.",
        )
        return redirect(reverse("erp-quote-edit", args=[revision.pk]))


class SendQuoteEmailView(LoginRequiredMixin, View):
    """POST: email the quote PDF to the customer."""

    login_url = "/erp/login/"

    def post(self, request, pk):
        from communications.services import send_quote_email

        quote = _get_quote_for_request(request, pk)
        recipient = (
            request.POST.get("recipient_email")
            or (quote.contact.email if quote.contact_id else "")
            or (quote.customer.email if quote.customer_id else "")
        )
        if not recipient:
            messages.error(request, "No email address on file for this customer.")
            return redirect(reverse("erp-quote-edit", args=[quote.pk]))

        # F-033: the form's _once token is claimed before sending; the same
        # token posted again (double click, retried request) sends nothing.
        if not once.claim(request, "send_quote", quote):
            messages.info(request, f"Quote {quote.quote_number} was already sent from this form.")
            return redirect(reverse("erp-quote-edit", args=[quote.pk]))

        log = send_quote_email(
            quote=quote,
            recipient_email=recipient,
            triggered_by="manual",
            user=request.user,
            message_body=request.POST.get("message_body") or None,
        )
        if log and log.status == EmailLog.Status.SENT:
            messages.success(request, f"Quote {quote.quote_number} emailed to {recipient}.")
        else:
            once.release(request, "send_quote", quote)
            error = log.error_message if log else "send blocked"
            messages.error(request, f"Could not email quote {quote.quote_number}: {error}")
        return redirect(reverse("erp-quote-edit", args=[quote.pk]))
