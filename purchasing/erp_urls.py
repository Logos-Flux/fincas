from django.urls import path

from .erp_views import (
    PurchaseOrderCreateView,
    PurchaseOrderListView,
    PurchaseOrderReceiveView,
    PurchaseOrderUpdateView,
    SupplierCreateView,
    SupplierListView,
    SupplierSearchAjaxView,
    SupplierShipFromView,
    SupplierUpdateView,
)

urlpatterns = [
    path("", PurchaseOrderListView.as_view(), name="erp-purchaseorder-list"),
    path("new/", PurchaseOrderCreateView.as_view(), name="erp-purchaseorder-create"),
    path("<uuid:pk>/edit/", PurchaseOrderUpdateView.as_view(), name="erp-purchaseorder-edit"),
    path("<uuid:pk>/receive/", PurchaseOrderReceiveView.as_view(), name="erp-purchaseorder-receive"),
    path("suppliers/", SupplierListView.as_view(), name="erp-supplier-list"),
    path("suppliers/search-ajax/", SupplierSearchAjaxView.as_view(), name="erp-supplier-search-ajax"),
    path("suppliers/new/", SupplierCreateView.as_view(), name="erp-supplier-create"),
    path("suppliers/<uuid:pk>/ship-from-locations/", SupplierShipFromView.as_view(), name="erp-supplier-ship-from"),
    path("suppliers/<uuid:pk>/edit/", SupplierUpdateView.as_view(), name="erp-supplier-edit"),
]
