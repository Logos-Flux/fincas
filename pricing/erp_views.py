from django.views.generic import DetailView

from core.views.form_view import ERPCreateView, ERPUpdateView
from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin

from .forms import PriceListForm, PriceListItemForm
from .models import PriceList

# -- Price List List -------------------------------------------------------

class PriceListListView(ERPListView):
    model = PriceList
    current_section = "price_lists"
    page_title = "Price Lists"
    entity_name = "Price List"
    create_url_name = "erp-pricelist-create"
    ordering = ["name"]
    search_fields = ["name", "description"]
    list_columns = [
        {"field": "name", "label": "Name", "link": True, "link_url_name": "erp-pricelist-detail"},
        {"field": "price_list_type", "label": "Type", "badge": True},
        {"field": "currency", "label": "Currency"},
        {"field": "is_active", "label": "Active", "bool_badge": True},
    ]
    list_filters = [
        {"field": "price_list_type", "label": "Type", "choices": PriceList.PriceListType.choices},
    ]


# -- Price List Create / Edit ----------------------------------------------

class PriceListFormMixin:
    model = PriceList
    form_class = PriceListForm
    current_section = "price_lists"
    entity_name = "Price List"
    success_url_name = "erp-pricelist-list"
    form_sections = [
        {
            "title": "Identification",
            "fields": ["name", "price_list_type", "currency", "is_default", "is_active"],
            "collapsible": False,
        },
        {
            "title": "Company",
            "fields": ["company"],
            "collapsible": False,
        },
        {
            "title": "Validity Period",
            "fields": ["valid_from", "valid_to"],
            "collapsible": False,
        },
        {
            "title": "Controls",
            "fields": ["margin_floor_percent", "discount_cap_percent"],
            "collapsible": True,
        },
        {
            "title": "Description",
            "fields": ["description", "notes"],
            "collapsible": True,
        },
    ]


class PriceListCreateView(PriceListFormMixin, ERPCreateView):
    page_title = "New Price List"


class PriceListUpdateView(PriceListFormMixin, ERPUpdateView):
    page_title = "Edit Price List"


# -- Price List Detail (items table) -----------------------------------------

class PriceListDetailView(ERPViewMixin, DetailView):
    model = PriceList
    template_name = "erp/price_list_detail.html"
    context_object_name = "price_list"
    current_section = "price_lists"
    page_title = "Price List"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["items"] = (
            self.object.items
            .select_related("variant", "variant__product")
            .order_by("variant__product__title", "variant__sku", "min_qty")
        )
        context["item_form"] = PriceListItemForm(initial={"price_list": self.object})
        context["page_title"] = str(self.object)
        context["status_colors"] = ERPListView.STATUS_COLORS
        return context
