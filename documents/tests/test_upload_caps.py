"""F-048: upload caps on ProductDocumentSerializer.

file_size is capped by settings.DOCUMENT_MAX_UPLOAD_BYTES (env-overridable,
default 25 MB) and r2_key must carry an allowlisted document extension.
"""

from django.conf import settings
from django.test import TestCase
from django.test.utils import override_settings

from documents.serializers import ProductDocumentSerializer
from products.models import Brand, Product


class DocumentUploadCapTests(TestCase):
    def setUp(self):
        brand = Brand.objects.create(title="Enphase", slug="enphase")
        self.product = Product.objects.create(
            title="Enphase IQ8HC Microinverter",
            part_number="IQ8HC-72-M-DOM-US",
            brand=brand,
            slug="enphase-iq8hc",
        )

    def _payload(self, **overrides):
        data = {
            "product": self.product.pk,
            "document_type": "datasheet",
            "title": "IQ8HC datasheet",
            "r2_key": "products/enphase/IQ8HC_datasheet.pdf",
            "r2_url": "https://example.com/d.pdf",
            "file_size": 1024,
        }
        data.update(overrides)
        return data

    def test_file_size_over_cap_rejected(self):
        serializer = ProductDocumentSerializer(
            data=self._payload(file_size=settings.DOCUMENT_MAX_UPLOAD_BYTES + 1)
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("file_size", serializer.errors)

    def test_file_size_at_cap_accepted(self):
        serializer = ProductDocumentSerializer(
            data=self._payload(file_size=settings.DOCUMENT_MAX_UPLOAD_BYTES)
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_cap_follows_settings_override(self):
        with override_settings(DOCUMENT_MAX_UPLOAD_BYTES=1024):
            too_big = ProductDocumentSerializer(data=self._payload(file_size=2048))
            self.assertFalse(too_big.is_valid())
            self.assertIn("file_size", too_big.errors)
            small_enough = ProductDocumentSerializer(data=self._payload(file_size=512))
            self.assertTrue(small_enough.is_valid(), small_enough.errors)

    def test_r2_key_extension_allowlist(self):
        bad = ProductDocumentSerializer(
            data=self._payload(r2_key="products/enphase/setup.exe")
        )
        self.assertFalse(bad.is_valid())
        self.assertIn("r2_key", bad.errors)

        no_extension = ProductDocumentSerializer(
            data=self._payload(r2_key="products/enphase/README")
        )
        self.assertFalse(no_extension.is_valid())
        self.assertIn("r2_key", no_extension.errors)

    def test_r2_key_allowed_extensions_accepted(self):
        for key in (
            "products/enphase/spec.pdf",
            "products/enphase/manual.DOCX",
            "products/enphase/ratings.csv",
        ):
            serializer = ProductDocumentSerializer(data=self._payload(r2_key=key))
            self.assertTrue(serializer.is_valid(), (key, serializer.errors))
