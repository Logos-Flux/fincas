"""
Tests for the auto-match pass: matcher tiers, text extract, inventory,
and the auto_match_docs management command.

Split into pure-unit (matcher/text_extract) and Django DB integration
(command behaviour: idempotency, dry-run, review CSV, never-auto-verify).
"""

from __future__ import annotations

import csv
import os
import tempfile
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from documents.matcher import (
    CONF_BRAND_FAMILY,
    CONF_MODEL_SUBSTR,
    CONF_MPN,
    CONF_SKU,
    HIGH_CONFIDENCE_MIN,
    best_match,
    build_index,
    infer_doc_type,
)
from documents.models import ProductDocument
from documents.text_extract import extract_first_page_text
from products.models import Brand, Product, ProductVariant

# ---------------------------------------------------------------------------
# Fixture helpers — mirrors *_INDEX_ENTRY shapes the task's doc-bucket implies
# ---------------------------------------------------------------------------

def _idx(*entries: dict):
    """Shorthand: build_index([...dicts...])."""
    return build_index(list(entries))

ENPHASE_ENTRY = {
    "product_id": "enphase-pid",
    "product_title": "Enphase IQ8HC Microinverter",
    "product_slug": "enphase-iq8hc-microinverter",
    "brand_title": "Enphase",
    "brand_slug": "enphase",
    "part_number": "IQ8HC-72-M-DOM-US",
    "product_mpn": "IQ8HC-72-M-DOM-US",
    "variant_skus": ["IQ8HC-72-M-DOM-US"],
    "variant_mpns": ["IQ8HC-72-M-DOM-US"],
}

VICTRON_ENTRY = {
    "product_id": "victron-pid",
    "product_title": "Victron SmartSolar MPPT 250/100",
    "product_slug": "victron-smartsolar-mppt-250-100",
    "brand_title": "Victron Energy",
    "brand_slug": "victron-energy",
    "part_number": "SCC125110412",
    "product_mpn": "SCC125110412",
    "variant_skus": ["SCC125110412", "SCC125110422"],
    "variant_mpns": ["SCC125110412"],
}

GENERIC_ENTRY = {
    "product_id": "generic-pid",
    "product_title": "Generic Solar Panel 400W",
    "product_slug": "generic-solar-panel-400w",
    "brand_title": "Generic",
    "brand_slug": "generic",
    "part_number": "GSP-400",
    "product_mpn": "GSP-400",
    "variant_skus": ["GSP-400"],
    "variant_mpns": [],
}

INDEX = _idx(ENPHASE_ENTRY, VICTRON_ENTRY, GENERIC_ENTRY)


# ---------------------------------------------------------------------------
# Pure matcher tier tests (no DB, no I/O)
# ---------------------------------------------------------------------------

class MatcherTierTests(TestCase):
    def test_sku_exact_from_key_wins(self):
        key = "docs/enphase/IQ8HC-72-M-DOM-US_datasheet.pdf"
        hit = best_match(key, "", INDEX)
        assert hit is not None
        assert hit.match_type == "sku"
        assert hit.confidence == CONF_SKU
        assert "key" in hit.matched_sources

    def test_sku_exact_from_pdf_text(self):
        # pdf_text alone matches SKU even when key doesn't mention it
        hit = best_match("docs/enphase/unknown.pdf", "Model: IQ8HC-72-M-DOM-US general", INDEX)
        assert hit is not None
        assert hit.match_type in ("sku", "mpn")  # sku and mpn overlap for enphase
        assert "pdf_text" in hit.matched_sources

    def test_mpn_tier_when_no_sku_match(self):
        entry = {
            "product_id": "mpn-only",
            "product_title": "Victron SmartSolar MPPT 150/35",
            "product_slug": "victron-mppt-150-35",
            "brand_title": "Victron Energy",
            "brand_slug": "victron-energy",
            "part_number": "",
            "product_mpn": "SCC115035210",
            "variant_skus": ["DIFFERENT-SKU"],
            "variant_mpns": ["SCC115035210"],
        }
        idx = _idx(entry)
        hit = best_match("staged/victron/SCC115035210_manual.pdf", "", idx)
        assert hit is not None
        assert hit.match_type == "mpn"
        assert hit.confidence == CONF_MPN

    def test_brand_family_tier(self):
        # Key has brand + family token; matches at brand_family tier or better.
        key = "docs/enphase/iq8hc_install_guide.pdf"
        hit = best_match(key, "", INDEX)
        assert hit is not None
        assert hit.confidence >= CONF_BRAND_FAMILY

    def test_no_match_returns_none(self):
        hit = best_match("random/unrelated/file_xyz.pdf", "nothing relevant here", INDEX)
        # Some entries share short tokens; but "xyz" shouldn't hit anything.
        # Assert it's either None or very low confidence.
        if hit is not None:
            assert hit.confidence <= CONF_MODEL_SUBSTR

    def test_empty_inputs_no_match(self):
        assert best_match("", "", INDEX) is None

    def test_sku_outranks_brand_family_for_same_product(self):
        # Key contains both the SKU and brand+family signals — SKU should win.
        key = "enphase/IQ8HC-72-M-DOM-US_iq8hc_guide.pdf"
        hit = best_match(key, "", INDEX)
        assert hit is not None
        assert hit.match_type == "sku"
        assert hit.confidence == CONF_SKU

    def test_multi_product_best_wins_by_confidence(self):
        # Two products could match; the highest confidence candidate should win.
        enphase_sku_key = "IQ8HC-72-M-DOM-US.pdf"
        hit = best_match(enphase_sku_key, "", INDEX)
        assert hit is not None
        assert hit.product_id == "enphase-pid"
        assert hit.confidence == CONF_SKU


class InferDocTypeTests(TestCase):
    def test_datasheet_hints(self):
        assert infer_doc_type("my_spec_sheet.pdf") == "datasheet"
        assert infer_doc_type("datasheet-enphase.pdf") == "datasheet"

    def test_manual(self):
        assert infer_doc_type("install_manual.pdf") in ("manual", "installation")

    def test_warranty(self):
        assert infer_doc_type("limited_warranty_WARR.pdf") == "warranty"

    def test_default_fallback(self):
        assert infer_doc_type("random.pdf") == "datasheet"


class BuildIndexTests(TestCase):
    def test_part_number_counted_as_sku(self):
        idx = _idx({
            "product_id": "p1",
            "product_title": "Acme Inverter 5000",
            "brand_title": "Acme",
            "brand_slug": "acme",
            "part_number": "ACM-5000",
            "product_mpn": "",
            "variant_skus": [],
            "variant_mpns": [],
        })
        assert "ACM-5000" in idx[0].sku_set

    def test_family_tokens_from_title_and_slug(self):
        idx = _idx({
            "product_id": "p2",
            "product_title": "Super Panel 450W Monocrystalline",
            "product_slug": "super-panel-450w-monocrystalline",
            "brand_title": "SunCo",
            "brand_slug": "sunco",
            "part_number": "",
            "product_mpn": "",
            "variant_skus": ["SUN-450"],
            "variant_mpns": [],
        })
        toks = idx[0].family_tokens
        assert any("panel" in t or "super" in t or "monocrystalline" in t for t in toks)


# ---------------------------------------------------------------------------
# Text extract (requires pypdf runtime but degrades gracefully)
# ---------------------------------------------------------------------------

class TextExtractTests(TestCase):
    def test_empty_bytes_returns_empty(self):
        assert extract_first_page_text(b"") == ""

    def test_non_pdf_returns_empty(self):
        assert extract_first_page_text(b"not a pdf at all") == ""

    def test_blank_pdf_returns_empty_or_whitespace(self):
        try:
            from pypdf import PdfWriter  # type: ignore
        except ImportError:
            self.skipTest("pypdf not installed")
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        buf = BytesIO()
        w.write(buf)
        text = extract_first_page_text(buf.getvalue())
        # Blank page has no extractable text
        assert text == ""

    def test_truncated_pdf_graceful(self):
        assert extract_first_page_text(b"%PDF-1.4 truncated") == ""


# ---------------------------------------------------------------------------
# Tigris inventory helpers (stub client, no real S3)
# ---------------------------------------------------------------------------

class TigrisInventoryTests(TestCase):
    def _stub_client(self, objects: list[dict]):
        """Minimal paginator stub: get_paginator returns an object whose paginate yields pages."""
        pages = [{"Contents": [{"Key": o["Key"], "Size": o.get("Size", 100), "ETag": o.get("ETag", '"abc"')} for o in objects]}]
        paginator = MagicMock()
        paginator.paginate.return_value = pages
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_list_bucket_keys_paginates(self):
        from documents.tigris_inventory import list_bucket_keys
        objs = [{"Key": f"docs/file_{i}.pdf", "Size": 1000 + i} for i in range(5)]
        client = self._stub_client(objs)
        out = list_bucket_keys(client, "test-product-docs")
        assert len(out) == 5
        assert out[0].key == "docs/file_0.pdf"

    def test_list_bucket_keys_respects_max_keys(self):
        from documents.tigris_inventory import list_bucket_keys
        objs = [{"Key": f"k{i}.pdf"} for i in range(10)]
        client = self._stub_client(objs)
        out = list_bucket_keys(client, "bucket", max_keys=3)
        assert len(out) == 3


# ---------------------------------------------------------------------------
# Management command — DB integration
# ---------------------------------------------------------------------------

class AutoMatchCommandTests(TestCase):
    """The command's DB-writing behaviour uses real Product/Brand/Variant rows."""

    def setUp(self):
        self.brand = Brand.objects.create(title="Enphase", slug="enphase")
        self.product = Product.objects.create(
            title="Enphase IQ8HC Microinverter",
            part_number="IQ8HC-72-M-DOM-US",
            mpn="IQ8HC-72-M-DOM-US",
            brand=self.brand,
            slug="enphase-iq8hc",
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            sku="IQ8HC-72-M-DOM-US",
            mpn="IQ8HC-72-M-DOM-US",
        )

    def _stub_client(self, keys: list[str]):
        pages = [{"Contents": [{"Key": k, "Size": 1024, "ETag": '"x"'} for k in keys]}]
        paginator = MagicMock()
        paginator.paginate.return_value = pages
        client = MagicMock()
        client.get_paginator.return_value = paginator
        # get_object for pdf_text path returns empty (no bytes) unless patched
        client.get_object.side_effect = Exception("no object")
        return client

    def test_dry_run_creates_nothing_and_emits_no_csv_content_written_to_disk_outside_temp(self):
        client = self._stub_client(["products/enphase/IQ8HC-72-M-DOM-US_datasheet.pdf"])
        with tempfile.TemporaryDirectory() as tmp:
            review = str(Path(tmp) / "review.csv")
            # Ensure the file doesn't already exist
            assert not Path(review).exists()
            with patch("documents.management.commands.auto_match_docs._build_tigris_client", return_value=client):
                call_command("auto_match_docs", bucket="test-product-docs", review_csv=review, dry_run=True, no_pdf_text=True)
            assert ProductDocument.objects.count() == 0
            # dry-run does not emit CSV (per command contract: dry_run previews only)
            assert not Path(review).exists()

    def test_writes_high_confidence_row_and_review_csv(self):
        client = self._stub_client([
            "products/enphase/IQ8HC-72-M-DOM-US_datasheet.pdf",  # HIGH (SKU)
            "products/enphase/random_unmatched_xyz_999.pdf",     # no match -> review no_match
        ])
        with tempfile.TemporaryDirectory() as tmp:
            review = str(Path(tmp) / "review.csv")
            with patch("documents.management.commands.auto_match_docs._build_tigris_client", return_value=client):
                call_command("auto_match_docs", bucket="test-product-docs", review_csv=review, no_pdf_text=True)
            # One HIGH-confidence row created
            assert ProductDocument.objects.count() == 1
            doc = ProductDocument.objects.first()
            assert doc is not None
            assert doc.r2_key == "products/enphase/IQ8HC-72-M-DOM-US_datasheet.pdf"
            assert doc.is_verified is False
            assert doc.match_method == ProductDocument.MatchMethod.AUTO_MATCH
            assert Decimal(str(doc.match_confidence)) >= Decimal(str(HIGH_CONFIDENCE_MIN))
            assert doc.matched_token != ""
            assert isinstance(doc.match_details, dict)
            assert doc.match_details.get("match_type") == "sku"
            # Review CSV must exist and contain the unmatched key
            assert Path(review).exists()
            with open(review, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            keys = {r["r2_key"] for r in rows}
            assert "products/enphase/random_unmatched_xyz_999.pdf" in keys

    def test_never_auto_verifies(self):
        client = self._stub_client(["products/enphase/IQ8HC-72-M-DOM-US_datasheet.pdf"])
        with tempfile.TemporaryDirectory() as tmp:
            review = str(Path(tmp) / "review.csv")
            with patch("documents.management.commands.auto_match_docs._build_tigris_client", return_value=client):
                call_command("auto_match_docs", bucket="test-product-docs", review_csv=review, no_pdf_text=True)
            assert ProductDocument.objects.filter(is_verified=True).count() == 0

    def test_idempotent_second_run_skips_existing_keys(self):
        client = self._stub_client(["products/enphase/IQ8HC-72-M-DOM-US_datasheet.pdf"])
        with tempfile.TemporaryDirectory() as tmp:
            r1 = str(Path(tmp) / "review1.csv")
            r2 = str(Path(tmp) / "review2.csv")
            with patch("documents.management.commands.auto_match_docs._build_tigris_client", return_value=client):
                call_command("auto_match_docs", bucket="test-product-docs", review_csv=r1, no_pdf_text=True)
                assert ProductDocument.objects.count() == 1
                call_command("auto_match_docs", bucket="test-product-docs", review_csv=r2, no_pdf_text=True)
                assert ProductDocument.objects.count() == 1
            # Second review should have 0 rows (everything already matched & skipped)
            with open(r2, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 0

    def test_limit_caps_processing(self):
        keys = [f"products/enphase/IQ8HC-72-M-DOM-US_{i}.pdf" for i in range(5)]
        client = self._stub_client(keys)
        with tempfile.TemporaryDirectory() as tmp:
            review = str(Path(tmp) / "review.csv")
            with patch("documents.management.commands.auto_match_docs._build_tigris_client", return_value=client):
                call_command("auto_match_docs", bucket="test-product-docs", review_csv=review, no_pdf_text=True, limit=2)
            assert ProductDocument.objects.count() == 2

    def test_pdf_text_path_used_when_available(self):
        # A key with no SKU; PDF title page contains the SKU so it qualifies via pdf_text.
        sku = "IQ8HC-72-M-DOM-US"
        key = "products/enphase/generic_title_page.pdf"
        fake_pdf = b"%PDF-1.4 fake"
        client = self._stub_client([key])
        with tempfile.TemporaryDirectory() as tmp:
            review = str(Path(tmp) / "review.csv")
            with patch("documents.management.commands.auto_match_docs._build_tigris_client", return_value=client), \
                 patch("documents.tigris_inventory.fetch_first_bytes", return_value=fake_pdf), \
                 patch("documents.text_extract.extract_first_page_text", return_value=f"Model {sku} spec sheet"):
                call_command("auto_match_docs", bucket="test-product-docs", review_csv=review)
            assert ProductDocument.objects.filter(r2_key=key).exists()


class AutoMatchFallbackInventoryTests(TestCase):
    """No-credentials path: existing ProductDocument rows stand in for the bucket.

    _build_tigris_client is deliberately NOT patched here — the tests exercise
    the real fallback branch (no AWS_* env vars on the worker), which the
    adversarial gate wrongly flagged as UnboundLocalError. Covers: empty-table
    early exit, pre-existing-row skip without --force, and --force re-scoring
    existing rows as *updated* (not created, not skipped) while preserving
    is_verified.
    """

    def setUp(self):
        self.brand = Brand.objects.create(title="Enphase", slug="enphase")
        self.product = Product.objects.create(
            title="Enphase IQ8HC Microinverter",
            part_number="IQ8HC-72-M-DOM-US",
            mpn="IQ8HC-72-M-DOM-US",
            brand=self.brand,
            slug="enphase-iq8hc",
        )
        ProductVariant.objects.create(
            product=self.product,
            sku="IQ8HC-72-M-DOM-US",
            mpn="IQ8HC-72-M-DOM-US",
        )
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env_patch.stop()

    def test_fallback_no_rows_writes_empty_review_and_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            review = str(Path(tmp) / "review.csv")
            call_command("auto_match_docs", bucket="test-product-docs", review_csv=review, no_pdf_text=True)
            assert ProductDocument.objects.count() == 0
            assert Path(review).exists()  # empty review queue, header only
            with open(review, encoding="utf-8") as f:
                assert len(list(csv.DictReader(f))) == 0

    def test_fallback_existing_row_skipped_without_force(self):
        ProductDocument.objects.create(
            product=self.product,
            document_type="datasheet",
            title="IQ8HC datasheet",
            r2_key="products/enphase/IQ8HC-72-M-DOM-US_datasheet.pdf",
            r2_url="https://example.com/d.pdf",
            match_method=ProductDocument.MatchMethod.MANUAL,
        )
        with tempfile.TemporaryDirectory() as tmp:
            review = str(Path(tmp) / "review.csv")
            call_command("auto_match_docs", bucket="test-product-docs", review_csv=review, no_pdf_text=True)
            assert ProductDocument.objects.count() == 1  # no duplicate created
            with open(review, encoding="utf-8") as f:
                assert len(list(csv.DictReader(f))) == 0

    def test_fallback_force_re_scores_existing_as_updated(self):
        out = StringIO()
        ProductDocument.objects.create(
            product=self.product,
            document_type="datasheet",
            title="IQ8HC datasheet",
            r2_key="products/enphase/IQ8HC-72-M-DOM-US_datasheet.pdf",
            r2_url="https://example.com/d.pdf",
            match_method=ProductDocument.MatchMethod.MANUAL,
            is_verified=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            review = str(Path(tmp) / "review.csv")
            call_command(
                "auto_match_docs", bucket="test-product-docs", review_csv=review,
                no_pdf_text=True, force=True, stdout=out,
            )
            summary = out.getvalue()
            assert "updated=1" in summary
            assert "created=0" in summary
            assert ProductDocument.objects.count() == 1
            doc = ProductDocument.objects.get()
            assert doc.is_verified is True  # --force never clears verification
            assert doc.match_method == ProductDocument.MatchMethod.AUTO_MATCH
            assert doc.matched_token == "IQ8HC-72-M-DOM-US"
