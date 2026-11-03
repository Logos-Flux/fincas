from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import JsonResponse
from django.views import View

from core.views.form_view import ERPCreateView, ERPUpdateView
from core.views.list_view import ERPListView

from .forms import WarehouseForm
from .models import Warehouse

# -- Warehouse List --------------------------------------------------------

class WarehouseListView(ERPListView):
    model = Warehouse
    current_section = "warehouses"
    page_title = "Warehouses"
    entity_name = "Warehouse"
    create_url_name = "erp-warehouse-create"
    ordering = ["name"]
    search_fields = ["warehouse_code", "name"]
    list_columns = [
        {"field": "warehouse_code", "label": "Code", "link": True, "link_url_name": "erp-warehouse-edit"},
        {"field": "name", "label": "Name"},
        {"field": "warehouse_type", "label": "Type", "badge": True},
        {"field": "is_active", "label": "Active", "bool_badge": True},
    ]
    list_filters = [
        {"field": "warehouse_type", "label": "Type", "choices": Warehouse.WarehouseType.choices},
    ]


# -- Warehouse Create / Edit -----------------------------------------------

class WarehouseFormMixin:
    model = Warehouse
    form_class = WarehouseForm
    current_section = "warehouses"
    entity_name = "Warehouse"
    success_url_name = "erp-warehouse-list"
    form_sections = [
        {
            "title": "Identification",
            "fields": ["warehouse_code", "name", "warehouse_type", "is_active", "company"],
            "collapsible": False,
        },
        {
            "title": "Capabilities",
            "fields": ["accepts_incoming", "can_fulfill", "processing_time_days"],
            "collapsible": False,
        },
        {
            "title": "Address & Contact",
            "fields": [
                "address_line_1", "address_line_2",
                "city", "state_province", "postal_code", "country",
                "ship_from_label", "phone", "email",
            ],
            "collapsible": False,
        },
        {
            "title": "Company & Management",
            "fields": ["default_for_company", "manager"],
            "collapsible": True,
        },
        {
            "title": "Notes",
            "fields": ["notes"],
            "collapsible": True,
        },
    ]


class WarehouseCreateView(WarehouseFormMixin, ERPCreateView):
    page_title = "New Warehouse"


class WarehouseUpdateView(WarehouseFormMixin, ERPUpdateView):
    page_title = "Edit Warehouse"


# -- Warehouse AJAX endpoints ----------------------------------------------

class WarehouseSearchAjaxView(UserPassesTestMixin, View):
    """GET /erp/inventory/warehouses/search-ajax/?q=..."""

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request):
        q = request.GET.get("q", "").strip()
        qs = Warehouse.objects.filter(is_active=True, can_fulfill=True)
        if len(q) >= 2:
            from django.db.models import Q
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(warehouse_code__icontains=q)
                | Q(city__icontains=q)
            )
        results = [
            {
                "id": str(w.pk),
                "label": f"{w.name}" + (f" ({w.city}, {w.state_province})" if w.city else ""),
                "code": w.warehouse_code,
            }
            for w in qs[:15]
        ]
        return JsonResponse({"results": results})
