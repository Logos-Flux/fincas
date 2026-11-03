from django.urls import path

from .erp_views import (
    BrandCreateView,
    BrandListView,
    BrandUpdateView,
    CategoryCreateView,
    CategoryListView,
    CategoryUpdateView,
    ProductListView,
    ProductProfileView,
    SupplierCostDeleteView,
    SupplierCostDetailView,
    SupplierCostSaveView,
    VariantLookupView,
    VariantSearchView,
)

urlpatterns = [
    path("", ProductListView.as_view(), name="erp-product-list"),
    path("new/", ProductProfileView.as_view(), name="erp-product-create"),
    path("variant-lookup/", VariantLookupView.as_view(), name="erp-variant-lookup"),
    path("variant-search/", VariantSearchView.as_view(), name="erp-variant-search"),
    path("supplier-cost/create/", SupplierCostSaveView.as_view(), name="erp-supplier-cost-create"),
    path("supplier-cost/<uuid:pk>/", SupplierCostDetailView.as_view(), name="erp-supplier-cost-detail"),
    path("supplier-cost/<uuid:pk>/update/", SupplierCostSaveView.as_view(), name="erp-supplier-cost-update"),
    path("supplier-cost/<uuid:pk>/delete/", SupplierCostDeleteView.as_view(), name="erp-supplier-cost-delete"),
    path("<uuid:pk>/", ProductProfileView.as_view(), name="erp-product-detail"),
    # Brands
    path("brands/", BrandListView.as_view(), name="erp-brand-list"),
    path("brands/new/", BrandCreateView.as_view(), name="erp-brand-create"),
    path("brands/<uuid:pk>/", BrandUpdateView.as_view(), name="erp-brand-detail"),
    # Categories
    path("categories/", CategoryListView.as_view(), name="erp-category-list"),
    path("categories/new/", CategoryCreateView.as_view(), name="erp-category-create"),
    path("categories/<uuid:pk>/", CategoryUpdateView.as_view(), name="erp-category-detail"),
]
