"""Template tags for building CDN image URLs from cf_image_id values.

CDN URL form:
    <CDN_BASE>/<key>[?optimizer=image&width=<W>&format=auto]

`<key>` is the stored cf_image_id with `/` flattened to `__` and a leading
account hash prefix stripped.
"""

from django import template
from django.conf import settings

register = template.Library()

LEGACY_CF_HASH_PREFIX = "Fdrr4r8cVWsy-JJCR0JU_Q/"

# Variant -> width in pixels. `None`/`'public'` mean "full-size, no transform"
# and so produce a URL with no query string.
VARIANT_WIDTHS = {
    "thumbnail": 200,
    "card": 600,
    "product": 800,
    "detail": 1200,
    "hero": 1600,
    "zoom": 2000,
}


def _to_bunny_key(cf_image_id: str) -> str:
    """Convert a stored cf_image_id to a CDN key."""
    key = cf_image_id
    if key.startswith(LEGACY_CF_HASH_PREFIX):
        key = key[len(LEGACY_CF_HASH_PREFIX):]
    return key.replace("/", "__").replace("#", "%23")


@register.simple_tag
def bunny_image_url(cf_image_id, variant=None):
    """Return a CDN URL for the given cf_image_id."""
    if not cf_image_id:
        return ""

    cdn_base = getattr(settings, "BUNNY_IMAGES_BASE", "").rstrip("/")
    if not cdn_base:
        return ""

    key = _to_bunny_key(str(cf_image_id))
    base = f"{cdn_base}/{key}"

    # Full-size: no transform query string.
    if variant is None or variant == "public":
        return base

    width = VARIANT_WIDTHS.get(variant)
    if width is None:
        return base

    return f"{base}?optimizer=image&width={width}&format=auto"
