from django.apps import AppConfig


class ProductsConfig(AppConfig):
    name = "products"

    def ready(self):
        # GOLDEN_SEO_PATTERN_FILES is validated at startup (ImproperlyConfigured),
        # not on the first check that happens to read it. No database access.
        from . import seo_quality

        seo_quality.rules()
