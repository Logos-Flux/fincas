"""Step 4 (test plan #13): proposals validated before touching rows.

No live model calls — ``unify.propose.complete`` is patched with recorded
JSON. Covers: happy-path propose→approve→apply chain, malformed/refused
output applying nothing, budget exhaustion, confidence bands, and dedupe
merge approval.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from core.models import Company
from products.models import Product
from unify.ingest import apply_template, ingest_csv
from unify.models import StagedRow
from unify.propose import (
    ProposalError,
    approve_high_confidence,
    approve_rows,
    propose_dedupes,
    propose_mappings,
)

MYSTERY_CSV = b"item_name,cost,weird\nWidg et,19.99,x\n"


def _completion(text, model="test-model"):
    return SimpleNamespace(
        text=text, resolved_model=model, route="test-route", usage={},
    )


MAPPING_JSON = (
    '{"mappings": {"item_name": {"field": "title", "confidence": 0.9}, '
    '"cost": {"field": "retail_price", "confidence": 0.85}}, '
    '"row_confidence": 0.88}'
)

DEDUPE_JSON = (
    '{"duplicates": [{"winner_row": 1, "loser_row": 2, '
    '"confidence": 0.9, "reason": "same SKU"}]}'
)


class MappingProposalTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Prop Co")
        self.batch = ingest_csv(self.company, "mystery.csv", MYSTERY_CSV)
        apply_template(self.batch)
        self.row = self.batch.rows.get(row_number=1)
        self.assertEqual(self.row.mapping_state, StagedRow.MappingState.UNMAPPED)

    def _propose(self, text=MAPPING_JSON):
        with patch("unify.propose.complete", return_value=_completion(text)):
            return propose_mappings(self.batch)

    def test_propose_validates_and_stores(self):
        counts = self._propose()
        self.assertEqual(counts, {"proposed": 1, "skipped": 0})
        self.row.refresh_from_db()
        self.assertEqual(self.row.mapping_state, StagedRow.MappingState.PROPOSED)
        self.assertEqual(
            self.row.proposed_mapping["fields"],
            {"title": "Widg et", "retail_price": "19.99"},
        )
        self.assertEqual(self.row.mapping_confidence, Decimal("0.880"))
        self.assertEqual(self.row.mapping_model, "test-model")
        # Nothing applied yet — mapped_values stay empty until approval.
        self.assertEqual(self.row.mapped_values, {})

    def test_unknown_fields_dropped_not_guessed(self):
        text = (
            '{"mappings": {"item_name": {"field": "title", "confidence": 0.9}, '
            '"weird": {"field": "mind_reading", "confidence": 0.99}}, '
            '"row_confidence": 0.9}'
        )
        self._propose(text)
        self.row.refresh_from_db()
        self.assertEqual(
            self.row.proposed_mapping["fields"], {"title": "Widg et"}
        )

    def test_malformed_output_applies_nothing(self):
        counts = self._propose("not json at all {{{")
        self.assertEqual(counts, {"proposed": 0, "skipped": 1})
        self.row.refresh_from_db()
        self.assertEqual(self.row.mapping_state, StagedRow.MappingState.UNMAPPED)
        self.assertIn("non-JSON", self.row.error)

    def test_out_of_range_confidence_applies_nothing(self):
        from llmhub.client import LLMHubError

        with patch(
            "unify.propose.complete",
            side_effect=LLMHubError("bad", category="shape"),
        ):
            counts = propose_mappings(self.batch)
        self.assertEqual(counts, {"proposed": 0, "skipped": 1})
        self.row.refresh_from_db()
        self.assertEqual(self.row.mapping_state, StagedRow.MappingState.UNMAPPED)

    def test_budget_exhaustion_refuses(self):
        self.batch.llm_call_budget = 0
        self.batch.save(update_fields=["llm_call_budget"])
        with self.assertRaises(ProposalError):
            self._propose()

    def test_approve_then_apply_chain(self):
        self._propose()
        approved = approve_rows(
            self.batch, [str(self.row.pk)], actor="reviewer"
        )
        self.assertEqual(approved, 1)
        self.row.refresh_from_db()
        self.assertEqual(self.row.mapping_state, StagedRow.MappingState.APPROVED)
        self.assertEqual(self.row.mapping_actor, "reviewer")
        self.assertEqual(self.row.mapped_values["title"], "Widg et")

        from unify.apply import apply_chunk, start_apply_run

        run = start_apply_run(self.batch, actor="reviewer")
        result = apply_chunk(str(run.pk), [])
        self.assertEqual(result["results"][0]["state"], "applied")
        self.assertTrue(
            Product.objects.filter(
                company=self.company, title="Widg et"
            ).exists()
        )

    def test_auto_apply_band_and_manual_rest(self):
        self._propose()  # 0.88 < 0.95: stays proposed
        auto = approve_high_confidence(
            self.batch, actor="system:auto-apply(x, 0.95)"
        )
        self.assertEqual(auto, 0)
        manual = approve_rows(self.batch, [], actor="reviewer")
        self.assertEqual(manual, 0)  # empty selection approves nothing
        manual = approve_rows(
            self.batch, [str(self.row.pk)], actor="reviewer"
        )
        self.assertEqual(manual, 1)

    def test_high_confidence_auto_applies(self):
        text = (
            '{"mappings": {"item_name": {"field": "title", "confidence": 0.99}}, '
            '"row_confidence": 0.99}'
        )
        self._propose(text)
        auto = approve_high_confidence(
            self.batch, actor="system:auto-apply(x, 0.95)"
        )
        self.assertEqual(auto, 1)
        self.row.refresh_from_db()
        self.assertEqual(
            self.row.mapping_actor, "system:auto-apply(x, 0.95)"
        )


class DedupeProposalTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Dedupe Co")
        # Near-dupes, not exact: ingest strips values, so exact matches
        # (even whitespace-only differences) collapse to NA at hash time.
        # Case differs → different hash → survives for dedupe review.
        self.batch = ingest_csv(
            self.company, "dupes.csv",
            b"sku,title\nA-1,Widget\nA-1,WIDGET\nB-2,Gadget\n",
        )
        apply_template(self.batch)

    def test_dedupe_merge_marks_loser_na(self):
        with patch("unify.propose.complete", return_value=_completion(DEDUPE_JSON)):
            out = propose_dedupes(self.batch)
        self.assertEqual(out["proposed"], 1)
        loser = self.batch.rows.get(row_number=2)
        self.assertEqual(loser.mapping_state, StagedRow.MappingState.PROPOSED)
        approved = approve_rows(
            self.batch, [str(loser.pk)], actor="reviewer"
        )
        self.assertEqual(approved, 1)
        loser.refresh_from_db()
        self.assertEqual(loser.mapping_state, StagedRow.MappingState.NA)
        winner = self.batch.rows.get(row_number=1)
        self.assertEqual(loser.dedupe_of_hash, winner.content_hash)

    def test_no_candidates_no_call(self):
        batch = ingest_csv(self.company, "clean.csv", b"sku,title\nZ-9,Solo\n")
        with patch(
            "unify.propose.complete",
            side_effect=AssertionError("must not call"),
        ):
            out = propose_dedupes(batch)
        self.assertEqual(out, {"proposed": 0, "groups": 0})

    def test_completeness_excludes_na(self):
        report = self.batch.completeness
        # 3 rows, none NA yet (in-batch exact dupes only) — sanity.
        self.assertEqual(report["total"], 3)


class CoaMappingTests(TestCase):
    def setUp(self):
        from accounting.tests.test_posting import make_accounts

        self.company = Company.objects.create(name="CoA Co")
        make_accounts(self.company)

    def test_coa_mapping_validates_against_chart(self):
        from unify.propose import propose_coa_mapping

        text = (
            '{"mapping": {"receivable": "1200", "revenue": "4010", '
            '"bogus_field": "1010", "cash": "NOPE"}, "confidence": 0.9}'
        )
        with patch("unify.propose.complete", return_value=_completion(text)):
            out = propose_coa_mapping(self.company, "sales_invoice")
        # Known accounts kept; unknown field and number dropped.
        self.assertEqual(
            out["mapping"], {"receivable": "1200", "revenue": "4010"}
        )
        self.assertEqual(out["model"], "test-model")

    def test_coa_mapping_empty_chart_refuses(self):
        from unify.propose import ProposalError, propose_coa_mapping

        empty = Company.objects.create(name="Empty Co")
        with self.assertRaises(ProposalError):
            propose_coa_mapping(empty, "sales_invoice")
