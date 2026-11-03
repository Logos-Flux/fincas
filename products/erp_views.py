import json
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.views import View

from core.views.form_view import ERPCreateView, ERPUpdateView
from core.views.list_view import ERPListView
from purchasing.models import Supplier

from .forms import BrandForm, CategoryForm, ProductForm, ProductVariantFormSet
from .models import (
    Brand,
    Category,
    Product,
    ProductCategory,
    ProductImage,
    ProductVariant,
    SupplierProduct,
)
from .seo_checks import seo_copy_warnings


class ProductListView(ERPListView):
    model = Product
    current_section = "products"
    page_title = "Products"
    entity_name = "Product"
    create_url_name = "erp-product-create"
    ordering = ["-created_at"]
    search_fields = ["title", "part_number", "mpn"]
    list_columns = [
        {"field": "part_number", "label": "SKU", "link": True, "link_url_name": "erp-product-detail"},
        {"field": "title", "label": "Name"},
        {"field": "brand", "label": "Brand"},
        {"field": "product_type", "label": "Type", "badge": True},
        {"field": "is_active", "label": "Active", "bool_badge": True},
    ]
    list_filters = [
        {"field": "product_type", "label": "Type", "choices": Product.ProductType.choices},
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("brand")


# -- Unified Item Profile (create + view/edit) --------------------------------

# Sections define the layout for both parent product and variant forms.
PARENT_SECTIONS = [
    {
        "id": "core",
        "title": "Product Info",
        "icon": "fas fa-box",
        "fields": [
            "title", "part_number", "brand", "product_type",
            "is_active", "visible_on_website", "has_variants",
            "is_featured_store", "search_boost",
        ],
    },
    {
        "id": "description",
        "title": "Description",
        "icon": "fas fa-align-left",
        "fields": ["description", "short_description"],
    },
    {
        "id": "images",
        "title": "Images",
        "icon": "fas fa-images",
        "fields": ["primary_image_id", "thumbnail_image_id"],
    },
    {
        "id": "identifiers",
        "title": "Identifiers",
        "icon": "fas fa-barcode",
        "fields": ["mpn", "gtin"],
    },
    {
        "id": "specifications",
        "title": "Specifications",
        "icon": "fas fa-list-check",
        "fields": [
            "specifications", "structured_specs",
            "unstructured_specs", "aggregate_spec_table",
        ],
        "collapsed": True,
    },
    {
        "id": "safety-warnings",
        "title": "Safety Warnings",
        "icon": "fas fa-exclamation-triangle",
        "fields": ["safety_warnings"],
        "collapsed": True,
    },
    {
        "id": "ratings",
        "title": "Ratings",
        "icon": "fas fa-star",
        "fields": ["rating_value", "rating_count"],
        "collapsed": True,
    },
    {
        "id": "item-dimensions",
        "title": "Item Dimensions",
        "icon": "fas fa-ruler-combined",
        "fields": [
            "item_weight", "item_weight_uom",
            "item_length", "item_width", "item_height", "item_dimension_uom",
        ],
        "collapsed": True,
    },
    {
        "id": "shipping-dimensions",
        "title": "Shipping Dimensions",
        "icon": "fas fa-truck",
        "fields": [
            "shipping_weight", "shipping_weight_uom",
            "shipping_length", "shipping_width", "shipping_height",
            "shipping_dimension_uom",
        ],
        "collapsed": True,
    },
    {
        "id": "shipping-flags",
        "title": "Shipping Methods & Hazmat",
        "icon": "fas fa-shipping-fast",
        "fields": [
            "ships_usps", "ships_ups", "ships_ltl", "ships_pickup", "free_shipping",
            "hazmat_flag", "hazmat_class", "oversized_flag",
        ],
        "collapsed": True,
    },
    {
        "id": "uom",
        "title": "Units of Measure",
        "icon": "fas fa-balance-scale",
        "fields": [
            "selling_uom", "selling_qty_per_uom",
            "purchasing_uom", "purchasing_qty_per_uom",
            "sell_pallet_only", "purchase_pallet_only",
        ],
        "collapsed": True,
    },
    {
        "id": "moq",
        "title": "MOQ & Lead Time",
        "icon": "fas fa-clock",
        "fields": ["minimum_order_qty", "lead_time_days"],
        "collapsed": True,
    },
    {
        "id": "seo",
        "title": "SEO",
        "icon": "fas fa-search",
        "fields": [
            "seo_title", "seo_meta_description",
            "seo_og_title", "seo_og_description",
            "seo_keywords", "seo_robots",
            "seo_related_searches", "seo_use_cases",
            "seo_faqs", "seo_schema_json",
            "seo_optimization_score", "seo_last_optimized",
            "seo_image_meta", "seo_cross_link_hints",
        ],
        "collapsed": True,
    },
    {
        "id": "gmc",
        "title": "Google Merchant Center",
        "icon": "fab fa-google",
        "fields": [
            "gmc_google_category", "gmc_product_type",
            "gmc_condition", "gmc_availability",
            "gmc_shipping_label",
            "gmc_custom_label_0", "gmc_custom_label_1",
            "gmc_custom_label_2", "gmc_custom_label_3",
            "gmc_custom_label_4",
            "gmc_additional_image_links",
            "gmc_energy_efficiency_class", "gmc_age_group",
            "gmc_multipack", "gmc_is_bundle",
        ],
        "collapsed": True,
    },
    {
        "id": "infrastructure",
        "title": "Infrastructure & Legacy IDs",
        "icon": "fas fa-database",
        "fields": ["bc_id", "erpnext_name", "d1_id", "vectorize_id"],
        "collapsed": True,
    },
]

# Variant fields shown in the inline variant cards
VARIANT_CORE_FIELDS = ["sku", "name", "is_active", "visible_on_website", "sort_order"]
VARIANT_PRICING_FIELDS = ["retail_price", "sale_price", "msrp_price", "map_price", "wholesale_price"]
VARIANT_STOCK_FIELDS = ["stock", "stock_status"]
VARIANT_IMAGE_FIELDS = ["primary_image_id"]
VARIANT_ID_FIELDS = ["gtin", "mpn"]

VARIANT_SECTIONS = [
    # Every ProductVariantForm field must be listed in a section: the form uses
    # `exclude`, so a field the cards never render is posted empty and wipes
    # the stored value on every product save (VariantSectionsCoverFormTests).
    {"id": "v-core", "title": "Core", "fields": VARIANT_CORE_FIELDS},
    {"id": "v-pricing", "title": "Pricing", "fields": VARIANT_PRICING_FIELDS},
    {"id": "v-stock", "title": "Stock", "fields": VARIANT_STOCK_FIELDS},
    {"id": "v-image", "title": "Image", "fields": VARIANT_IMAGE_FIELDS},
    {"id": "v-identifiers", "title": "Identifiers", "fields": VARIANT_ID_FIELDS},
    {
        "id": "v-dimensions",
        "title": "Dimensions (override)",
        "collapsed": True,
        "fields": [
            "item_weight", "item_weight_uom",
            "item_length", "item_width", "item_height", "item_dimension_uom",
            "shipping_weight", "shipping_weight_uom",
            "shipping_length", "shipping_width", "shipping_height",
            "shipping_dimension_uom",
        ],
    },
    {
        "id": "v-shipping",
        "title": "Shipping (override)",
        "collapsed": True,
        "fields": [
            "ships_usps", "ships_ups", "ships_ltl", "ships_pickup", "free_shipping",
            "hazmat_flag", "hazmat_class", "oversized_flag",
        ],
    },
    {
        "id": "v-uom",
        "title": "UOM (override)",
        "collapsed": True,
        "fields": [
            "selling_uom", "selling_qty_per_uom",
            "purchasing_uom", "purchasing_qty_per_uom",
            "sell_pallet_only", "purchase_pallet_only",
            "minimum_order_qty", "lead_time_days",
        ],
    },
    {
        "id": "v-gmc",
        "title": "GMC (override)",
        "collapsed": True,
        "fields": [
            "gmc_google_category", "gmc_product_type",
            "gmc_condition", "gmc_availability", "gmc_shipping_label",
            "gmc_custom_label_0", "gmc_custom_label_1",
            "gmc_custom_label_2", "gmc_custom_label_3", "gmc_custom_label_4",
            "gmc_additional_image_links",
            "gmc_energy_efficiency_class",
            "gmc_multipack", "gmc_is_bundle",
        ],
    },
    {
        "id": "v-seo",
        "title": "SEO (override)",
        "collapsed": True,
        "fields": ["seo_title", "seo_meta_description"],
    },
]


class ProductProfileView(LoginRequiredMixin, View):
    """Unified item profile — create (empty/unlocked) or view/edit (filled/locked)."""

    login_url = "/erp/login/"

    def _get_product(self, pk):
        return get_object_or_404(Product.objects.select_related("brand"), pk=pk) if pk else None

    def _build_context(self, request, product, form, formset):
        is_new = product is None
        ctx = {
            "current_section": "products",
            "page_title": "New Product" if is_new else product.title,
            "product": product,
            "form": form,
            "formset": formset,
            "is_new": is_new,
            "parent_sections": PARENT_SECTIONS,
            "variant_sections": VARIANT_SECTIONS,
        }

        if product:
            images = ProductImage.objects.filter(product=product).order_by("-is_primary", "sort_order")
            categories = ProductCategory.objects.filter(product=product).select_related("category")
            ctx["images"] = images
            ctx["primary_image"] = images.filter(is_primary=True).first() or images.first()
            ctx["categories"] = categories
            ctx["created_at"] = product.created_at
            ctx["updated_at"] = product.updated_at

            # Supplier costs — all SupplierProduct rows for this product's variants
            variant_ids = product.variants.values_list("pk", flat=True)
            ctx["supplier_products"] = (
                SupplierProduct.objects
                .filter(variant_id__in=variant_ids, is_active=True)
                .select_related("variant", "supplier")
                .order_by("variant__sku", "-is_primary", "supplier__company_name")
            )

        return ctx

    def get(self, request, pk=None):
        product = self._get_product(pk) if pk else None
        form = ProductForm(instance=product)
        formset = ProductVariantFormSet(instance=product) if product else ProductVariantFormSet()
        ctx = self._build_context(request, product, form, formset)
        html = render_to_string("erp/product_profile.html", ctx, request=request)
        return HttpResponse(html)

    def post(self, request, pk=None):
        product = self._get_product(pk) if pk else None
        form = ProductForm(request.POST, instance=product)
        formset = ProductVariantFormSet(request.POST, instance=product)

        if form.is_valid() and formset.is_valid():
            product = form.save()
            formset.instance = product
            formset.save()
            messages.success(request, f"Product '{product.title}' saved.")
            return redirect("erp-product-detail", pk=product.pk)

        ctx = self._build_context(request, product, form, formset)
        html = render_to_string("erp/product_profile.html", ctx, request=request)
        return HttpResponse(html)


# -- AJAX endpoints -----------------------------------------------------------

class VariantLookupView(LoginRequiredMixin, View):
    """AJAX endpoint: given a variant ID, return item name and price."""
    login_url = "/erp/login/"

    def get(self, request):
        variant_id = request.GET.get("id", "").strip()
        if not variant_id:
            return JsonResponse({"error": "id required"}, status=400)
        try:
            v = ProductVariant.objects.select_related("product").get(pk=variant_id)
        except (ProductVariant.DoesNotExist, ValueError):
            return JsonResponse({"error": "not found"}, status=404)
        return JsonResponse({
            "id": str(v.pk),
            "sku": v.sku,
            "name": v.name or v.product.title,
            "retail_price": str(v.retail_price) if v.retail_price else "0",
            "wholesale_price": str(v.wholesale_price) if v.wholesale_price else "0",
        })


class VariantSearchView(LoginRequiredMixin, View):
    """AJAX endpoint: search variants by SKU or product name."""
    login_url = "/erp/login/"

    def get(self, request):
        q = request.GET.get("q", "").strip()
        if len(q) < 1:
            return JsonResponse({"results": []})

        variants = (
            ProductVariant.objects
            .select_related("product")
            .filter(
                Q(sku__icontains=q)
                | Q(name__icontains=q)
                | Q(product__title__icontains=q)
            )
            .order_by("sku")[:20]
        )

        results = []
        for v in variants:
            results.append({
                "id": str(v.pk),
                "sku": v.sku,
                "name": v.name or v.product.title,
                "retail_price": str(v.retail_price) if v.retail_price else "0",
                "wholesale_price": str(v.wholesale_price) if v.wholesale_price else "0",
            })

        return JsonResponse({"results": results})


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


class SupplierCostDetailView(LoginRequiredMixin, UserPassesTestMixin, View):
    """GET: return a single SupplierProduct as JSON for modal pre-fill."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request, pk):
        sp = get_object_or_404(SupplierProduct.objects.select_related("variant", "supplier"), pk=pk)
        return JsonResponse({
            "id": str(sp.pk),
            "variant_id": str(sp.variant_id),
            "variant_sku": sp.variant.sku,
            "supplier_id": str(sp.supplier_id),
            "supplier_name": sp.supplier.display_name or sp.supplier.company_name,
            "supplier_sku": sp.supplier_sku,
            "cost": str(sp.cost),
            "purchasing_uom": sp.purchasing_uom,
            "qty_per_uom": sp.qty_per_uom,
            "minimum_order_qty": sp.minimum_order_qty,
            "lead_time_days": sp.lead_time_days,
            "is_primary": sp.is_primary,
            "shipping_location": sp.shipping_location,
            "notes": sp.notes,
        })


class SupplierCostSaveView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST: create or update a SupplierProduct."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk=None):
        # Bad input is the caller's error (400), an unknown row is a 404;
        # neither is a 500 (the access matrix found KeyError and
        # DoesNotExist escaping here).
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        if not isinstance(data, dict):
            return JsonResponse({"error": "Expected a JSON object"}, status=400)

        missing = [f for f in ("variant_id", "supplier_id", "cost") if not data.get(f)]
        if missing:
            return JsonResponse({"error": f"Missing required fields: {', '.join(missing)}"}, status=400)

        sp = get_object_or_404(SupplierProduct, pk=pk) if pk else SupplierProduct()

        variant = ProductVariant.objects.filter(pk=_uuid_or_none(data["variant_id"])).first()
        supplier = Supplier.objects.filter(pk=_uuid_or_none(data["supplier_id"])).first()
        if variant is None:
            return JsonResponse({"error": "Unknown variant_id"}, status=400)
        if supplier is None:
            return JsonResponse({"error": "Unknown supplier_id"}, status=400)

        try:
            # Required fields
            sp.variant = variant
            sp.supplier = supplier
            sp.cost = Decimal(str(data["cost"]))

            # Optional fields
            sp.supplier_sku = data.get("supplier_sku", "")
            sp.purchasing_uom = data.get("purchasing_uom", "each")
            sp.qty_per_uom = int(data.get("qty_per_uom", 1) or 1)
            sp.minimum_order_qty = int(data.get("minimum_order_qty", 1) or 1)
            lead = data.get("lead_time_days")
            sp.lead_time_days = int(lead) if lead else None
            sp.is_primary = bool(data.get("is_primary", False))
            sp.shipping_location = data.get("shipping_location", "")
            sp.notes = data.get("notes", "")
        except (TypeError, ValueError, InvalidOperation):
            return JsonResponse({"error": "cost, qty_per_uom, minimum_order_qty and lead_time_days must be numbers"}, status=400)

        try:
            sp.save()
        except IntegrityError:
            return JsonResponse({"error": "This supplier already has a cost for this variant"}, status=400)

        return JsonResponse({
            "id": str(sp.pk),
            "variant_sku": variant.sku,
            "supplier_name": supplier.display_name or supplier.company_name,
            "supplier_type": supplier.supplier_type,
            "supplier_sku": sp.supplier_sku,
            "cost": str(sp.cost),
            "purchasing_uom": sp.purchasing_uom,
            "qty_per_uom": sp.qty_per_uom,
            "unit_cost": str(sp.unit_cost),
            "minimum_order_qty": sp.minimum_order_qty,
            "lead_time_days": sp.lead_time_days,
            "is_primary": sp.is_primary,
        })


class SupplierCostDeleteView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST: deactivate a SupplierProduct."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk):
        sp = get_object_or_404(SupplierProduct, pk=pk)
        sp.is_active = False
        sp.save()
        return JsonResponse({"ok": True})


# -- Brands -------------------------------------------------------------------

class BrandListView(ERPListView):
    model = Brand
    current_section = "brands"
    page_title = "Brands"
    entity_name = "Brand"
    create_url_name = "erp-brand-create"
    ordering = ["title"]
    search_fields = ["title"]
    list_columns = [
        {"field": "title", "label": "Brand", "link": True, "link_url_name": "erp-brand-detail"},
        {"field": "visible_on_website", "label": "Visible", "bool_badge": True},
        {"field": "is_authorized_dealer", "label": "Authorized", "bool_badge": True},
        {"field": "is_featured_store", "label": "Featured", "bool_badge": True},
    ]


BRAND_SECTIONS = [
    {
        "title": "Brand Info",
        "fields": ["title", "visible_on_website", "is_featured_store",
                   "is_authorized_dealer", "sort_order"],
    },
    {
        "title": "Description & Website",
        "fields": ["description", "website_url"],
    },
    {
        "title": "Images",
        "fields": ["logo_image_id", "banner_image_id"],
    },
    {
        "title": "SEO",
        "fields": ["seo_title", "seo_meta_description",
                   "seo_og_title", "seo_og_description", "seo_og_image_id"],
        "collapsible": True,
    },
    {
        "title": "Migration IDs",
        "fields": ["bc_id", "erpnext_name", "d1_id"],
        "collapsible": True,
    },
]


class SEOCopyWarningsMixin:
    """Flash soft warnings about templated or cut-off SEO copy after a save.

    The row is saved either way (products/seo_checks.py explains why these
    are warnings, not form errors).
    """

    def form_valid(self, form):
        response = super().form_valid(form)
        for warning in seo_copy_warnings(self.object):
            messages.warning(self.request, warning)
        return response


class BrandCreateView(SEOCopyWarningsMixin, ERPCreateView):
    model = Brand
    form_class = BrandForm
    current_section = "brands"
    page_title = "New Brand"
    entity_name = "Brand"
    success_url_name = "erp-brand-list"
    form_sections = BRAND_SECTIONS


class BrandUpdateView(SEOCopyWarningsMixin, ERPUpdateView):
    model = Brand
    form_class = BrandForm
    current_section = "brands"
    page_title = "Edit Brand"
    entity_name = "Brand"
    success_url_name = "erp-brand-list"
    form_sections = BRAND_SECTIONS


# -- Categories ---------------------------------------------------------------

class CategoryListView(ERPListView):
    model = Category
    current_section = "categories"
    page_title = "Categories"
    entity_name = "Category"
    create_url_name = "erp-category-create"
    ordering = ["sort_order", "title"]
    search_fields = ["title"]
    list_columns = [
        {"field": "title", "label": "Category", "link": True, "link_url_name": "erp-category-detail"},
        {"field": "parent", "label": "Parent"},
        {"field": "tree_depth", "label": "Depth"},
        {"field": "visible_on_website", "label": "Visible", "bool_badge": True},
        {"field": "is_featured_store", "label": "Featured", "bool_badge": True},
    ]
    list_filters = [
        {"field": "tree_depth", "label": "Depth", "choices": [(0, "Top-level"), (1, "Child")]},
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("parent")


CATEGORY_SECTIONS = [
    {
        "title": "Category Info",
        "fields": ["title", "parent", "visible_on_website",
                   "is_featured_store", "sort_order"],
    },
    {
        "title": "Description",
        "fields": ["description", "short_description"],
    },
    # Must stay listed: CategoryForm uses `exclude`, so body_copy is a form
    # field either way, and a field the template never renders is submitted
    # empty, so every save would silently wipe the page copy.
    {
        "title": "Page Body Copy",
        "fields": ["body_copy"],
    },
    {
        "title": "Images",
        "fields": ["primary_image_id", "thumbnail_image_id"],
    },
    {
        "title": "SEO",
        "fields": ["seo_title", "seo_meta_description",
                   "seo_og_title", "seo_og_description",
                   "seo_og_image_id", "seo_canonical_url"],
        "collapsible": True,
    },
    {
        "title": "Migration IDs",
        "fields": ["bc_id", "erpnext_name", "d1_id"],
        "collapsible": True,
    },
]


class CategoryCreateView(SEOCopyWarningsMixin, ERPCreateView):
    model = Category
    form_class = CategoryForm
    current_section = "categories"
    page_title = "New Category"
    entity_name = "Category"
    success_url_name = "erp-category-list"
    form_sections = CATEGORY_SECTIONS


class CategoryUpdateView(SEOCopyWarningsMixin, ERPUpdateView):
    model = Category
    form_class = CategoryForm
    current_section = "categories"
    page_title = "Edit Category"
    entity_name = "Category"
    success_url_name = "erp-category-list"
    form_sections = CATEGORY_SECTIONS
