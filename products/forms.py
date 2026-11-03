import json

from django import forms
from django.db import models
from django.forms import inlineformset_factory

from .models import Brand, Category, Product, ProductVariant, validate_safety_warnings
from .seo_checks import (
    SEO_CANONICAL_HELP,
    SEO_META_HELP,
    SEO_OG_DESCRIPTION_HELP,
    SEO_OG_TITLE_HELP,
    SEO_TITLE_HELP,
)

SEO_HELP_TEXTS = {
    "seo_title": SEO_TITLE_HELP,
    "seo_meta_description": SEO_META_HELP,
    "seo_og_title": SEO_OG_TITLE_HELP,
    "seo_og_description": SEO_OG_DESCRIPTION_HELP,
}


class JSONWidget(forms.Textarea):
    """Textarea that pretty-prints JSON on render."""

    def format_value(self, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(value, (dict, list)):
            return json.dumps(value, indent=2)
        return value


class BrandForm(forms.ModelForm):
    class Meta:
        model = Brand
        exclude = ["id", "created_at", "updated_at", "slug"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "seo_meta_description": forms.Textarea(attrs={"rows": 3}),
            "seo_og_description": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = SEO_HELP_TEXTS


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        exclude = ["id", "created_at", "updated_at", "slug", "tree_depth"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "short_description": forms.Textarea(attrs={"rows": 2}),
            "body_copy": forms.Textarea(attrs={"rows": 16}),
            "seo_meta_description": forms.Textarea(attrs={"rows": 3}),
            "seo_og_description": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {**SEO_HELP_TEXTS, "seo_canonical_url": SEO_CANONICAL_HELP}


class ProductForm(forms.ModelForm):
    def clean_safety_warnings(self):
        # An emptied JSON box binds as None; the column is NOT NULL.
        value = self.cleaned_data.get("safety_warnings")
        return validate_safety_warnings([] if value is None else value)

    def clean(self):
        cleaned = super().clean()
        # Same for every other NOT NULL JSON field: an emptied box means "no
        # entries", i.e. the field's default ([] or {}), never SQL NULL.
        for field in Product._meta.concrete_fields:
            if (isinstance(field, models.JSONField) and not field.null
                    and field.name in cleaned and cleaned[field.name] is None):
                cleaned[field.name] = field.get_default()
        return cleaned

    class Meta:
        model = Product
        exclude = [
            "id", "created_at", "updated_at", "slug", "company",
            # M2M fields handled separately
            "categories", "related_products", "featured_categories",
        ]
        widgets = {
            # Description
            "description": forms.Textarea(attrs={"rows": 6}),
            "short_description": forms.Textarea(attrs={"rows": 3}),
            # SEO
            "seo_meta_description": forms.Textarea(attrs={"rows": 3}),
            "seo_og_description": forms.Textarea(attrs={"rows": 3}),
            "seo_keywords": forms.Textarea(attrs={"rows": 2}),
            "seo_related_searches": forms.Textarea(attrs={"rows": 2}),
            "seo_use_cases": forms.Textarea(attrs={"rows": 2}),
            # JSON fields
            "specifications": JSONWidget(attrs={"rows": 6}),
            "structured_specs": JSONWidget(attrs={"rows": 6}),
            "unstructured_specs": JSONWidget(attrs={"rows": 4}),
            "aggregate_spec_table": JSONWidget(attrs={"rows": 6}),
            "safety_warnings": JSONWidget(attrs={"rows": 4}),
            "seo_faqs": JSONWidget(attrs={"rows": 4}),
            "seo_schema_json": JSONWidget(attrs={"rows": 4}),
            "seo_image_meta": JSONWidget(attrs={"rows": 4}),
            "seo_cross_link_hints": JSONWidget(attrs={"rows": 4}),
            "gmc_additional_image_links": JSONWidget(attrs={"rows": 3}),
            # DateTime
            "seo_last_optimized": forms.DateTimeInput(
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
        }


class ProductVariantForm(forms.ModelForm):
    class Meta:
        model = ProductVariant
        exclude = [
            "id", "created_at", "updated_at", "product",
        ]
        widgets = {
            "gmc_additional_image_links": JSONWidget(attrs={"rows": 2}),
        }


ProductVariantFormSet = inlineformset_factory(
    Product,
    ProductVariant,
    form=ProductVariantForm,
    extra=1,
    can_delete=True,
)
