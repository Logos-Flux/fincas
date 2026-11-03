from rest_framework import viewsets
from rest_framework.permissions import SAFE_METHODS, AllowAny, BasePermission

from core.managers import scope_to_company
from core.permissions import PrincipalPermission
from core.views import CompanyScopedViewSet

from .models import (
    Brand,
    Category,
    Product,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    SupplierProduct,
    VariantOptionValue,
)
from .serializers import (
    BrandSerializer,
    CategoryIndex,
    CategorySerializer,
    ProductDetailSerializer,
    ProductListSerializer,
    ProductOptionSerializer,
    ProductOptionValueSerializer,
    ProductVariantDetailSerializer,
    ProductVariantListSerializer,
    PublicBrandSerializer,
    PublicCategorySerializer,
    SupplierProductSerializer,
    VariantOptionValueSerializer,
    is_staff_reader,
)


def full_catalog_reader(request, view) -> bool:
    """Staff, or a service account (not the storefront) within its contract."""
    from core.service_accounts import is_service_account, service_contract_allows
    from core.storefront import is_storefront_user

    if is_staff_reader(request):
        return True
    user = getattr(request, "user", None)
    return (
        is_service_account(user)
        and not is_storefront_user(user)
        and service_contract_allows(request, view)
    )


class PublicCatalogReadMixin:
    """Brand/Category reads are public storefront data, like ProductViewSet.

    Anonymous visitors, shoppers and the storefront-api identity read only
    rows that are ``visible_on_website`` (for categories: with every
    ancestor visible too), through the public serializer: the fields a
    storefront renders, never migration provenance. Staff keep the full
    rows and every row. Writes stay behind ``PrincipalPermission``
    (unchanged), so the storefront identity still cannot edit the catalog.
    """

    permission_classes = [AllowAny]
    public_serializer_class = None

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in self.permission_classes]
        return [PrincipalPermission()]

    def public_reader(self) -> bool:
        """Anonymous visitors, shoppers and the storefront identity.

        Staff, and service accounts whose contract allows the read (e.g.
        ``erp_assist`` resolving hidden rows), keep the full staff shape.
        """
        if self.request.method not in SAFE_METHODS:
            return False
        return not full_catalog_reader(self.request, self)

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.public_reader():
            queryset = queryset.filter(visible_on_website=True)
        return queryset

    def get_serializer_class(self):
        if self.public_reader():
            return self.public_serializer_class
        return super().get_serializer_class()


class BrandViewSet(PublicCatalogReadMixin, viewsets.ModelViewSet):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    public_serializer_class = PublicBrandSerializer
    # Global lookup table (no company FK). Reads public (storefront brand
    # pages), writes behind PrincipalPermission: see PublicCatalogReadMixin.
    filterset_fields = ["slug", "visible_on_website", "is_authorized_dealer", "is_featured_store"]
    search_fields = ["title"]
    ordering_fields = ["title", "sort_order", "created_at"]


class CategoryViewSet(PublicCatalogReadMixin, viewsets.ModelViewSet):
    queryset = Category.objects.select_related("parent")
    serializer_class = CategorySerializer
    public_serializer_class = PublicCategorySerializer
    # Global lookup table (no company FK). Reads public (storefront category
    # pages), writes behind PrincipalPermission: see PublicCatalogReadMixin.
    filterset_fields = ["slug", "parent", "visible_on_website", "is_featured_store", "tree_depth"]
    search_fields = ["title"]
    ordering_fields = ["title", "sort_order"]

    def category_index(self):
        # One query for the whole tree per request: visibility, ancestors
        # (breadcrumbs) and children come from it instead of per-row queries.
        if not hasattr(self, "_category_index"):
            self._category_index = CategoryIndex.build()
        return self._category_index

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.public_reader():
            # A visible category under a hidden one is hidden too.
            queryset = queryset.filter(pk__in=self.category_index().public_ids())
        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.public_reader():
            context["category_index"] = self.category_index()
        return context


class CatalogStaffWritePermission(BasePermission):
    """Catalog writes need ERP staff (or a superuser, e.g. the API-key service)."""

    def has_permission(self, request, view):
        from core.permissions import is_staff_principal

        return is_staff_principal(request.user)


class ProductViewSet(CompanyScopedViewSet):
    # Deliberately anonymous-readable: headless storefront catalog
    # (testing-plan §4 pinned decision). Anonymous callers only see
    # visible_on_website + is_active rows via get_queryset below.
    # Reads are open; every write requires authentication — AllowAny on
    # the whole viewset let anonymous callers CREATE/UPDATE/DELETE
    # catalog rows (M3 Track G). Reads honour the class attribute (the
    # patch seam used by permission-audit tests); writes are always
    # authenticated regardless of it.
    permission_classes = [AllowAny]
    queryset = Product.objects.select_related("brand").prefetch_related(
        "variants", "images", "options", "categories"
    )

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permission() for permission in self.permission_classes]
        # Writes stay behind auth — and behind the storefront guest gate:
        # the low-privilege storefront identity holds no catalog write
        # permissions (F-037).
        # ...and behind ERP staff: catalog writes are an ERP action. Without
        # this, any authenticated non-staff account with no company (a shopper
        # login) could POST a product into ANY company, because company
        # enforcement on create is a no-op for company-less callers.
        return [PrincipalPermission(), CatalogStaffWritePermission()]
    filterset_fields = ["brand", "product_type", "is_active", "visible_on_website", "company"]
    search_fields = ["title", "part_number", "slug"]
    ordering_fields = ["title", "created_at"]

    def get_queryset(self):
        # READS: the catalog is public data. Anyone who isn't ERP staff —
        # anonymous visitors, but also the storefront-api identity behind
        # guest checkout and logged-in shoppers — reads the public catalog.
        # Before this, a non-staff token fell through to company scoping,
        # matched no company and got an EMPTY catalog, so the storefront
        # swapped to its demo products mid-checkout.
        # WRITES keep company scoping for everyone: widening the write
        # queryset would let one company's staff edit or delete another
        # company's public products.
        user = getattr(self.request, "user", None)
        if user is None or not user.is_authenticated:
            queryset = self.queryset.filter(visible_on_website=True, is_active=True)
        elif not is_staff_reader(self.request) and self.request.method in SAFE_METHODS:
            queryset = self.queryset.filter(visible_on_website=True, is_active=True)
        else:
            queryset = super().get_queryset()
        if self.request.method in SAFE_METHODS:
            queryset = self._filter_by_slugs(queryset)
        return queryset

    def _filter_by_slugs(self, queryset):
        """Storefront listing filters: ``?category_slug=`` and ``?brand_slug=``.

        A category page lists its own products and those of every category
        below it, so ``category_slug`` takes the whole subtree. An unknown
        slug matches nothing (an empty page), never the whole catalog.
        """
        params = self.request.query_params
        # Public readers resolve slugs against public rows only: a hidden
        # category or brand (or a hidden branch of the tree) lists nothing.
        public = not is_staff_reader(self.request)
        category_slug = params.get("category_slug")
        if category_slug:
            ids = CategoryIndex.build().subtree_ids(category_slug, public_only=public)
            queryset = queryset.filter(categories__in=ids).distinct() if ids else queryset.none()
        brand_slug = params.get("brand_slug")
        if brand_slug:
            brands = {"brand__slug": brand_slug}
            if public:
                brands["brand__visible_on_website"] = True
            queryset = queryset.filter(**brands)
        return queryset

    def get_serializer_class(self):
        # Full rows per product are a staff/sync feature (and ~4 queries each).
        if (
            self.action == "list"
            and self.request.query_params.get("detail") == "true"
            and is_staff_reader(self.request)
        ):
            return ProductDetailSerializer
        if self.action == "list":
            return ProductListSerializer
        return ProductDetailSerializer


class ProductVariantViewSet(viewsets.ModelViewSet):
    queryset = ProductVariant.objects.select_related("product")
    permission_classes = [PrincipalPermission]
    filterset_fields = ["product", "is_active", "stock_status"]
    search_fields = ["sku", "name"]
    ordering_fields = ["sku", "sort_order"]

    def get_serializer_class(self):
        if self.action == "list":
            return ProductVariantListSerializer
        return ProductVariantDetailSerializer

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "product__company"
        )


class SupplierProductViewSet(CompanyScopedViewSet):
    queryset = SupplierProduct.objects.select_related("variant", "supplier")
    serializer_class = SupplierProductSerializer
    filterset_fields = ["variant", "supplier", "is_active", "is_primary"]
    search_fields = ["variant__sku", "supplier_sku"]
    company_field = None
    parent_links = [
        ("supplier", "company"),
        ("variant", "product__company"),
    ]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "supplier__company"
        )


class ProductOptionViewSet(CompanyScopedViewSet):
    queryset = ProductOption.objects.prefetch_related('values')
    serializer_class = ProductOptionSerializer
    filterset_fields = ['product']
    search_fields = ['name']
    ordering_fields = ['sort_order']
    company_field = None
    parent_links = [("product", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "product__company"
        )


class ProductOptionValueViewSet(CompanyScopedViewSet):
    queryset = ProductOptionValue.objects.select_related('option')
    serializer_class = ProductOptionValueSerializer
    filterset_fields = ['option']
    search_fields = ['value']
    ordering_fields = ['sort_order']
    company_field = None
    parent_links = [("option", "product__company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "option__product__company"
        )


class VariantOptionValueViewSet(CompanyScopedViewSet):
    queryset = VariantOptionValue.objects.select_related('variant', 'option_value')
    serializer_class = VariantOptionValueSerializer
    filterset_fields = ['variant', 'option_value']
    company_field = None
    parent_links = [("variant", "product__company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "variant__product__company"
        )
