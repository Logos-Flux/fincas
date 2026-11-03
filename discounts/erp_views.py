import secrets
import string

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import CreateView, DetailView, FormView, UpdateView

from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin

from .forms import DiscountCodeForm, GenerateCodesForm
from .models import DiscountCode


class DiscountCodeListView(ERPListView):
    model = DiscountCode
    current_section = "discounts"
    page_title = "Discount Codes"
    entity_name = "Discount Code"
    create_url_name = "erp-discount-create"
    ordering = ["-created_at"]
    search_fields = ["code", "description"]
    list_columns = [
        {"field": "code", "label": "Code", "link": True, "link_url_name": "erp-discount-detail"},
        {"field": "discount_type", "label": "Type", "badge": True},
        {"field": "value", "label": "Value"},
        {"field": "is_active", "label": "Active", "bool_badge": True},
        {"field": "times_used", "label": "Uses"},
        {"field": "max_uses", "label": "Max"},
        {"field": "valid_to", "label": "Expires"},
    ]
    list_filters = [
        {"field": "discount_type", "label": "Type", "choices": DiscountCode.DiscountType.choices},
        {"field": "is_active", "label": "Active", "choices": [(True, "Yes"), (False, "No")]},
    ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["extra_buttons"] = [
            {
                "url": reverse("erp-discount-generate"),
                "label": "Generate Codes",
                "icon": "fas fa-magic",
                "class": "btn-outline-primary",
            },
        ]
        return context


class DiscountCodeCreateView(ERPViewMixin, CreateView):
    model = DiscountCode
    form_class = DiscountCodeForm
    template_name = "erp/form.html"
    current_section = "discounts"
    page_title = "New Discount Code"
    entity_name = "Discount Code"
    form_sections = [
        {
            "title": "Code",
            "fields": ["code", "description", "is_active"],
        },
        {
            "title": "Discount",
            "fields": ["discount_type", "value", "minimum_order"],
        },
        {
            "title": "Validity",
            "fields": ["valid_from", "valid_to", "max_uses"],
        },
        {
            "title": "Scope",
            "fields": ["applies_to", "allowed_skus"],
        },
    ]

    def get_success_url(self):
        return reverse("erp-discount-list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entity_name"] = self.entity_name
        context["form_sections"] = self.form_sections
        context["is_edit"] = False
        return context


class DiscountCodeUpdateView(ERPViewMixin, UpdateView):
    model = DiscountCode
    form_class = DiscountCodeForm
    template_name = "erp/form.html"
    current_section = "discounts"
    page_title = "Edit Discount Code"
    entity_name = "Discount Code"
    form_sections = [
        {
            "title": "Code",
            "fields": ["code", "description", "is_active"],
        },
        {
            "title": "Discount",
            "fields": ["discount_type", "value", "minimum_order"],
        },
        {
            "title": "Validity",
            "fields": ["valid_from", "valid_to", "max_uses"],
        },
        {
            "title": "Scope",
            "fields": ["applies_to", "allowed_skus"],
        },
    ]

    def get_success_url(self):
        return reverse("erp-discount-detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entity_name"] = self.entity_name
        context["form_sections"] = self.form_sections
        context["is_edit"] = True
        return context


class DiscountCodeDetailView(ERPViewMixin, DetailView):
    model = DiscountCode
    template_name = "discounts/detail.html"
    current_section = "discounts"
    page_title = "Discount Code"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["usages"] = self.object.usages.all()[:50]
        return context


class GenerateCodesView(ERPViewMixin, FormView):
    template_name = "discounts/generate.html"
    form_class = GenerateCodesForm
    current_section = "discounts"
    page_title = "Generate Discount Codes"

    def form_valid(self, form):
        prefix = form.cleaned_data["prefix"].upper()
        count = form.cleaned_data["count"]
        code_length = form.cleaned_data["code_length"]
        discount_type = form.cleaned_data["discount_type"]
        value = form.cleaned_data["value"]
        max_uses = form.cleaned_data["max_uses"]
        applies_to = form.cleaned_data["applies_to"]
        valid_from = form.cleaned_data.get("valid_from")
        valid_to = form.cleaned_data.get("valid_to")
        minimum_order = form.cleaned_data.get("minimum_order")

        # Parse allowed SKUs (one per line)
        skus_text = form.cleaned_data.get("allowed_skus", "")
        allowed_skus = [s.strip() for s in skus_text.splitlines() if s.strip()] if skus_text else []

        alphabet = string.ascii_uppercase + string.digits
        codes = []
        existing = set(DiscountCode.objects.values_list("code", flat=True))

        for _ in range(count):
            for _attempt in range(100):
                suffix = "".join(secrets.choice(alphabet) for _ in range(code_length))
                code = f"{prefix}-{suffix}"
                if code not in existing:
                    existing.add(code)
                    codes.append(
                        DiscountCode(
                            code=code,
                            discount_type=discount_type,
                            value=value,
                            max_uses=max_uses,
                            applies_to=applies_to,
                            allowed_skus=allowed_skus,
                            valid_from=valid_from,
                            valid_to=valid_to,
                            minimum_order=minimum_order,
                        )
                    )
                    break

        DiscountCode.objects.bulk_create(codes)
        messages.success(self.request, f"Generated {len(codes)} discount codes with prefix '{prefix}'.")
        return redirect(reverse("erp-discount-list"))
