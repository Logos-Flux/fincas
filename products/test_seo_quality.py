"""products.seo_quality: template and truncation checks, rules from settings + data files."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from products import seo_quality as q
from products.models import Brand, Category
from products.seo_checks import seo_copy_warnings

TENANT = Path(__file__).resolve().parent / "test_data" / "seo_patterns_tenant.json"


class DefaultRulesTests(SimpleTestCase):
    """golden alone: no template patterns, tenant-neutral truncation shapes only."""

    def test_defaults_carry_no_template_patterns(self):
        self.assertEqual(q.patterns(), ())
        self.assertEqual(q.patterns_payload(), [])
        self.assertFalse(q.is_boilerplate_seo("Shop Batteries Equipment | Example Store", "title"))

    def test_defaults_name_no_store_or_product_vocabulary(self):
        text = q.DEFAULTS_PATH.read_text(encoding="utf-8").lower()
        data = json.loads(text)
        self.assertEqual(data["patterns"], [])
        self.assertEqual(data["truncation"]["soft_dangling_words"], [])

    def test_cut_off_titles(self):
        for title in ("Deep-Cycle Batteries for Solar, Golf,", "Inverters |", "Batteries for the"):
            self.assertTrue(q.is_cut_off(title, "title"), title)
            self.assertTrue(q.is_unusable(title, "title"), title)
        self.assertFalse(q.is_cut_off("Mounting Rails 168 in", "title"))

    def test_cut_off_metas(self):
        for meta in ("Batteries with expert support, and.", "Inverters for homes, cabins,.",
                     "Shop panels at the store with."):
            self.assertTrue(q.is_cut_off(meta, "meta"), meta)
        self.assertFalse(q.is_cut_off("Grid-tie inverter with rapid shutdown built in.", "meta"))

    def test_soft_truncation_is_replaceable_but_not_unusable(self):
        meta = "Hybrid inverter, 48V, 15kW output"  # no closing punctuation
        self.assertTrue(q.is_truncated(meta, "meta"))
        self.assertFalse(q.is_cut_off(meta, "meta"))
        self.assertFalse(q.is_unusable(meta, "meta"))
        self.assertTrue(q.is_replaceable(meta, "meta"))

    def test_real_copy_is_kept(self):
        for kind, text in (("title", "Solar Batteries | Lithium, AGM & Rack-Mounted"),
                           ("meta", "Lithium and AGM batteries for off-grid and backup systems.")):
            self.assertFalse(q.is_replaceable(text, kind), text)
            self.assertEqual(q.problems(text, kind), [])

    def test_empty_values(self):
        for kind in q.KINDS:
            self.assertFalse(q.is_boilerplate_seo("", kind))
            self.assertFalse(q.is_truncated("  ", kind))
            self.assertTrue(q.is_replaceable("", kind))
            self.assertTrue(q.is_replaceable(None, kind))
            self.assertEqual(q.problems("", kind), [])

    def test_bad_kind_raises(self):
        with self.assertRaises(ValueError):
            q.is_boilerplate_seo("x", "seo_title")

    def test_length_is_reported_not_fixed(self):
        long_meta = "A" * 170 + "."
        self.assertTrue(any("over 160" in p for p in q.problems(long_meta, "meta")))
        self.assertFalse(q.is_replaceable(long_meta, "meta"))


@override_settings(GOLDEN_SEO_PATTERN_FILES=[TENANT])
class TenantRulesTests(SimpleTestCase):
    def test_tenant_patterns_are_added(self):
        self.assertEqual([p.id for p in q.patterns()], ["category-title-shop-x", "meta-length-extender"])
        self.assertTrue(q.is_template_seo("Shop Batteries Equipment | Example Store", "title"))
        self.assertTrue(q.is_unusable("Shop Batteries Equipment | Example Store", "title"))

    def test_every_tenant_example_matches(self):
        for p in q.patterns():
            self.assertTrue(p.examples, p.id)
            for example in p.examples:
                for kind in p.applies_to:
                    self.assertTrue(q.is_boilerplate_seo(example, kind), (p.id, example))

    def test_padding_is_replaceable_not_unusable(self):
        meta = "Rugged 48V batteries for cabins. Order online at Example Store for fast shipping."
        self.assertTrue(q.is_boilerplate_seo(meta, "meta"))
        self.assertFalse(q.is_template_seo(meta, "meta"))
        self.assertFalse(q.is_unusable(meta, "meta"))
        self.assertTrue(q.is_replaceable(meta, "meta"))

    def test_tenant_truncation_merges_onto_defaults(self):
        self.assertTrue(q.is_cut_off("Batteries with expert support and fast.", "meta"))  # tenant shape
        self.assertTrue(q.is_cut_off("Batteries with expert support, and.", "meta"))  # golden shape kept
        self.assertTrue(q.is_truncated("Plug and play kit. Ships fast.", "meta"))  # tenant soft word
        self.assertFalse(q.is_cut_off("Plug and play kit. Ships fast.", "meta"))

    def test_payload_mirrors_the_patterns(self):
        payload = q.patterns_payload()
        self.assertEqual([p["id"] for p in payload], [p.id for p in q.patterns()])
        self.assertEqual(payload[0]["flags"], "i")
        self.assertEqual(payload[1]["flags"], "")


class RulesValidationTests(SimpleTestCase):
    def _file(self, data) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, tmp)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def _pattern(self, **overrides):
        return {"id": "p", "applies_to": ["title"], "severity": "template", "pattern": "^x$", **overrides}

    def assertRefused(self, files, message):
        with override_settings(GOLDEN_SEO_PATTERN_FILES=files):
            with self.assertRaisesMessage(ImproperlyConfigured, message):
                q.rules()

    def test_missing_file(self):
        self.assertRefused(["/nonexistent/seo.json"], "no such file")

    def test_single_path_instead_of_a_list(self):
        self.assertRefused(str(TENANT), "must be a list")

    def test_wrong_version(self):
        self.assertRefused([self._file({"version": 2})], '"version": 1')

    def test_duplicate_id_across_files(self):
        self.assertRefused([TENANT, self._file({"version": 1, "patterns": [self._pattern(id="category-title-shop-x")]})],
                           "duplicate pattern id")

    def test_bad_kind_severity_and_flags(self):
        self.assertRefused([self._file({"version": 1, "patterns": [self._pattern(applies_to=["body"])]})], "applies_to")
        self.assertRefused([self._file({"version": 1, "patterns": [self._pattern(severity="loud")]})], "severity")
        self.assertRefused([self._file({"version": 1, "patterns": [self._pattern(flags="m")]})], "flags")

    def test_patterns_that_do_not_mirror_to_javascript(self):
        for pattern in ("(?<=a)b", "(?P<x>a)", "(?i)abc"):
            self.assertRefused([self._file({"version": 1, "patterns": [self._pattern(pattern=pattern)]})],
                               "do not mirror to JS")

    def test_bad_regex(self):
        self.assertRefused([self._file({"version": 1, "patterns": [self._pattern(pattern="(")]})], "bad regex")

    def test_unknown_truncation_key(self):
        self.assertRefused([self._file({"version": 1, "truncation": {"dangling": ["and"]}})],
                           "unknown truncation key")

    def test_relative_paths_resolve_against_base_dir(self):
        with override_settings(BASE_DIR=TENANT.parent, GOLDEN_SEO_PATTERN_FILES=[TENANT.name]):
            self.assertEqual(len(q.patterns()), 2)

    def test_products_app_validates_at_startup(self):
        with override_settings(GOLDEN_SEO_PATTERN_FILES=["/nonexistent/seo.json"]):
            with self.assertRaises(ImproperlyConfigured):
                apps.get_app_config("products").ready()


class SeoCopyWarningsUseTheRulesTests(TestCase):
    """products.seo_checks (Category/Brand save warnings) reads the same rules."""

    def test_connector_ending_warns_as_mid_sentence(self):
        cat = Category.objects.create(
            title="Batteries", seo_meta_description="Lithium batteries with expert technical support, and.")
        text = " ".join(seo_copy_warnings(cat))
        self.assertIn("stops mid-sentence", text)
        self.assertNotIn("full stop", text)

    def test_no_pattern_warning_without_tenant_rules(self):
        cat = Category.objects.create(title="Batteries", seo_title="Shop Batteries Equipment | Example Store")
        self.assertFalse([w for w in seo_copy_warnings(cat) if "generated-copy pattern" in w])

    @override_settings(GOLDEN_SEO_PATTERN_FILES=[TENANT])
    def test_tenant_template_pattern_warns(self):
        brand = Brand.objects.create(title="Acme", seo_title="Shop Acme Equipment | Example Store")
        warnings = [w for w in seo_copy_warnings(brand) if "generated-copy pattern" in w]
        self.assertEqual(len(warnings), 1)
        self.assertIn("'category-title-shop-x' (template)", warnings[0])



class StandaloneScriptTests(SimpleTestCase):
    def test_without_django_settings_it_gets_the_defaults(self):
        import os
        import subprocess
        import sys

        code = ("from products import seo_quality as q; assert q.patterns() == (); "
                "assert q.is_cut_off('Batteries with support, and.', 'meta'); print('ok')")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent.parent,
                             # Keep the interpreter's environment (CI's toolcache Python needs it
                             # to find site-packages); drop only the Django settings module.
                             env={k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"})
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)
