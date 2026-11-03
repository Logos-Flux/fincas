from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory

from inventory.models import Warehouse
from orders.forms import _limit_fk_queryset

from .models import POLineItem, PurchaseOrder, Supplier


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = [
            # Identification
            "supplier_code", "company_name", "display_name",
            "supplier_type", "is_active", "is_approved",
            # Primary contact
            "primary_contact_name", "primary_contact_email",
            "primary_contact_phone", "po_email", "accounts_email",
            "website", "dealer_portal_url",
            # Address
            "address_line_1", "address_line_2",
            "city", "state_province", "postal_code", "country",
            # Commercial
            "payment_terms", "payment_method_preferred",
            "currency", "incoterms",
            "default_lead_time_days", "minimum_order_value",
            "free_freight_threshold", "freight_account_number",
            # Tax & credit
            "tax_id", "w9_on_file", "credit_limit",
            # Catalog
            "supports_drop_ship", "drop_ship_fee", "brands_carried",
            # Internal
            "account_manager", "company", "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "brands_carried": forms.Textarea(attrs={"rows": 3}),
        }


class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = [
            # Header (po_number auto-generated on save)
            "po_type", "supplier",
            # Source
            "sub_order", "order", "shipment_group", "customer_name_snapshot",
            "vendor_quote_reference", "vendor_order_number",
            # Supplier contact
            "supplier_contact_name", "supplier_contact_email",
            # Ship-to
            "ship_to_type", "ship_to_address", "ship_to_label", "ship_to_attention",
            # Commercial
            "incoterms", "vendor_payment_terms",
            "currency", "exchange_rate", "price_list",
            # People
            "requested_by", "buyer",
            # Dates
            "requested_delivery_date", "vendor_estimated_ship_date",
            "expected_delivery_date",
            # Totals
            "subtotal", "tax_total", "shipping_cost", "other_charges",
            "discount_amount", "discount_percent", "grand_total",
            # Receiving
            "receiving_status", "receiving_notes",
            # Shipping
            "shipping_method", "carrier", "tracking_number",
            # Payment
            "payment_status", "amount_paid", "balance_due",
            "payment_due_date", "vendor_invoice_number", "vendor_invoice_date",
            # Status
            "status", "status_note",
            # Internal
            "company", "warehouse", "internal_notes", "vendor_notes",
        ]
        widgets = {
            "requested_delivery_date": forms.DateInput(attrs={"type": "date"}),
            "vendor_estimated_ship_date": forms.DateInput(attrs={"type": "date"}),
            "expected_delivery_date": forms.DateInput(attrs={"type": "date"}),
            "payment_due_date": forms.DateInput(attrs={"type": "date"}),
            "vendor_invoice_date": forms.DateInput(attrs={"type": "date"}),
            "ship_to_address": forms.Textarea(attrs={"rows": 3}),
            "status_note": forms.Textarea(attrs={"rows": 3}),
            "internal_notes": forms.Textarea(attrs={"rows": 4}),
            "vendor_notes": forms.Textarea(attrs={"rows": 4}),
            "receiving_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].required = False
        for fk in ("supplier", "sub_order", "order"):
            if fk in self.fields:
                _limit_fk_queryset(self, fk)
        if self.instance is not None and not self.instance._state.adding:
            # The status moves through the action bar's transition select
            # (PurchaseOrder.VALID_TRANSITIONS, ``set_status_*``), never by
            # editing the field: a disabled field keeps the instance's value
            # whatever the POST carries.
            self.fields["status"].disabled = True
            self.fields["status"].help_text = "Change it with the status control in the action bar."


class ReceiveForm(forms.Form):
    """The header of a receipt on the Receive screen (R4.03); the per-line
    quantities are read by the view (``line-<pk>-quantity_received``)."""

    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), required=False, label="Receive into")
    carrier = forms.CharField(max_length=100, required=False, label="Carrier")
    tracking_number = forms.CharField(max_length=255, required=False, label="Tracking number")
    notes = forms.CharField(max_length=2000, required=False, label="Notes",
                            widget=forms.Textarea(attrs={"rows": 2}))
    close_when_complete = forms.BooleanField(
        required=False, initial=False, label="Close the purchase order once everything is received",
    )

    def __init__(self, *args, purchase_order=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.purchase_order = purchase_order
        if purchase_order is not None:
            self.fields["warehouse"].queryset = Warehouse.objects.filter(
                company=purchase_order.company, is_active=True,
            ).order_by("warehouse_code")
            if purchase_order.warehouse_id:
                self.fields["warehouse"].initial = purchase_order.warehouse_id


class POLineItemForm(forms.ModelForm):
    """A purchase-order line in the editor (``erp/document_form.html``).

    The editor renders variant (hidden, picked through the SKU search),
    item name, quantity, unit cost, discount and line total. The other
    model fields keep their defaults: ``purchasing_uom`` is not rendered,
    so a blank one means the model's ``each`` (until 2026-10-17 it was a
    required field nothing posted, and no purchase order could be saved
    from the back office at all, R1.12).
    """

    #: Money fields that mean zero when left blank (see orders.forms).
    ZERO_DEFAULT_FIELDS = frozenset({"discount_percent", "line_total"})

    class Meta:
        model = POLineItem
        fields = [
            "variant", "item_name", "supplier_sku", "our_sku",
            "quantity_ordered", "purchasing_uom",
            "unit_cost", "discount_percent", "line_total",
            # The order line a prefilled row was raised for (R4.07): the link
            # is what locks that line to its shipment (orders.line_locks).
            "order_line_item",
        ]
        widgets = {
            "item_name": forms.Textarea(attrs={"rows": 1}),
            "order_line_item": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance._state.adding:
            self.fields["quantity_ordered"].initial = 1
        self.fields["purchasing_uom"].required = False
        for name in self.ZERO_DEFAULT_FIELDS:
            self.fields[name].required = False
        # Ensure qty and rate are editable
        self.fields["quantity_ordered"].widget.attrs.update({"class": "form-control form-control-sm text-center"})
        self.fields["unit_cost"].widget.attrs.update({"class": "form-control form-control-sm text-end"})
        # Only load the currently-selected variant (not all 1000+): the
        # instance's, the POSTed one (so validation accepts it), or the
        # initial one (a PO prefilled from a shipment group, R1.11: without
        # it the hidden select rendered no option and every prefilled row
        # posted with no variant).
        variant = self.fields["variant"]
        selected = None
        if not self.instance._state.adding and self.instance.variant_id:
            selected = self.instance.variant_id
        elif self.data:
            selected = self.data.get(self.add_prefix("variant")) or None
        else:
            selected = self.initial.get("variant") or None
        if selected:
            variant.queryset = variant.queryset.filter(pk=selected)
        else:
            variant.queryset = variant.queryset.none()
        # Same for the order line: only the one this row carries.
        order_line = self.fields["order_line_item"]
        order_line.required = False
        if not self.instance._state.adding and self.instance.order_line_item_id:
            linked = self.instance.order_line_item_id
        elif self.data:
            linked = self.data.get(self.add_prefix("order_line_item")) or None
        else:
            linked = self.initial.get("order_line_item") or None
        order_line.queryset = order_line.queryset.filter(pk=linked) if linked else order_line.queryset.none()

    def has_changed(self):
        """A row with no product and no name is the blank extra row.

        The page's JS writes ``0.00`` totals into every row, which Django
        would count as data entered; a row the user never filled must stay
        skipped instead of failing with "variant: This field is required".
        """
        if not self.is_bound:
            return False
        if self.data.get(self.add_prefix("DELETE")):
            return super().has_changed()
        if self.instance._state.adding and not any(
            (self.data.get(self.add_prefix(name)) or "").strip()
            for name in ("variant", "item_name", "our_sku")
        ):
            return False
        return super().has_changed()

    def clean_purchasing_uom(self):
        return (self.cleaned_data.get("purchasing_uom") or "").strip() or POLineItem._meta.get_field("purchasing_uom").default

    def clean(self):
        cleaned = super().clean()
        for name in self.ZERO_DEFAULT_FIELDS:
            if cleaned.get(name) is None:
                cleaned[name] = Decimal("0")
        return cleaned


POLineItemFormSet = inlineformset_factory(
    PurchaseOrder, POLineItem,
    form=POLineItemForm,
    extra=1,
    can_delete=True,
)
