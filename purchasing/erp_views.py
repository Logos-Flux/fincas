import uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, TemplateView, UpdateView

from accounting.posting import NoPostingRule, PostingError
from core import once
from core.html_safety import dumps_script_safe
from core.models import Company
from core.views.form_view import ERPCreateView, ERPUpdateView
from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin
from orders.models import SubOrder

from .forms import POLineItemFormSet, PurchaseOrderForm, ReceiveForm, SupplierForm
from .models import PurchaseOrder, Supplier
from .services import RECEIVABLE_STATUSES, ReceiptRefused, receive_purchase_order, remaining

# -- Purchase Order List ---------------------------------------------------

class PurchaseOrderListView(ERPListView):
    model = PurchaseOrder
    current_section = "purchase_orders"
    page_title = "Purchase Orders"
    entity_name = "Purchase Order"
    create_url_name = "erp-purchaseorder-create"
    ordering = ["-po_date"]
    search_fields = ["po_number", "supplier__company_name", "vendor_order_number"]
    list_columns = [
        {"field": "po_number", "label": "PO #", "link": True, "link_url_name": "erp-purchaseorder-edit"},
        {"field": "po_date", "label": "Date", "date_only": True},
        {"field": "supplier", "label": "Supplier"},
        {"field": "status", "label": "Status", "badge": True},
        {"field": "grand_total", "label": "Total", "money": True, "align": "end"},
    ]
    list_filters = [
        {"field": "status", "label": "Status", "choices": PurchaseOrder.Status.choices},
        {"field": "payment_status", "label": "Payment", "choices": PurchaseOrder.PaymentStatus.choices},
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")


# -- Purchase Order Create / Edit ------------------------------------------

class PurchaseOrderDocMixin(ERPViewMixin):
    model = PurchaseOrder
    form_class = PurchaseOrderForm
    template_name = "erp/document_form.html"
    current_section = "purchase_orders"
    entity_name = "Purchase Order"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["line_item_formset"] = POLineItemFormSet(
                self.request.POST,
                instance=self.object if hasattr(self, "object") else None,
            )
        else:
            context["line_item_formset"] = POLineItemFormSet(
                instance=self.object if hasattr(self, "object") else None,
            )
        context["doc_type_label"] = "Purchase Order"
        context["can_cancel"] = True
        context["is_edit"] = hasattr(self, "object") and self.object and self.object.pk
        if context["is_edit"]:
            context["doc_number"] = self.object.po_number
            # The action bar's status control offers only the legal moves
            # (PurchaseOrder.VALID_TRANSITIONS); the form's status field is
            # disabled. Until 2026-10-17 the field wrote any status (R4.06).
            labels = dict(PurchaseOrder.Status.choices)
            context["status_transitions"] = [
                (value, labels[value])
                for value in PurchaseOrder.VALID_TRANSITIONS.get(self.object.status, [])
            ]
            context["can_cancel"] = PurchaseOrder.Status.CANCELLED in PurchaseOrder.VALID_TRANSITIONS.get(
                self.object.status, []
            )
            context["can_receive"] = (
                self.object.status in RECEIVABLE_STATUSES and self.object.line_items.exists()
            )
        context["header_fields"] = ["supplier", "po_type", "vendor_quote_reference"]
        context["address_fields"] = [
            "ship_to_type", "ship_to_address", "ship_to_label", "ship_to_attention",
        ]
        context["accounting_fields"] = []
        context["currency_fields"] = []
        context["tax_fields"] = []
        context["totals_fields"] = [
            "subtotal", "discount_amount", "discount_percent",
            "tax_total", "shipping_cost", "other_charges", "grand_total",
        ]
        context["terms_fields"] = ["vendor_payment_terms"]
        # Line item field name mapping (PO uses different names)
        context["li_qty_field"] = "quantity_ordered"
        context["li_rate_field"] = "unit_cost"
        context["li_total_field"] = "line_total"
        context["li_sku_field"] = "our_sku"
        context["more_info_sections"] = [
            {
                "title": "Additional Information",
                "fields": ["company", "warehouse"],
                "collapsible": False,
            },
            {
                "title": "Currency & Price List",
                "fields": ["currency", "exchange_rate", "price_list"],
                "collapsible": False,
            },
            {
                "title": "Payment Tracking",
                "fields": [
                    "payment_status", "amount_paid", "balance_due",
                    "payment_due_date", "vendor_invoice_number", "vendor_invoice_date",
                ],
                "collapsible": False,
            },
            {
                "title": "People",
                "fields": ["requested_by", "buyer"],
                "collapsible": False,
            },
            {
                "title": "Source",
                "fields": ["order", "shipment_group", "sub_order", "customer_name_snapshot", "vendor_order_number"],
                "collapsible": False,
            },
            {
                "title": "Supplier Contact",
                "fields": ["supplier_contact_name", "supplier_contact_email"],
                "collapsible": False,
            },
            {
                "title": "Dates",
                "fields": [
                    "requested_delivery_date", "vendor_estimated_ship_date",
                    "expected_delivery_date",
                ],
                "collapsible": False,
            },
            {
                "title": "Receiving",
                "fields": ["receiving_status", "receiving_notes"],
                "collapsible": False,
            },
            {
                "title": "Shipping",
                "fields": ["shipping_method", "carrier", "tracking_number"],
                "collapsible": False,
            },
            {
                "title": "Status",
                "fields": ["status", "status_note"],
                "collapsible": False,
            },
            {
                "title": "Internal Notes",
                "fields": ["internal_notes", "vendor_notes"],
                "collapsible": False,
            },
        ]
        # SubOrder tracking (if PO is linked to a SubOrder)
        obj = self.object if hasattr(self, "object") and self.object else None
        if obj and obj.sub_order_id:
            try:
                so = (
                    SubOrder.objects
                    .select_related("order__customer")
                    .get(pk=obj.sub_order_id)
                )
                context["po_sub_order_tracking"] = {
                    "sub_order": so,
                    "has_customer_email": bool(
                        so.order and so.order.customer and so.order.customer.email
                    ),
                }
            except SubOrder.DoesNotExist:
                pass

        # AJAX search config for supplier FK
        config = {
            "supplier": {
                "search_url": reverse("erp-supplier-search-ajax"),
                "placeholder": "Search suppliers...",
                "value": str(obj.supplier_id) if obj and obj.supplier_id else "",
                "label": str(obj.supplier) if obj and obj.supplier_id else "",
            },
        }
        context["ajax_search_fields"] = dumps_script_safe(config)
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        formset = context["line_item_formset"]
        if formset.is_valid():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            # Freight and other charges allocate as landed cost per line
            # (F-016); a header-only edit changes no line, so the line
            # signal would not run it.
            self.object.recalculate_totals()
            action = self.request.POST.get("_action", "save")
            if action == "cancel_order":
                action = f"set_status_{PurchaseOrder.Status.CANCELLED}"
            if action.startswith("set_status_"):
                new_status = action[len("set_status_"):]
                try:
                    self.object.transition_to(new_status)
                except ValidationError as exc:
                    messages.error(self.request, f"Status not changed: {' '.join(exc.messages)}")
                else:
                    messages.success(
                        self.request,
                        f"Purchase Order {self.object.po_number} is now {self.object.get_status_display().lower()}.",
                    )
                return redirect(reverse("erp-purchaseorder-edit", args=[self.object.pk]))
            if action == "save_continue":
                return redirect(reverse("erp-purchaseorder-edit", args=[self.object.pk]))
            return redirect(self.get_success_url())
        # The formset is invalid: say so. Until 2026-10-17 the page
        # re-rendered with the posted values and no message, which looked
        # exactly like a successful save (R1.11, R1.12).
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
        return reverse("erp-purchaseorder-list")


class PurchaseOrderCreateView(PurchaseOrderDocMixin, CreateView):
    page_title = "New Purchase Order"

    def get_initial(self):
        initial = super().get_initial()
        company = Company.objects.first()
        if company:
            initial["company"] = company.pk

        order_pk = self.request.GET.get("order")
        if order_pk:
            from orders.models import Order
            try:
                order = Order.objects.select_related(
                    "customer", "shipping_address",
                ).get(pk=order_pk)
                initial["order"] = order.pk
                initial["customer_name_snapshot"] = str(order.customer) if order.customer else ""
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

        supplier_pk = self.request.GET.get("supplier")
        if supplier_pk:
            initial["supplier"] = supplier_pk
            initial["po_type"] = "drop_ship"

        sg_pk = self.request.GET.get("shipment_group")
        if sg_pk:
            initial["shipment_group"] = sg_pk

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

            # A PO line needs a product (POLineItem.variant is NOT NULL): a
            # free-text order line cannot be prefilled, so it is named on the
            # page instead of becoming a row that fails to save (R1.11).
            unlinked = [li for li in lines if not li.variant_id]
            if unlinked:
                names = ", ".join(f"{li.sku or li.item_name}" for li in unlinked)
                messages.warning(
                    self.request,
                    f"Not carried onto the purchase order (no product linked): {names}. "
                    "Add them by hand with a product, or leave them off.",
                )
            lines = [li for li in lines if li.variant_id]
            if lines:
                initial_data = []
                for li in lines:
                    initial_data.append({
                        "variant": li.variant_id,
                        "order_line_item": li.pk,
                        "item_name": li.item_name,
                        "our_sku": li.sku,
                        "quantity_ordered": li.quantity,
                        "unit_cost": li.unit_cost or 0,
                        "line_total": (Decimal(li.quantity or 0) * Decimal(li.unit_cost or 0)).quantize(Decimal("0.01")),
                    })
                # Create a new formset with the initial data
                formset = POLineItemFormSet(initial=initial_data)
                # Set extra to match data count + 1 blank row
                formset.extra = len(initial_data) + 1
                context["line_item_formset"] = formset
        return context


class PurchaseOrderUpdateView(PurchaseOrderDocMixin, UpdateView):
    page_title = "Edit Purchase Order"


class PurchaseOrderReceiveView(ERPViewMixin, TemplateView):
    """Receive goods against a purchase order (R4.03).

    GET: a line per PO line with "received now" defaulting to what is still
    to come, the warehouse, carrier/tracking, notes, and whether to close
    the order once complete. POST: ``purchasing.services.receive_purchase_order``
    (the same service the API's ``receive`` action uses) behind the
    ``_once`` guard, whose token is the receipt's idempotency key.
    """

    template_name = "erp/purchase_order_receive.html"
    current_section = "purchase_orders"
    entity_name = "Purchase Order"

    def purchase_order(self):
        return get_object_or_404(
            PurchaseOrder.objects.for_request(self.request).select_related("supplier", "company", "warehouse"),
            pk=self.kwargs["pk"],
        )

    def rows(self, po, posted=None):
        rows = []
        for line in po.line_items.select_related("variant").order_by("sort_order", "created_at"):
            left = remaining(line)
            value = posted.get(f"line-{line.pk}-quantity_received", "") if posted is not None else None
            rows.append({
                "line": line,
                "remaining": left,
                "field": f"line-{line.pk}-quantity_received",
                "value": value if value is not None else (left if left > 0 else Decimal("0")),
            })
        return rows

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        po = kwargs.get("po") or self.purchase_order()
        context["po"] = po
        context["page_title"] = f"Receive {po.po_number}"
        context["form"] = kwargs.get("form") or ReceiveForm(purchase_order=po)
        context["rows"] = kwargs.get("rows") or self.rows(po)
        context["can_receive"] = po.status in RECEIVABLE_STATUSES and bool(context["rows"])
        context["receipts"] = list(po.receipts.order_by("-received_at", "-created_at").prefetch_related("lines"))
        return context

    def post(self, request, pk):
        po = self.purchase_order()
        back = reverse("erp-purchaseorder-edit", args=[po.pk])
        form = ReceiveForm(request.POST, purchase_order=po)
        rows = self.rows(po, posted=request.POST)
        if not form.is_valid():
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{form.fields[field].label}: {error}")
            return self.render_to_response(self.get_context_data(po=po, form=form, rows=rows))
        quantities = []
        for row in rows:
            raw = (request.POST.get(row["field"]) or "").strip()
            if raw in ("", "0", "0.0", "0.00", "0.000"):
                continue
            try:
                quantities.append((row["line"], Decimal(raw)))
            except InvalidOperation:
                messages.error(request, f"{row['line'].our_sku or row['line'].item_name}: {raw!r} is not a quantity.")
                return self.render_to_response(self.get_context_data(po=po, form=form, rows=rows))
        if not once.claim(request, "receive", po):
            messages.info(request, f"That receipt against {po.po_number} was already recorded from this form.")
            return redirect(back)
        token = once.token_from(request.POST)
        data = form.cleaned_data
        try:
            receipt = receive_purchase_order(
                po, quantities, warehouse=data["warehouse"],
                received_by=getattr(request.user, "userprofile", None),
                notes=data["notes"], carrier=data["carrier"], tracking_number=data["tracking_number"],
                close_when_complete=data["close_when_complete"],
                idempotency_key=f"erp-once:{token}" if token else "",
            )
        except ReceiptRefused as exc:
            once.release(request, "receive", po)
            messages.error(request, f"Not received: {exc}")
            return self.render_to_response(self.get_context_data(po=po, form=form, rows=rows))
        except (NoPostingRule, PostingError, ValidationError) as exc:
            once.release(request, "receive", po)
            reason = " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            messages.error(request, f"Not received: {reason}")
            return self.render_to_response(self.get_context_data(po=po, form=form, rows=rows))
        po.refresh_from_db()
        state = po.get_receiving_status_display().lower()
        messages.success(
            request,
            f"Receipt {receipt.receipt_number} recorded against {po.po_number}: {state}, "
            f"the order is {po.get_status_display().lower()}.",
        )
        return redirect(back)


# -- Supplier List ---------------------------------------------------------

class SupplierListView(ERPListView):
    model = Supplier
    current_section = "suppliers"
    page_title = "Suppliers"
    entity_name = "Supplier"
    create_url_name = "erp-supplier-create"
    ordering = ["company_name"]
    search_fields = ["supplier_code", "company_name", "primary_contact_name", "primary_contact_email"]
    list_columns = [
        {"field": "supplier_code", "label": "Code", "link": True, "link_url_name": "erp-supplier-edit"},
        {"field": "company_name", "label": "Company"},
        {"field": "supplier_type", "label": "Type", "badge": True},
        {"field": "primary_contact_name", "label": "Contact"},
        {"field": "is_active", "label": "Active", "bool_badge": True},
    ]
    list_filters = [
        {"field": "supplier_type", "label": "Type", "choices": Supplier.SupplierType.choices},
    ]


# -- Supplier Create / Edit -----------------------------------------------

class SupplierFormMixin:
    model = Supplier
    form_class = SupplierForm
    current_section = "suppliers"
    entity_name = "Supplier"
    success_url_name = "erp-supplier-list"
    form_sections = [
        {
            "title": "Identification",
            "fields": [
                "supplier_code", "company_name", "display_name",
                "supplier_type", "is_active", "is_approved",
            ],
            "collapsible": False,
        },
        {
            "title": "Primary Contact",
            "fields": [
                "primary_contact_name", "primary_contact_email",
                "primary_contact_phone", "po_email", "accounts_email",
                "website", "dealer_portal_url",
            ],
            "collapsible": False,
        },
        {
            "title": "Address",
            "fields": [
                "address_line_1", "address_line_2",
                "city", "state_province", "postal_code", "country",
            ],
            "collapsible": True,
        },
        {
            "title": "Commercial Terms",
            "fields": [
                "payment_terms", "payment_method_preferred",
                "currency", "incoterms",
                "default_lead_time_days", "minimum_order_value",
                "free_freight_threshold", "freight_account_number",
            ],
            "collapsible": True,
        },
        {
            "title": "Tax & Credit",
            "fields": ["tax_id", "w9_on_file", "credit_limit"],
            "collapsible": True,
        },
        {
            "title": "Catalog & Drop Ship",
            "fields": ["supports_drop_ship", "drop_ship_fee", "brands_carried"],
            "collapsible": True,
        },
        {
            "title": "Internal",
            "fields": ["account_manager", "company", "notes"],
            "collapsible": True,
        },
    ]


class SupplierCreateView(SupplierFormMixin, ERPCreateView):
    page_title = "New Supplier"


class SupplierUpdateView(SupplierFormMixin, ERPUpdateView):
    page_title = "Edit Supplier"


# -- Supplier AJAX endpoints -----------------------------------------------

class SupplierSearchAjaxView(UserPassesTestMixin, View):
    """GET /erp/purchasing/suppliers/search-ajax/?q=..."""

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request):
        q = request.GET.get("q", "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})

        from django.db.models import Q
        # Only suppliers the document's save will keep: a shipment group
        # and a purchase order bind a supplier of their own company
        # (orders.erp_views._save_shipment_groups), so the picker is
        # scoped the same way. The caller may name the document's
        # company (``?company=``) when it differs from the viewer's.
        qs = Supplier.objects.for_request(request).filter(is_active=True)
        company_id = (request.GET.get("company") or "").strip()
        if company_id:
            try:
                qs = qs.filter(company_id=uuid.UUID(company_id))
            except ValueError:
                return JsonResponse({"results": []})
        qs = qs.filter(
            Q(company_name__icontains=q)
            | Q(display_name__icontains=q)
            | Q(supplier_code__icontains=q)
        ).order_by("company_name")[:15]
        results = [
            {
                "id": str(s.pk),
                "label": str(s),
                "code": s.supplier_code,
                "supplier_type": s.supplier_type,
            }
            for s in qs
        ]
        return JsonResponse({"results": results})


class SupplierShipFromView(UserPassesTestMixin, View):
    """GET /erp/purchasing/suppliers/<uuid:pk>/ship-from-locations/"""

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request, pk):
        supplier = get_object_or_404(Supplier, pk=pk)
        locations = []
        # Default ship-from address (if set)
        if supplier.default_ship_from_address:
            a = supplier.default_ship_from_address
            locations.append({
                "id": f"addr-{a.pk}",
                "label": f"{a.address_line_1}, {a.city}, {a.state_province} {a.postal_code}",
                "default": True,
            })
        # Supplier's own address as fallback
        if supplier.address_line_1:
            locations.append({
                "id": "supplier-address",
                "label": f"{supplier.address_line_1}, {supplier.city}, {supplier.state_province} {supplier.postal_code}",
                "default": not supplier.default_ship_from_address,
            })
        # Additional locations from JSONField
        for loc in (supplier.ship_from_locations or []):
            locations.append({
                "id": loc.get("id", ""),
                "label": loc.get("label", ""),
                "default": False,
            })
        return JsonResponse({"locations": locations})
