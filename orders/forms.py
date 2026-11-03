from decimal import Decimal

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, inlineformset_factory

from core.models import Company

from .models import Order, OrderLineItem, Quote, QuoteLineItem


def _default_company(form):
    """Pre-fill `company` for new Orders/Quotes.

    Prefers the row named by the ``GOLDEN_DEFAULT_COMPANY_NAME`` setting
    (the deployment's selling entity; matched case-insensitively), falling
    back to the only Company row on single-tenant installs. The field is
    required by the model but only rendered on the "More Info" tab; without
    a default, hitting Save on the visible Details tab silently fails
    validation.

    BaseModel auto-assigns `instance.pk` from a UUID4 default, so we use
    `_state.adding` to detect new-vs-existing.
    """
    if "company" not in form.fields or form.is_bound:
        return
    if form.instance and not form.instance._state.adding:
        return
    if form.initial.get("company"):
        return
    name = getattr(settings, "GOLDEN_DEFAULT_COMPANY_NAME", None)
    preferred = (
        Company.objects.filter(name__iexact=name).values_list("pk", flat=True).first()
        if name
        else None
    )
    if preferred:
        form.initial["company"] = preferred
        return
    pks = list(Company.objects.values_list("pk", flat=True)[:2])
    if len(pks) == 1:
        form.initial["company"] = pks[0]

PAYMENT_TERMS_CHOICES = [
    ("", "— Select —"),
    ("Prepaid", "Prepaid"),
    ("Due on Receipt", "Due on Receipt"),
    ("Net 15", "Net 15"),
    ("Net 30", "Net 30"),
    ("Net 45", "Net 45"),
    ("Net 60", "Net 60"),
    ("2/10 Net 30", "2/10 Net 30"),
    ("COD", "COD"),
]

SHIPPING_TERMS_CHOICES = [
    ("", "— Select —"),
    ("FOB Origin", "FOB Origin"),
    ("FOB Destination", "FOB Destination"),
    ("Prepaid", "Prepaid"),
    ("Prepaid & Add", "Prepaid & Add"),
    ("Collect", "Collect"),
    ("Free Shipping", "Free Shipping"),
    ("Flat Rate", "Flat Rate"),
    ("Customer Account", "Customer Account"),
]


def _limit_fk_queryset(form, field_name):
    """Limit FK select to only the currently-selected value (or none).

    On POST we MUST honor the submitted value — the search widgets pick a
    customer/address client-side and the chosen pk only exists in form.data.
    Without this, ModelChoiceField rejects with "not one of the available
    choices" because the queryset was scoped to .none() before validation
    saw the POSTed pk. Mirrors the variant fix in OrderLineItemForm.
    """
    qs = form.fields[field_name].queryset
    submitted = form.data.get(form.add_prefix(field_name)) if form.is_bound else None
    val = (
        submitted
        or form.initial.get(field_name)
        or (
            getattr(form.instance, f"{field_name}_id", None)
            if form.instance and form.instance.pk
            else None
        )
    )
    if val:
        form.fields[field_name].queryset = qs.filter(pk=val)
    else:
        form.fields[field_name].queryset = qs.none()


class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = [
            # Header
            "order_date", "customer_po_number", "customer",
            "requested_ship_date", "order_type",
            # Address
            "billing_address", "shipping_address", "contact", "customer_message",
            # Accounting
            "company", "campaign", "lead_source",
            # Currency
            "currency", "exchange_rate", "price_list",
            # Tax
            "tax_rate", "tax_exempt", "tax_exempt_certificate",
            # Totals
            "subtotal_ex_tax", "discount_amount", "discount_percent",
            "tax_total", "shipping_cost_ex_tax", "handling_fee", "grand_total",
            # Payment & terms
            "payment_terms", "include_standard_terms",
            "payment_status", "amount_paid", "balance_due",
            # More Info (Tab 3)
            "sales_rep", "commission_rate",
            "channel", "external_source", "external_id", "quote",
            "status", "fulfillment_status", "status_note",
            "internal_notes",
        ]
        widgets = {
            "order_date": forms.DateTimeInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "requested_ship_date": forms.DateInput(attrs={"type": "date"}),
            "customer_message": forms.Textarea(attrs={"rows": 3}),
            "status_note": forms.Textarea(attrs={"rows": 3}),
            "internal_notes": forms.Textarea(attrs={"rows": 4}),
            "payment_terms": forms.Select(choices=PAYMENT_TERMS_CHOICES),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _default_company(self)
        # Limit heavy FK querysets to only the selected value — search handles the rest
        for fk in ("customer", "billing_address", "shipping_address", "contact"):
            if fk in self.fields:
                _limit_fk_queryset(self, fk)


class ShipmentGroupIndexMixin:
    """Keep the UI-only `sg_index` field out of the has_changed() decision.

    `extra=1` renders a trailing blank line-item row. Django skips
    validating an extra form whose has_changed() is False, which is what
    lets that blank row exist without demanding a product.

    But renumberAllGroups() in erp/document_form.html stamps EVERY row's
    sg_index with its shipment-group index. On a document with two or more
    groups, the untouched blank row sitting in group 1+ gets its sg_index
    rewritten 0 -> 1, has_changed() flips True, and Django then demands
    variant/sku/item_name/quantity/unit_price on a phantom row the user
    never touched. It surfaces as "Line N — variant: This field is
    required." and cannot be cleared by filling anything in, because
    __init__ pins that row's variant queryset to .none(). The only escape
    was deleting the row, which is not discoverable.

    sg_index is bookkeeping for which shipment group a row belongs to. It
    is never evidence that a human entered line data, so it must not make
    an otherwise-empty row look filled in. Every other field still counts,
    so a genuinely half-filled row keeps raising its real errors.
    """

    #: Fields that carry no user intent and cannot mark a row as touched.
    UI_ONLY_FIELDS = frozenset({"sg_index"})

    #: Money fields that mean zero when left blank.
    #:
    #: These carry `default=0` on the model but not `blank=True`, so Django
    #: built them as REQUIRED form fields and an empty box produced "This
    #: field is required." That is wrong for both: a line with no discount
    #: is the normal case, and line_total_ex_tax is computed by the page's
    #: JS rather than typed. Blank now means 0.
    #:
    #: quantity and unit_price_ex_tax are deliberately NOT here. They have
    #: no model default and no sensible zero — silently pricing a line at
    #: $0.00 because someone tabbed past the box is worse than an error.
    ZERO_DEFAULT_FIELDS = frozenset({"discount_percent", "line_total_ex_tax"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.ZERO_DEFAULT_FIELDS:
            if name in self.fields:
                self.fields[name].required = False

    #: The line's FK to its document ("order" / "quote"), set by each form.
    PARENT_FIELD = None

    def has_changed(self):
        return bool(set(self.changed_data) - self.UI_ONLY_FIELDS)

    def clean_shipment_group(self):
        """Drop a posted group that belongs to another document.

        The hidden field's queryset is every ShipmentGroup, so on its own it
        would accept any document's group id. The view assigns the real group
        from sg_index after saving; this keeps a crafted id from ever being
        written in between (or kept, when no groups are posted).
        """
        group = self.cleaned_data.get("shipment_group")
        if group is None or self.PARENT_FIELD is None:
            return group
        parent_id = getattr(self.instance, f"{self.PARENT_FIELD}_id", None)
        if parent_id is None or getattr(group, f"{self.PARENT_FIELD}_id") != parent_id:
            return None
        return group

    def clean(self):
        cleaned = super().clean()
        # required=False turns an empty box into None, which would hit a NOT
        # NULL column — the model default only applies when the attribute is
        # never set, and a ModelForm always sets it. Coerce here instead.
        for name in self.ZERO_DEFAULT_FIELDS:
            if name in self.fields and cleaned.get(name) is None:
                cleaned[name] = Decimal("0")
        return cleaned


class OrderLineItemForm(ShipmentGroupIndexMixin, forms.ModelForm):
    PARENT_FIELD = "order"
    sg_index = forms.IntegerField(widget=forms.HiddenInput, required=False, initial=0)

    class Meta:
        model = OrderLineItem
        fields = [
            "variant", "sku", "item_name", "quantity",
            "unit_price_ex_tax", "discount_percent", "line_total_ex_tax",
            "shipment_group",
        ]
        # sub_order is fulfillment's, never the editor's: it used to sit in
        # ``fields`` without being rendered, so every save posted it blank and
        # detached each line from its sub-order (review 2026-10-17).
        widgets = {
            "shipment_group": forms.HiddenInput,
            "item_name": forms.Textarea(attrs={"rows": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # (A `quantity` default of 1 for new rows used to sit here behind
        # `if not self.instance.pk`, which a UUID default made always false;
        # the blank-row tests pin the behaviour that actually shipped.)
        self.fields["shipment_group"].required = False
        # Only load the currently-selected variant (not all 1000+).
        # On POST, the user may have changed the variant via AJAX picker —
        # in that case the POSTed value differs from self.instance.variant_id.
        # We MUST use the POSTed value for queryset validation, otherwise
        # ModelChoiceField rejects the new pick and the formset silently
        # invalidates (page re-renders with no save, no visible error).
        submitted = self.data.get(self.add_prefix("variant")) if self.data else None
        target_pk = submitted or (self.instance.variant_id if self.instance.pk else None)
        if target_pk:
            self.fields["variant"].queryset = self.fields["variant"].queryset.filter(pk=target_pk)
        else:
            self.fields["variant"].queryset = self.fields["variant"].queryset.none()


class LineItemInlineFormSet(BaseInlineFormSet):
    """Lines in the order the editor saved them (``sort_order``).

    Lines saved before the editor kept their order all have ``sort_order``
    0; ``created_at`` then ``pk`` break the tie, so they render in the same
    order on every load instead of whatever order the database returns.

    An order line that is locked to its shipment (``order_line_move_locks``:
    shipped, picked, on a PO or a pick ticket) cannot be deleted either: the
    PO and pick-ticket lines point at it with ``SET_NULL``, so removing it
    would leave those documents describing a line the order no longer has.
    """

    def get_queryset(self):
        if not hasattr(self, "_ordered_queryset"):
            self._ordered_queryset = super().get_queryset().order_by("sort_order", "created_at", "pk")
        return self._ordered_queryset

    def clean(self):
        super().clean()
        if any(self.errors) or not isinstance(self.instance, Order) or self.instance._state.adding:
            return
        deleting = [
            form for form in self.forms
            if not form.instance._state.adding and self._should_delete_form(form)
        ]
        if not deleting:
            return
        from .line_locks import order_line_move_locks

        locks = order_line_move_locks(self.instance)
        for form in deleting:
            lock = locks.get(form.instance.pk)
            if lock:
                raise ValidationError(
                    f"{form.instance.item_name or form.instance.sku or 'A line'} cannot be removed: "
                    f"{lock['reason']}"
                )


OrderLineItemFormSet = inlineformset_factory(
    Order, OrderLineItem,
    form=OrderLineItemForm,
    formset=LineItemInlineFormSet,
    extra=1,
    can_delete=True,
)


class QuoteForm(forms.ModelForm):
    class Meta:
        model = Quote
        fields = [
            # Header
            "customer", "quote_expiration_date", "validity_period_days",
            # Address
            "billing_address", "shipping_address", "contact",
            # Accounting
            "company",
            # Currency
            "currency", "exchange_rate", "price_list",
            # Tax
            "tax_rate", "tax_exempt",
            # Totals
            "subtotal_ex_tax", "discount_amount", "tax_total",
            "shipping_cost_ex_tax", "grand_total",
            # Quote-specific. The deal's stage, probability, follow-up and
            # attribution live on the engagement now (CRM Phase 3, spec 1.6).
            "status", "sales_rep",
            "payment_terms_proposed", "shipping_terms_proposed",
            "include_standard_terms",
            "special_conditions",
            "internal_notes",
        ]
        widgets = {
            "quote_expiration_date": forms.DateInput(attrs={"type": "date"}),
            "special_conditions": forms.Textarea(attrs={"rows": 4}),
            "internal_notes": forms.Textarea(attrs={"rows": 4}),
            "payment_terms_proposed": forms.Select(choices=PAYMENT_TERMS_CHOICES),
            "shipping_terms_proposed": forms.Select(choices=SHIPPING_TERMS_CHOICES),
        }

    # The deal a NEW quote joins (R1.06): one of the customer's open or won
    # engagements, else "New engagement" and Quote.save() opens one
    # (crm.engagements.ensure_for_quote). Not a model field: on an existing
    # quote the engagement is fixed (a revision inherits its original's).
    engagement = forms.ModelChoiceField(
        queryset=None, required=False, label="Engagement", empty_label="New engagement",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _default_company(self)
        for fk in ("customer", "billing_address", "shipping_address", "contact"):
            if fk in self.fields:
                _limit_fk_queryset(self, fk)
        if self.instance._state.adding:
            self._limit_engagement_queryset()
        else:
            del self.fields["engagement"]

    def _limit_engagement_queryset(self):
        """The engagements the picked customer's party is on, plus the one
        named by ``initial`` (the engagement page's "New quote") or the POST,
        so a page whose customer was just created keeps its selection."""
        from django.db.models import Q
        from django.urls import reverse

        from crm.engagements import engagements_for_customer
        from crm.models import Engagement
        from customers.models import Customer

        field = self.fields["engagement"]
        field.widget.attrs["data-options-url"] = reverse("erp-quote-engagement-options")
        customer_pk = (
            self.data.get(self.add_prefix("customer")) if self.is_bound else self.initial.get("customer")
        )
        company_pk = (
            self.data.get(self.add_prefix("company")) if self.is_bound else self.initial.get("company")
        )
        picked = self.data.get(self.add_prefix("engagement")) if self.is_bound else self.initial.get("engagement")
        qs = Engagement.objects.none()
        customer = Customer.objects.filter(pk=customer_pk).first() if customer_pk else None
        if customer is not None and company_pk:
            try:
                qs = engagements_for_customer(Company(pk=company_pk), customer)
            except (ValueError, TypeError):
                qs = Engagement.objects.none()
        if picked:
            try:
                qs = Engagement.objects.filter(Q(pk__in=qs.values("pk")) | Q(pk=picked)).select_related("stage")
            except (ValueError, TypeError, ValidationError):
                pass
        field.queryset = qs
        field.label_from_instance = lambda eng: f"{eng.number} {eng.title}".strip() + (
            f" ({eng.stage.name})" if eng.stage_id else ""
        )

    def clean(self):
        cleaned = super().clean()
        engagement = cleaned.get("engagement")
        if engagement is not None and self.instance._state.adding:
            from crm.engagements import check_quote_engagement

            company = cleaned.get("company")
            customer = cleaned.get("customer")
            if company is not None:
                try:
                    check_quote_engagement(engagement, company=company, customer=customer)
                except ValidationError as exc:
                    self.add_error("engagement", exc.message_dict.get("engagement", ["Not allowed."])[0])
        return cleaned


class QuoteLineItemForm(ShipmentGroupIndexMixin, forms.ModelForm):
    PARENT_FIELD = "quote"
    sg_index = forms.IntegerField(widget=forms.HiddenInput, required=False, initial=0)

    class Meta:
        model = QuoteLineItem
        fields = [
            "variant", "sku", "item_name", "quantity",
            "unit_price_ex_tax", "discount_percent", "line_total_ex_tax",
            "shipment_group",
        ]
        widgets = {
            "shipment_group": forms.HiddenInput,
            "item_name": forms.Textarea(attrs={"rows": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["quantity"].initial = 1
        self.fields["shipment_group"].required = False
        # Mirrors OrderLineItemForm: prefer the POSTed variant pk over
        # the instance's saved variant_id so AJAX-picked changes pass
        # ModelChoiceField validation. See OrderLineItemForm.__init__
        # for the full rationale.
        submitted = self.data.get(self.add_prefix("variant")) if self.data else None
        target_pk = submitted or (self.instance.variant_id if self.instance.pk else None)
        if target_pk:
            self.fields["variant"].queryset = self.fields["variant"].queryset.filter(pk=target_pk)
        else:
            self.fields["variant"].queryset = self.fields["variant"].queryset.none()


QuoteLineItemFormSet = inlineformset_factory(
    Quote, QuoteLineItem,
    form=QuoteLineItemForm,
    formset=LineItemInlineFormSet,
    extra=1,
    can_delete=True,
)
