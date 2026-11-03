import re
import unicodedata

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

from core.managers import CompanyScopedManager
from core.models import BaseModel


class Brand(BaseModel):
    """Manufacturer / brand (71 records)."""

    title = models.CharField(max_length=255)
    # blank=True lets DRF/admin creators omit slug; save() auto-derives from title.
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    visible_on_website = models.BooleanField(default=True)
    is_featured_store = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    description = models.TextField(blank=True)
    website_url = models.URLField(blank=True)
    is_authorized_dealer = models.BooleanField(default=False)
    # Images (CF Images IDs)
    logo_image_id = models.CharField(max_length=64, blank=True)
    banner_image_id = models.CharField(max_length=64, blank=True)
    # SEO
    seo_title = models.CharField(max_length=255, blank=True)
    seo_meta_description = models.TextField(blank=True)
    seo_og_title = models.CharField(max_length=255, blank=True)
    seo_og_description = models.TextField(blank=True)
    seo_og_image_id = models.CharField(max_length=64, blank=True)
    # Migration provenance
    bc_id = models.CharField(max_length=64, blank=True, db_index=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)
    d1_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["sort_order", "title"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)
            slug = base
            n = 1
            while Brand.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)


# Body-copy publishing guard. Category.body_copy is long-form page copy that a
# storefront renders as-is, so copy still carrying review metadata must never
# be saved. Shared inputs live in products/test_data/body_copy_draft_cases.json;
# a storefront that re-checks the copy before rendering should run them too.
#
# It is an allow-list, not a draft blocklist. Copy is refused when:
#   1. any line looks like a `status:` line mentioning "draft" (any case,
#      "drafted", "DRAFT_v2", leading spaces or emphasis), or
#   2. it starts with a `---` front-matter block of any kind, or
#   3. it starts with a metadata block (a first `key: value` line whose key is
#      lower-case or a known metadata key, down to the first blank line),
#      unless every line in it is `slug:`/`status:` and every status value is
#      exactly `approved`.
# Ordinary prose such as "Note: ..." or "Order status: we draft a quote ..." at
# the top is not metadata. Detection runs on normalised text: CR, CRLF, U+2028,
# U+2029 and U+0085 become newlines, NFKC folds fullwidth colons, NBSP and the
# like, and format characters (zero-width spaces, BOM, soft hyphen) are removed.
BODY_COPY_DRAFT_RE = re.compile(r"^\s*[*_]*status[*_]*\s*:.*draft", re.IGNORECASE | re.MULTILINE)
_BODY_COPY_META_KEY_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$")
_BODY_COPY_KNOWN_META_KEYS = frozenset(
    {"slug", "status", "state", "draft", "title", "review_status", "review-status", "approval", "published"}
)
_BODY_COPY_LINE_BREAKS = re.compile("\r\n|[\r\u2028\u2029\u0085]")

BODY_COPY_NOT_APPROVED_MESSAGE = (
    "This copy is not approved for publication: it still has a DRAFT status line, "
    "or metadata at the top (a '---' block, or 'slug:'/'status:' lines that are not "
    "exactly 'status: approved'). The storefront never shows such copy. Once it has "
    "been reviewed, delete those lines and save again."
)


def _body_copy_detection_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", _BODY_COPY_LINE_BREAKS.sub("\n", text))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _body_copy_start(lines: list[str]) -> int | None:
    """Index of the first copy line, or None if the copy must be refused."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i == len(lines):
        return i
    if re.match(r"\s*---", lines[i]):
        return None
    first = _BODY_COPY_META_KEY_LINE.match(lines[i])
    if not first or not (
        first.group(1) == first.group(1).lower() or first.group(1).lower() in _BODY_COPY_KNOWN_META_KEYS
    ):
        return i  # ordinary prose
    approved = False
    j = i
    while j < len(lines) and lines[j].strip():
        m = _BODY_COPY_META_KEY_LINE.match(lines[j])
        if not m:
            return None
        key = m.group(1).lower()
        if key == "status":
            if m.group(2).strip().lower() != "approved":
                return None
            approved = True
        elif key != "slug":
            return None
        j += 1
    return j if approved else None


def body_copy_is_draft(text: str | None) -> bool:
    """True when body copy must not be published (see the rules above)."""
    if not text:
        return False
    detect = _body_copy_detection_text(text)
    return bool(BODY_COPY_DRAFT_RE.search(detect)) or _body_copy_start(detect.split("\n")) is None


class Category(BaseModel):
    """Product category tree (69 records)."""

    title = models.CharField(max_length=255)
    # blank=True lets DRF/admin creators omit slug; save() auto-derives from title.
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="children",
    )
    tree_depth = models.PositiveSmallIntegerField(default=0)
    sort_order = models.IntegerField(default=0)
    visible_on_website = models.BooleanField(default=True)
    is_featured_store = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    short_description = models.CharField(max_length=500, blank=True)
    # Long-form page body. Separate from `description`, which stays the short
    # meta-description text. Null = the storefront renders no block.
    body_copy = models.TextField(
        null=True,
        blank=True,
        help_text=(
            "Long-form copy shown in an 'About this category' block below the "
            "product grid. Plain markdown: paragraphs, ## / ### headings, '- ' "
            "lists, **bold** and [links](/relative/url/). Leave empty to show nothing."
        ),
    )
    # Images
    primary_image_id = models.CharField(max_length=64, blank=True)
    thumbnail_image_id = models.CharField(max_length=64, blank=True)
    # SEO
    seo_title = models.CharField(max_length=255, blank=True)
    seo_meta_description = models.TextField(blank=True)
    seo_og_title = models.CharField(max_length=255, blank=True)
    seo_og_description = models.TextField(blank=True)
    seo_og_image_id = models.CharField(max_length=64, blank=True)
    seo_canonical_url = models.URLField(blank=True)
    # Migration provenance
    bc_id = models.CharField(max_length=64, blank=True, db_index=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)
    d1_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["sort_order", "title"]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if body_copy_is_draft(self.body_copy):
            raise ValidationError({"body_copy": BODY_COPY_NOT_APPROVED_MESSAGE})

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)
            slug = base
            n = 1
            while Category.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        # Form widgets submit "" for an emptied textarea (TextField, unlike
        # CharField, does not map blank to None); store "no copy" as NULL.
        if self.body_copy is not None and not self.body_copy.strip():
            self.body_copy = None
        if self.parent:
            self.tree_depth = self.parent.tree_depth + 1
        else:
            self.tree_depth = 0
        super().save(*args, **kwargs)


def validate_safety_warnings(value) -> list:
    """``safety_warnings`` must be a list of warning dicts. Returns it unchanged."""
    if not isinstance(value, list) or not all(isinstance(w, dict) for w in value):
        raise ValidationError(
            "Safety warnings must be a JSON list of objects, e.g. "
            '[{"type": "prop65", "text": "...", "evidence_url": "..."}].'
        )
    return value


class Product(BaseModel):
    """Product parent (~99 fields). Variants hold SKUs and prices."""

    class ProductType(models.TextChoices):
        PHYSICAL = "physical", "Physical"
        DIGITAL = "digital", "Digital"
        SERVICE = "service", "Service"
        KIT = "kit", "Kit"
        BOM = "bom", "Bill of Materials"

    # Core identity
    part_number = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=255)
    # blank=True lets DRF/admin creators omit slug; save() auto-derives from title.
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    product_type = models.CharField(
        max_length=20, choices=ProductType.choices, default=ProductType.PHYSICAL,
    )
    is_active = models.BooleanField(default=True)
    visible_on_website = models.BooleanField(default=True)
    has_variants = models.BooleanField(default=False)

    # Relationships
    brand = models.ForeignKey(
        Brand, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="products",
    )
    categories = models.ManyToManyField(Category, through="ProductCategory", blank=True)
    related_products = models.ManyToManyField("self", blank=True)

    # Description
    description = models.TextField(blank=True)
    short_description = models.TextField(blank=True)

    # Display
    is_featured_store = models.BooleanField(default=False)
    featured_categories = models.ManyToManyField(
        Category, blank=True, related_name="featured_products",
    )
    search_boost = models.FloatField(default=1.0)

    # Images (CF Images IDs)
    primary_image_id = models.CharField(max_length=64, blank=True)
    thumbnail_image_id = models.CharField(max_length=64, blank=True)

    # Identifiers (inherited by variants)
    gtin = models.CharField(max_length=50, blank=True)
    mpn = models.CharField(max_length=100, blank=True)

    # Specifications
    specifications = models.JSONField(default=dict, blank=True)
    # Structured specs, validated against a category schema by the
    # products.validate_structured_specs hook when one is installed (core/hook_registry.py)
    structured_specs = models.JSONField(default=dict, blank=True, help_text="Category-specific structured specs. Keys match spec_schemas registry.")
    # Unstructured specs — catch-all for unusual/unknown fields
    unstructured_specs = models.JSONField(default=dict, blank=True, help_text="Specs not in the category schema. Free-form key/value pairs.")
    # Aggregate comparison table for variants (parent-level)
    aggregate_spec_table = models.JSONField(default=list, blank=True, help_text="Variant comparison table. List of {spec_key, unit, values: {variant_sku: value}} dicts.")
    # Accepted safety warnings / disclosures (Prop 65 and similar), each with
    # its evidence. Never stored under certifications. Each entry:
    # {"type", "text", "evidence_url", "reviewed_by", "reviewed_at"}.
    safety_warnings = models.JSONField(default=list, blank=True, help_text="Accepted safety warnings (e.g. Prop 65) with evidence. Never certifications.")

    # Ratings (parent only)
    rating_value = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    rating_count = models.IntegerField(default=0)

    # Item weight & dimensions (inherited)
    item_weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_weight_uom = models.CharField(max_length=5, default="lb")
    item_length = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_width = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_height = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_dimension_uom = models.CharField(max_length=5, default="in")

    # Shipping weight & dimensions (inherited)
    shipping_weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    shipping_weight_uom = models.CharField(max_length=5, default="lb")
    shipping_length = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    shipping_width = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    shipping_height = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    shipping_dimension_uom = models.CharField(max_length=5, default="in")

    # Shipping flags (inherited — null = inherit parent)
    ships_usps = models.BooleanField(null=True, blank=True)
    ships_ups = models.BooleanField(null=True, blank=True)
    ships_ltl = models.BooleanField(null=True, blank=True)
    ships_pickup = models.BooleanField(null=True, blank=True)
    free_shipping = models.BooleanField(null=True, blank=True, default=False, help_text="This item ships free (no shipping charge at checkout)")
    hazmat_flag = models.BooleanField(null=True, blank=True)
    hazmat_class = models.CharField(max_length=50, blank=True)
    oversized_flag = models.BooleanField(null=True, blank=True)

    # UOM (inherited)
    selling_uom = models.CharField(max_length=20, default="each")
    selling_qty_per_uom = models.IntegerField(default=1)
    purchasing_uom = models.CharField(max_length=20, default="each")
    purchasing_qty_per_uom = models.IntegerField(default=1)
    sell_pallet_only = models.BooleanField(default=False)
    purchase_pallet_only = models.BooleanField(default=False)

    # MOQ & Lead time (inherited)
    minimum_order_qty = models.IntegerField(default=1)
    lead_time_days = models.IntegerField(null=True, blank=True)

    # SEO (parent only)
    seo_title = models.CharField(max_length=255, blank=True)
    seo_meta_description = models.TextField(blank=True)
    seo_og_title = models.CharField(max_length=255, blank=True)
    seo_og_description = models.TextField(blank=True)
    seo_keywords = models.TextField(blank=True)
    seo_robots = models.CharField(max_length=50, default="index, follow")
    seo_faqs = models.JSONField(default=list, blank=True)
    seo_related_searches = models.TextField(blank=True)
    seo_use_cases = models.TextField(blank=True)
    seo_last_optimized = models.DateTimeField(null=True, blank=True)
    seo_schema_json = models.JSONField(default=dict, blank=True, help_text="JSON-LD Product schema markup")
    seo_optimization_score = models.IntegerField(null=True, blank=True, help_text="SEO quality score (0-100)")
    seo_image_meta = models.JSONField(default=dict, blank=True, help_text="Breadcrumbs for downstream i2t alt text generation")
    seo_cross_link_hints = models.JSONField(default=dict, blank=True, help_text="Search queries for cross-linking")

    # GMC / Google Merchant Center (inherited)
    gmc_google_category = models.CharField(max_length=255, blank=True)
    gmc_product_type = models.CharField(max_length=255, blank=True)
    gmc_condition = models.CharField(max_length=20, default="new")
    gmc_availability = models.CharField(max_length=20, default="in_stock")
    gmc_shipping_label = models.CharField(max_length=255, blank=True)
    gmc_custom_label_0 = models.CharField(max_length=255, blank=True)
    gmc_custom_label_1 = models.CharField(max_length=255, blank=True)
    gmc_custom_label_2 = models.CharField(max_length=255, blank=True)
    gmc_custom_label_3 = models.CharField(max_length=255, blank=True)
    gmc_custom_label_4 = models.CharField(max_length=255, blank=True)
    gmc_additional_image_links = models.JSONField(default=list, blank=True)
    gmc_energy_efficiency_class = models.CharField(max_length=50, blank=True)
    gmc_age_group = models.CharField(max_length=50, blank=True)
    gmc_multipack = models.IntegerField(null=True, blank=True)
    gmc_is_bundle = models.BooleanField(default=False)

    # Infrastructure
    vectorize_id = models.CharField(max_length=255, blank=True)

    # Legacy system IDs
    bc_id = models.CharField(max_length=64, blank=True, db_index=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)
    d1_id = models.CharField(max_length=64, blank=True, db_index=True)

    # Company scoping
    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, null=True, blank=True,
    )

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)
            slug = base
            n = 1
            while Product.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)


class ProductVariant(BaseModel):
    """Sellable SKU — child of Product. Inherits parent fields when null."""

    class StockStatus(models.TextChoices):
        IN_STOCK = "in_stock", "In Stock"
        OUT_OF_STOCK = "out_of_stock", "Out of Stock"
        PREORDER = "preorder", "Preorder"
        BACKORDER = "backorder", "Backorder"

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    sku = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    visible_on_website = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    # Pricing (variant only)
    retail_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sale_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    msrp_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    map_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    wholesale_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Stock
    stock = models.IntegerField(default=0)
    stock_status = models.CharField(
        max_length=20, choices=StockStatus.choices, default=StockStatus.IN_STOCK,
    )

    # Variant image
    primary_image_id = models.CharField(max_length=64, blank=True)

    # Inherited fields (null = use parent)
    gtin = models.CharField(max_length=50, blank=True)
    mpn = models.CharField(max_length=100, blank=True)

    item_weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_weight_uom = models.CharField(max_length=5, blank=True)
    item_length = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_width = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_height = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    item_dimension_uom = models.CharField(max_length=5, blank=True)

    shipping_weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    shipping_weight_uom = models.CharField(max_length=5, blank=True)
    shipping_length = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    shipping_width = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    shipping_height = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    shipping_dimension_uom = models.CharField(max_length=5, blank=True)

    ships_usps = models.BooleanField(null=True, blank=True)
    ships_ups = models.BooleanField(null=True, blank=True)
    ships_ltl = models.BooleanField(null=True, blank=True)
    ships_pickup = models.BooleanField(null=True, blank=True)
    hazmat_flag = models.BooleanField(null=True, blank=True)
    hazmat_class = models.CharField(max_length=50, blank=True)
    oversized_flag = models.BooleanField(null=True, blank=True)

    selling_uom = models.CharField(max_length=20, blank=True)
    selling_qty_per_uom = models.IntegerField(null=True, blank=True)
    purchasing_uom = models.CharField(max_length=20, blank=True)
    purchasing_qty_per_uom = models.IntegerField(null=True, blank=True)
    sell_pallet_only = models.BooleanField(null=True, blank=True)
    purchase_pallet_only = models.BooleanField(null=True, blank=True)

    minimum_order_qty = models.IntegerField(null=True, blank=True)
    lead_time_days = models.IntegerField(null=True, blank=True)

    # GMC overrides (all nullable — fall back to parent Product via get_effective_field)
    gmc_google_category = models.CharField(max_length=255, blank=True)
    gmc_product_type = models.CharField(max_length=255, blank=True)
    gmc_condition = models.CharField(max_length=20, blank=True)
    gmc_availability = models.CharField(max_length=20, blank=True)
    gmc_shipping_label = models.CharField(max_length=255, blank=True)
    gmc_custom_label_0 = models.CharField(max_length=255, blank=True)
    gmc_custom_label_1 = models.CharField(max_length=255, blank=True)
    gmc_custom_label_2 = models.CharField(max_length=255, blank=True)
    gmc_custom_label_3 = models.CharField(max_length=255, blank=True)
    gmc_custom_label_4 = models.CharField(max_length=255, blank=True)
    gmc_additional_image_links = models.JSONField(default=list, blank=True)
    gmc_energy_efficiency_class = models.CharField(max_length=50, blank=True)
    gmc_multipack = models.IntegerField(null=True, blank=True)
    gmc_is_bundle = models.BooleanField(null=True, blank=True)

    # SEO overrides. Blank = use the parent Product's value: a variant page
    # whose own copy is empty shows the product's seo_title/seo_meta_description.
    seo_title = models.CharField(
        max_length=255, blank=True,
        help_text="Variant-specific page title. Leave blank to use the parent product's.",
    )
    seo_meta_description = models.TextField(
        blank=True,
        help_text="Variant-specific meta description. Leave blank to use the parent product's.",
    )

    class Meta:
        ordering = ["product", "sort_order"]

    def __str__(self):
        return self.sku

    def get_effective_field(self, field_name):
        """CSS cascade: return variant value if set, else parent value."""
        val = getattr(self, field_name, None)
        if val is None or val == "":
            return getattr(self.product, field_name, None)
        return val


class ProductOption(BaseModel):
    """Option dimension for a product (e.g. Voltage, Wattage)."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="options")
    name = models.CharField(max_length=100)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.product} — {self.name}"


class ProductOptionValue(BaseModel):
    """Single value for an option (e.g. 12V, 24V, 48V)."""

    option = models.ForeignKey(ProductOption, on_delete=models.CASCADE, related_name="values")
    value = models.CharField(max_length=100)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.option.name}: {self.value}"


class VariantOptionValue(BaseModel):
    """Junction: which option values a variant has."""

    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name="option_values")
    option_value = models.ForeignKey(ProductOptionValue, on_delete=models.CASCADE)

    class Meta:
        unique_together = [("variant", "option_value")]

    def __str__(self):
        return f"{self.variant.sku} — {self.option_value}"


class ProductImage(BaseModel):
    """Parent-level gallery image (5,398 existing records)."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    cf_image_id = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.product} image {self.sort_order}"


class VariantImage(BaseModel):
    """Variant-specific image."""

    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name="images")
    cf_image_id = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.variant.sku} image {self.sort_order}"


class ProductCategory(BaseModel):
    """M2M junction with is_primary flag for breadcrumbs."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    is_primary = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)

    class Meta:
        unique_together = [("product", "category")]
        verbose_name_plural = "product categories"

    def __str__(self):
        return f"{self.product} → {self.category}"


class SupplierProduct(BaseModel):
    """Multi-vendor pricing junction — variant × supplier."""

    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="supplier_products")
    supplier = models.ForeignKey(
        "purchasing.Supplier", on_delete=models.PROTECT, related_name="supplier_products",
    )
    supplier_sku = models.CharField(max_length=100, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2)
    lead_time_days = models.IntegerField(null=True, blank=True)
    minimum_order_qty = models.IntegerField(default=1)
    purchasing_uom = models.CharField(max_length=20, default="each")
    qty_per_uom = models.IntegerField(default=1)
    shipping_location = models.CharField(max_length=100, blank=True)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    last_quoted_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = [("variant", "supplier")]

    def __str__(self):
        return f"{self.variant.sku} × {self.supplier}"

    @property
    def unit_cost(self):
        """Cost per single unit (cost / qty_per_uom)."""
        if self.qty_per_uom and self.qty_per_uom > 0:
            return self.cost / self.qty_per_uom
        return self.cost
