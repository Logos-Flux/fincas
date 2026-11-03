from django.urls import path

from .erp_views import (
    PriceListCreateView,
    PriceListDetailView,
    PriceListListView,
    PriceListUpdateView,
)

urlpatterns = [
    path("", PriceListListView.as_view(), name="erp-pricelist-list"),
    path("new/", PriceListCreateView.as_view(), name="erp-pricelist-create"),
    path("<uuid:pk>/", PriceListDetailView.as_view(), name="erp-pricelist-detail"),
    path("<uuid:pk>/edit/", PriceListUpdateView.as_view(), name="erp-pricelist-edit"),
]
