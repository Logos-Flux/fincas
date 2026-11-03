from django.db import models

from core.models import BaseModel


class Article(BaseModel):
    """Blog article / content piece."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    title = models.CharField(max_length=255)
    # max_length=255, not SlugField's default 50 — real blog slugs are long SEO
    # URLs (the migrated posts run to ~90 chars).
    slug = models.SlugField(max_length=255, unique=True)
    content = models.TextField()
    excerpt = models.TextField(blank=True)
    # Free-text byline shown on the storefront (e.g. "Lucy Lin Markus"). Blog
    # authors are not system users, so this is the display author. The `author`
    # FK below is the optional "owned by" staff account.
    author_name = models.CharField(max_length=255, blank=True)
    author = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True,
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    # Full URL/key the storefront renders for the hero/card image.
    featured_image_url = models.CharField(max_length=500, blank=True)
    tags = models.JSONField(default=list, blank=True)

    # SEO
    seo_title = models.CharField(max_length=255, blank=True)
    seo_meta_description = models.TextField(blank=True)
    meta_keywords = models.CharField(max_length=255, blank=True)

    # Legacy BigCommerce provenance (bc_id / bc_url / bc_thumbnail_url) lives here.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-published_at"]

    def __str__(self):
        return self.title
