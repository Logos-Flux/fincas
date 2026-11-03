"""CRM settings registries (spec 1.1, 1.2): import-time validation, no DB."""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from crm import registries
from crm.registries import (
    BUILTIN_ENGAGEMENT_SOURCES,
    BUILTIN_LOSS_REASONS,
    BUILTIN_TASK_KINDS,
    ENGAGEMENT_SOURCES,
    LOSS_REASONS,
    TASK_KINDS,
    build_default_pipelines,
    build_engagement_sources,
    build_loss_reasons,
    build_task_kinds,
    counted_loss_reasons,
    default_pipelines,
)

BUILDERS = {
    "GOLDEN_LOSS_REASONS": build_loss_reasons,
    "GOLDEN_ENGAGEMENT_SOURCES": build_engagement_sources,
    "GOLDEN_TASK_KINDS": build_task_kinds,
}


class BuiltinTests(SimpleTestCase):
    def test_builtins_match_the_spec_table(self):
        self.assertEqual([r.name for r in BUILTIN_LOSS_REASONS], [
            "price", "competitor", "timing", "budget", "no_response", "scope", "expired",
            "cancelled", "disqualified", "duplicate", "other", "unknown",
        ])
        self.assertEqual([s.name for s in BUILTIN_ENGAGEMENT_SOURCES], [
            "website_form", "phone", "email", "trade_show", "referral", "marketplace", "social",
            "paid_search", "paid_social", "partner", "outbound", "ai_chat", "other",
        ])
        self.assertEqual([k.name for k in BUILTIN_TASK_KINDS],
                         ["todo", "call", "email", "meeting", "follow_up"])

    def test_disqualified_and_duplicate_do_not_count_as_losses(self):
        self.assertEqual({n for n, r in LOSS_REASONS.items() if not r.counts_as_loss},
                         {"disqualified", "duplicate"})
        self.assertNotIn("duplicate", counted_loss_reasons())

    def test_every_lead_source_is_an_engagement_source(self):
        from crm.models import Lead

        self.assertLessEqual(set(Lead.Source.values), set(ENGAGEMENT_SOURCES))

    def test_default_pipeline_is_the_union_of_lead_and_quote_open_stages(self):
        (sales,) = default_pipelines()
        self.assertEqual(sales.key, "sales")
        self.assertEqual([(s.key, s.default_probability) for s in sales.stages], [
            ("new", 5), ("prospecting", 10), ("qualification", 20), ("needs_analysis", 30),
            ("proposal_sent", 50), ("negotiation", 70), ("verbal_commit", 90), ("closed_signed", 100),
        ])
        self.assertEqual([(s.key, s.name) for s in sales.stages if s.outcome_hint],
                         [("closed_signed", "Closed/Signed")])
        self.assertEqual(sales.stages[-1].outcome_hint, "won")


class AppendAndReloadTests(SimpleTestCase):
    def test_appended_entries_and_reload_on_setting_changed(self):
        with override_settings(GOLDEN_LOSS_REASONS=[{"name": "credit", "label": "Failed credit",
                                                     "counts_as_loss": False}],
                               GOLDEN_TASK_KINDS=[{"name": "site_visit", "label": "Site visit",
                                                   "icon": "ti ti-map-pin"}],
                               GOLDEN_ENGAGEMENT_SOURCES=[{"name": "podcast", "label": "Podcast"}]):
            self.assertFalse(LOSS_REASONS["credit"].counts_as_loss)
            self.assertEqual(TASK_KINDS["site_visit"].icon, "ti ti-map-pin")
            self.assertIn("podcast", ENGAGEMENT_SOURCES)
            self.assertEqual(list(LOSS_REASONS)[:12], [r.name for r in BUILTIN_LOSS_REASONS])
        self.assertNotIn("credit", LOSS_REASONS)
        self.assertNotIn("site_visit", TASK_KINDS)

    def test_default_pipelines_override_reloads(self):
        seed = [{"key": "service", "name": "Service", "stages": [
            {"key": "triage", "name": "Triage", "default_probability": 10},
            {"key": "done", "name": "Done", "outcome_hint": "won"},
        ]}]
        with override_settings(GOLDEN_DEFAULT_PIPELINES=seed):
            (service,) = default_pipelines()
            self.assertEqual([s.key for s in service.stages], ["triage", "done"])
            self.assertEqual(service.stages[1].outcome_hint, "won")
        self.assertEqual(default_pipelines()[0].key, "sales")


class ImproperlyConfiguredTests(SimpleTestCase):
    def assertRefused(self, setting, value, fragment):
        # The setting_changed reload raises on entry, the way a bad deployment
        # setting raises at import (CrmConfig.ready); the builder is the same.
        builder = BUILDERS.get(setting, build_default_pipelines)
        with self.assertRaises(ImproperlyConfigured) as ctx, self.settings(**{setting: value}):
            builder()
        self.assertIn(fragment, str(ctx.exception))

    def test_entry_registries_refuse_bad_entries(self):
        for setting in BUILDERS:
            with self.subTest(setting=setting):
                self.assertRefused(setting, {"name": "x"}, "must be a list")
                self.assertRefused(setting, ["x"], "must be a dict")
                self.assertRefused(setting, [{"name": "Bad-Key", "label": "x"}], "lower-case key")
                self.assertRefused(setting, [{"name": "a" * 33, "label": "x"}], "lower-case key")
                self.assertRefused(setting, [{"name": "ok"}], "not a valid")
                self.assertRefused(setting, [{"name": "ok", "label": " "}], "non-empty label")
                self.assertRefused(setting, [{"name": "ok", "label": "x", "colour": "red"}], "not a valid")
                self.assertRefused(setting, [{"name": "ok", "label": "x", "icon": "fa fa-x"}], "ti ti-")
                self.assertRefused(setting, [{"name": "ok", "label": "x"}, {"name": "ok", "label": "y"}],
                                   "reuses the name")
        self.assertRefused("GOLDEN_LOSS_REASONS", [{"name": "price", "label": "Cost"}], "built-in")
        self.assertRefused("GOLDEN_ENGAGEMENT_SOURCES", [{"name": "phone", "label": "Tel"}], "built-in")
        self.assertRefused("GOLDEN_TASK_KINDS", [{"name": "todo", "label": "To do"}], "built-in")
        self.assertRefused("GOLDEN_LOSS_REASONS",
                           [{"name": "credit", "label": "x", "counts_as_loss": "no"}], "True or False")

    def test_default_pipelines_refuse_bad_shapes(self):
        stage = {"key": "a", "name": "A"}
        cases = [
            ([], "is empty"),
            ("sales", "must be a list"),
            ([{"key": "sales", "name": "Sales"}], "stages must be a non-empty list"),
            ([{"key": "sales", "name": "Sales", "stages": []}], "stages must be a non-empty list"),
            ([{"key": "Sales", "name": "Sales", "stages": [stage]}], "lower-case key"),
            ([{"key": "sales", "name": "", "stages": [stage]}], "non-empty name"),
            ([{"key": "sales", "name": "S", "stages": [stage], "colour": 1}], "unknown field"),
            ([{"key": "sales", "name": "S", "stages": [stage, stage]}], "stage key twice"),
            ([{"key": "sales", "name": "S", "stages": [{**stage, "default_probability": 101}]}], "0-100"),
            ([{"key": "sales", "name": "S", "stages": [{**stage, "default_probability": "50"}]}], "0-100"),
            ([{"key": "sales", "name": "S", "stages": [{**stage, "outcome_hint": "maybe"}]}], "outcome_hint"),
            ([{"key": "sales", "name": "S", "stages": [{**stage, "colour": 1}]}], "unknown field"),
            ([{"key": "sales", "name": "S", "stages": [stage]}] * 2, "pipeline key twice"),
        ]
        for value, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertRefused("GOLDEN_DEFAULT_PIPELINES", value, fragment)

    def test_bad_setting_fails_override_like_import(self):
        # setting_changed rebuilds, so a bad override raises at override time,
        # as a bad deployment setting raises at import (CrmConfig.ready).
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(GOLDEN_TASK_KINDS=[{"name": "todo", "label": "dup"}]):
                pass  # pragma: no cover
        self.assertIn("todo", registries.TASK_KINDS)
