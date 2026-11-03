from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.html import strip_tags
from django.utils.text import Truncator
from rest_framework import serializers

from documents.models import ProductDocument

from .models import (
    BODY_COPY_NOT_APPROVED_MESSAGE,
    Brand,
    Category,
    Product,
    ProductCategory,
    ProductImage,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    SupplierProduct,
    VariantImage,
    VariantOptionValue,
    body_copy_is_draft,
    validate_safety_warnings,
)
from .templatetags.bunny_images import bunny_image_url


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = "__all__"
        read_only_fields = ["id", "tree_depth", "created_at", "updated_at"]

    def validate_body_copy(self, value):
        # DRF never calls Model.clean(), so repeat Category.clean()'s DRAFT
        # guard here. The storefront must never render such copy.
        if body_copy_is_draft(value):
            raise serializers.ValidationError(BODY_COPY_NOT_APPROVED_MESSAGE)
        return value


class CategoryIndex:
    """The whole category tree from one query, for breadcrumbs and subtrees.

    Category trees are small (tens to a few hundred rows), so reading every
    row once beats a query per ancestor per category on a list page.
    """

    FIELDS = ("id", "slug", "title", "parent_id", "visible_on_website", "sort_order")

    def __init__(self, rows):
        self.by_id = {row["id"]: row for row in rows}
        self.by_slug = {row["slug"]: row for row in rows}
        self._public = {}
        self.children = {}
        for row in sorted(rows, key=lambda r: (r["sort_order"], r["title"])):
            self.children.setdefault(row["parent_id"], []).append(row)

    @classmethod
    def build(cls):
        return cls(list(Category.objects.values(*cls.FIELDS)))

    def ancestors(self, category_id):
        """Root-first chain above a category (not including it)."""
        chain, seen = [], {category_id}
        parent_id = self.by_id.get(category_id, {}).get("parent_id")
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            row = self.by_id.get(parent_id)
            if row is None:
                break
            chain.append(row)
            parent_id = row["parent_id"]
        return list(reversed(chain))

    def is_public(self, category_id):
        """Visible on the website AND every ancestor is too.

        A visible category under a hidden one is hidden for public readers:
        otherwise its breadcrumb trail (and ``parent``) would name the hidden
        row and link to a page that 404s.
        """
        if category_id in self._public:
            return self._public[category_id]
        result, seen, current = True, set(), category_id
        while current is not None:
            row = self.by_id.get(current)
            if row is None or current in seen or not row["visible_on_website"]:
                result = False
                break
            seen.add(current)
            current = row["parent_id"]
        self._public[category_id] = result
        return result

    def public_ids(self):
        return [category_id for category_id in self.by_id if self.is_public(category_id)]

    def visible_children(self, category_id):
        return [r for r in self.children.get(category_id, []) if self.is_public(r["id"])]

    def subtree_ids(self, slug, public_only=False):
        """Ids of the category with this slug and everything below it ([] if unknown).

        With ``public_only`` the root must be public and hidden descendants
        are skipped together with everything below them.
        """
        root = self.by_slug.get(slug)
        if root is None or (public_only and not self.is_public(root["id"])):
            return []
        ids, stack = [], [root["id"]]
        while stack:
            current = stack.pop()
            if current in ids:
                continue
            ids.append(current)
            stack.extend(
                r["id"] for r in self.children.get(current, [])
                if not public_only or r["visible_on_website"]
            )
        return ids


def _category_link(row):
    return {"id": str(row["id"]), "slug": row["slug"], "title": row["title"]}


def _image(image_id, variant):
    return bunny_image_url(image_id, variant) or None


# SEO copy the storefront renders verbatim. The ERP is the only source of SEO
# copy (docs/storefront-seo.md): the storefront never writes its own, it only
# falls back to the plain name and description when a field is empty.
PUBLIC_SEO_FIELDS = ["seo_title", "seo_meta_description", "seo_og_title", "seo_og_description"]


class PublicBrandSerializer(serializers.ModelSerializer):
    """A brand as the public storefront sees it (brand pages and index)."""

    logo_url = serializers.SerializerMethodField()
    banner_url = serializers.SerializerMethodField()
    seo_og_image_url = serializers.SerializerMethodField()

    class Meta:
        model = Brand
        fields = [
            "id", "title", "slug", "description", "website_url", "is_authorized_dealer",
            "is_featured_store", "sort_order", "logo_url", "banner_url",
            *PUBLIC_SEO_FIELDS, "seo_og_image_url",
        ]
        read_only_fields = fields

    def get_logo_url(self, brand) -> str | None:
        return _image(brand.logo_image_id, "thumbnail")

    def get_banner_url(self, brand) -> str | None:
        return _image(brand.banner_image_id, "hero")

    def get_seo_og_image_url(self, brand) -> str | None:
        return _image(brand.seo_og_image_id or brand.banner_image_id or brand.logo_image_id, "detail")


class PublicCategorySerializer(serializers.ModelSerializer):
    """A category as the public storefront sees it.

    ``parent_slug`` lets a client rebuild the tree from one list call;
    ``ancestors`` (root first) is the breadcrumb trail and ``children`` the
    visible sub-categories. Both come from the ``category_index`` the
    viewset puts in the context, so a page of categories is one extra query.
    """

    parent_slug = serializers.SerializerMethodField()
    ancestors = serializers.SerializerMethodField()
    children = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    seo_og_image_url = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id", "title", "slug", "parent", "parent_slug", "tree_depth", "sort_order",
            "is_featured_store", "description", "short_description", "body_copy",
            "image_url", *PUBLIC_SEO_FIELDS, "seo_og_image_url", "seo_canonical_url",
            "ancestors", "children",
        ]
        read_only_fields = fields

    def _index(self):
        index = self.context.get("category_index")
        if index is None:
            index = self.context["category_index"] = CategoryIndex.build()
        return index

    def get_parent_slug(self, category) -> str | None:
        index = self._index()
        parent = index.by_id.get(category.parent_id)
        return parent["slug"] if parent and index.is_public(parent["id"]) else None

    def get_ancestors(self, category) -> list[dict[str, str]]:
        # The viewset only serves categories whose whole chain is public; the
        # filter keeps a hidden row out even if this serializer is reused.
        index = self._index()
        return [_category_link(row) for row in index.ancestors(category.id) if index.is_public(row["id"])]

    def _hide_hidden_parent(self, data, category):
        if category.parent_id and not self._index().is_public(category.parent_id):
            data["parent"] = None
        return data

    def get_children(self, category) -> list[dict[str, str]]:
        return [_category_link(row) for row in self._index().visible_children(category.id)]

    def get_image_url(self, category) -> str | None:
        return _image(category.primary_image_id or category.thumbnail_image_id, "card")

    def get_seo_og_image_url(self, category) -> str | None:
        return _image(category.seo_og_image_id or category.primary_image_id, "detail")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Belt and braces: the model refuses draft copy on save, but a row
        # written around clean() (QuerySet.update, a raw import) must still
        # never reach the storefront.
        if body_copy_is_draft(data.get("body_copy")):
            data["body_copy"] = None
        return self._hide_hidden_parent(data, instance)


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class VariantImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = VariantImage
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class ProductOptionValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductOptionValue
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class ProductOptionSerializer(serializers.ModelSerializer):
    values = ProductOptionValueSerializer(many=True, read_only=True)

    class Meta:
        model = ProductOption
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class VariantOptionValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = VariantOptionValue
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class VariantOptionValueBriefSerializer(serializers.ModelSerializer):
    """Minimal serializer for variant->option_value junction, used in list views."""
    option_id = serializers.UUIDField(source='option_value.option_id', read_only=True)
    value = serializers.CharField(source='option_value.value', read_only=True)

    class Meta:
        model = VariantOptionValue
        fields = ["option_value", "option_id", "value"]


class ProductVariantListSerializer(serializers.ModelSerializer):
    option_values = VariantOptionValueBriefSerializer(many=True, read_only=True)

    class Meta:
        model = ProductVariant
        fields = [
            "id", "sku", "name", "retail_price", "sale_price", "msrp_price",
            "map_price", "wholesale_price", "mpn", "gtin",
            "stock", "stock_status", "is_active", "sort_order",
            "primary_image_id", "option_values",
            # Shipping dims — allow variant-level overrides to propagate to D1
            "shipping_weight", "shipping_weight_uom",
            "shipping_length", "shipping_width", "shipping_height",
            "shipping_dimension_uom",
            "ships_usps", "ships_ups", "ships_ltl", "ships_pickup",
            "hazmat_flag", "hazmat_class", "oversized_flag",
        ]


class ProductVariantDetailSerializer(serializers.ModelSerializer):
    images = VariantImageSerializer(many=True, read_only=True)
    option_values = VariantOptionValueSerializer(many=True, read_only=True)

    class Meta:
        model = ProductVariant
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


def is_staff_reader(request) -> bool:
    from core.permissions import is_staff_principal

    return is_staff_principal(getattr(request, "user", None))


def unit_price(variant):
    """A variant's selling price. A zero/blank sale price means "no sale"."""
    return variant.sale_price if variant.sale_price else variant.retail_price


class PublicVariantSerializer(serializers.ModelSerializer):
    """A sellable variant as the public storefront sees it — no costs."""

    option_values = VariantOptionValueBriefSerializer(many=True, read_only=True)
    price = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = [
            "id", "sku", "name", "price", "msrp_price", "stock", "stock_status",
            "primary_image_id", "option_values",
        ]

    def get_price(self, variant):
        price = unit_price(variant)
        return f"{price:.2f}" if price is not None else None


class StorefrontFieldsMixin(serializers.Serializer):
    """Commerce fields the headless storefront renders, on list and detail.

    They come from the product's sellable variants (active and visible on the
    website) and are computed from prefetched rows, so a page of products
    costs no extra queries. Values the ERP doesn't have stay null rather than
    invented: `price` is null when no sellable variant has a price,
    `image_url` is null without a CDN base or image.
    """

    price = serializers.SerializerMethodField()
    stock_quantity = serializers.SerializerMethodField()
    stock_status = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    category_slug = serializers.SerializerMethodField()
    brand_slug = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()

    @staticmethod
    def _sellable(product):
        return [v for v in product.variants.all() if v.is_active and v.visible_on_website]

    def get_price(self, product):
        prices = [unit_price(v) for v in self._sellable(product)]
        prices = [p for p in prices if p is not None]
        return f"{min(prices):.2f}" if prices else None

    def get_stock_quantity(self, product):
        return sum(max(v.stock, 0) for v in self._sellable(product))

    def get_stock_status(self, product):
        statuses = {v.stock_status for v in self._sellable(product)}
        for status in (
            ProductVariant.StockStatus.IN_STOCK,
            ProductVariant.StockStatus.PREORDER,
            ProductVariant.StockStatus.BACKORDER,
        ):
            if status in statuses:
                return status
        return ProductVariant.StockStatus.OUT_OF_STOCK

    def get_summary(self, product):
        text = strip_tags(product.short_description or product.description or "").strip()
        return Truncator(" ".join(text.split())).chars(300) or None

    @staticmethod
    def _display_category(product):
        # Prefer a category the website shows; sort_order breaks ties.
        categories = sorted(product.categories.all(), key=lambda c: (not c.visible_on_website, c.sort_order))
        return categories[0] if categories else None

    def get_category_name(self, product):
        category = self._display_category(product)
        return category.title if category else None

    def get_category_slug(self, product) -> str | None:
        # Only a category the website shows has a page to link to.
        category = self._display_category(product)
        return category.slug if category and category.visible_on_website else None

    def get_brand_slug(self, product) -> str | None:
        brand = product.brand
        return brand.slug if brand and brand.visible_on_website else None

    def get_image_url(self, product):
        return bunny_image_url(product.primary_image_id or product.thumbnail_image_id, "card") or None


STOREFRONT_FIELDS = [
    "price", "stock_quantity", "stock_status", "summary", "category_name", "category_slug",
    "brand_slug", "image_url",
]


class ProductListSerializer(StorefrontFieldsMixin, serializers.ModelSerializer):
    """Catalog row — what the headless storefront renders (see the mixin)."""

    brand_name = serializers.CharField(source="brand.title", read_only=True, default="")

    class Meta:
        model = Product
        fields = [
            "id", "title", "slug", "part_number", "product_type",
            "is_active", "brand", "brand_name",
            "primary_image_id", "thumbnail_image_id",
            *STOREFRONT_FIELDS,
        ]


class ProductDocumentSyncSerializer(serializers.ModelSerializer):
    """Lightweight serializer for syncing documents to D1 storefront."""

    class Meta:
        model = ProductDocument
        fields = [
            "id",
            "document_type",
            "title",
            "r2_key",
            "r2_url",
            "file_size",
            "match_method",
            "match_confidence",
            "is_verified",
        ]


class ProductDetailSerializer(StorefrontFieldsMixin, serializers.ModelSerializer):
    variants = ProductVariantListSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    options = ProductOptionSerializer(many=True, read_only=True)
    documents = ProductDocumentSyncSerializer(many=True, read_only=True)
    brand_name = serializers.CharField(source="brand.title", read_only=True, default="")

    class Meta:
        model = Product
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_safety_warnings(self, value):
        try:
            return validate_safety_warnings(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Public readers (anonymous, shoppers, the storefront identity) get
        # sellable variants only, without wholesale/MAP costs. Staff and the
        # API-key sync keep the full variant rows.
        if not is_staff_reader(self.context.get("request")):
            data["variants"] = PublicVariantSerializer(self._sellable(instance), many=True).data
        return data


class ProductCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class SupplierProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierProduct
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]
