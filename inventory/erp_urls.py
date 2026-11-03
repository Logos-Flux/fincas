from django.urls import path

from .erp_views import (
    WarehouseCreateView,
    WarehouseListView,
    WarehouseSearchAjaxView,
    WarehouseUpdateView,
)

urlpatterns = [
    path("", WarehouseListView.as_view(), name="erp-warehouse-list"),
    path("warehouses/search-ajax/", WarehouseSearchAjaxView.as_view(), name="erp-warehouse-search-ajax"),
    path("new/", WarehouseCreateView.as_view(), name="erp-warehouse-create"),
    path("<uuid:pk>/edit/", WarehouseUpdateView.as_view(), name="erp-warehouse-edit"),
]
