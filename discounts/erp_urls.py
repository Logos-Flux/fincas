from django.urls import path

from .erp_views import (
    DiscountCodeCreateView,
    DiscountCodeDetailView,
    DiscountCodeListView,
    DiscountCodeUpdateView,
    GenerateCodesView,
)

urlpatterns = [
    path("", DiscountCodeListView.as_view(), name="erp-discount-list"),
    path("new/", DiscountCodeCreateView.as_view(), name="erp-discount-create"),
    path("generate/", GenerateCodesView.as_view(), name="erp-discount-generate"),
    path("<uuid:pk>/", DiscountCodeDetailView.as_view(), name="erp-discount-detail"),
    path("<uuid:pk>/edit/", DiscountCodeUpdateView.as_view(), name="erp-discount-edit"),
]
