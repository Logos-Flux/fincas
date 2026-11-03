"""Steps 3-4 (test plan #12, #14): CSV staging, dry-run, chunked apply."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Company
from products.models import Product
from unify.apply import (
    apply_chunk,
    cancel_run,
    start_apply_run,
    undo_run,
)
from unify.ingest import apply_template, dry_run, ingest_csv
from unify.models import ApprovalRow, StagedRow

CSV = """sku,title,retail_price,weird_column
W-1,Widget,19.99,hello
W-2,Gadget,29.50,world
W-1,Widget,19.99,hello
"""


class IngestTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="CSV Co")

    def test_ingest_stages_rows_with_hashes(self):
        batch = ingest_csv(self.company, "products.csv", CSV.encode())
        self.assertEqual(batch.rows.count(), 3)
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.mapping_state, StagedRow.MappingState.UNMAPPED)
        self.assertTrue(row.content_hash)

    def test_in_batch_duplicate_marked_na(self):
        batch = ingest_csv(self.company, "products.csv", CSV.encode())
        dupe = batch.rows.get(row_number=3)
        self.assertEqual(dupe.mapping_state, StagedRow.MappingState.NA)
        self.assertEqual(dupe.dedupe_of_hash, dupe.content_hash)
        self.assertEqual(dupe.mapping_actor, "system:dedupe")

    def test_reupload_after_apply_marks_na(self):
        batch = ingest_csv(self.company, "products.csv", CSV.encode())
        apply_template(batch)
        run = start_apply_run(batch, actor="tester")
        apply_chunk(str(run.pk), [])
        batch2 = ingest_csv(self.company, "products.csv", CSV.encode())
        states = dict(
            batch2.rows.values_list("row_number", "mapping_state")
        )
        self.assertEqual(states[1], StagedRow.MappingState.NA)
        self.assertEqual(states[2], StagedRow.MappingState.NA)

    def test_malformed_inputs_refused(self):
        with self.assertRaises(ValueError):
            ingest_csv(self.company, "empty.csv", b"sku,title\n")
        with self.assertRaises(ValueError):
            ingest_csv(self.company, "noheader.csv", b"\n\n")
        with self.assertRaises(ValueError):
            ingest_csv(self.company, "bin.csv", b"\xff\xfe\x00bad")

    def test_row_cap_named(self):
        import unify.ingest as ingest

        old, ingest.MAX_ROWS = ingest.MAX_ROWS, 1
        try:
            with self.assertRaises(ValueError) as ctx:
                ingest_csv(self.company, "big.csv", CSV.encode())
        finally:
            ingest.MAX_ROWS = old
        self.assertIn("1-row", str(ctx.exception).replace("1,", "1-"))

    def test_template_maps_known_columns(self):
        batch = ingest_csv(self.company, "products.csv", CSV.encode())
        counts = apply_template(batch)
        self.assertEqual(counts, {"mapped": 2, "unmapped": 0})
        row = batch.rows.get(row_number=1)
        self.assertEqual(row.mapping_state, StagedRow.MappingState.APPROVED)
        self.assertEqual(row.mapping_actor, "system:template")
        self.assertEqual(row.mapped_values["sku"], "W-1")
        self.assertEqual(row.mapped_values["title"], "Widget")
        # Unknown header stays in raw for the LLM step — nothing invented.
        self.assertEqual(row.raw["weird_column"], "hello")
        self.assertNotIn("weird_column", row.mapped_values)

    def test_template_leaves_unknown_rows_unmapped(self):
        batch = ingest_csv(
            self.company, "mystery.csv", b"foo,bar\n1,2\n"
        )
        counts = apply_template(batch)
        self.assertEqual(counts, {"mapped": 0, "unmapped": 1})

    def test_dry_run_and_completeness(self):
        batch = ingest_csv(self.company, "products.csv", CSV.encode())
        apply_template(batch)
        report = dry_run(batch)
        self.assertEqual(report["would_create"], 2)
        self.assertEqual(report["needs_review"], 0)
        # 2 approved of 3 rows, 1 NA excluded → 100%.
        self.assertEqual(report["completeness"]["percent"], 100.0)

    def test_approved_requires_mapped_values(self):
        batch = ingest_csv(self.company, "products.csv", CSV.encode())
        row = batch.rows.get(row_number=1)
        row.mapping_state = StagedRow.MappingState.APPROVED
        row.mapped_values = {}
        with self.assertRaises(ValidationError):
            row.full_clean()


class ApplyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Apply Co")
        self.batch = ingest_csv(self.company, "products.csv", CSV.encode())
        apply_template(self.batch)

    def test_apply_creates_products_idempotently(self):
        run = start_apply_run(self.batch, actor="tester")
        result = apply_chunk(str(run.pk), [])
        self.assertEqual(
            [r["state"] for r in result["results"]],
            ["applied", "applied"],
        )
        self.assertEqual(
            Product.objects.filter(company=self.company).count(), 2
        )
        widget = Product.objects.get(title="Widget")
        self.assertEqual(widget.variants.get().sku, "W-1")
        self.assertEqual(widget.variants.get().retail_price, Decimal("19.99"))

        # Re-apply: rows already APPLIED, approval rows already APPLIED.
        result2 = apply_chunk(str(run.pk), [])
        self.assertEqual(result2["results"], [])

    def test_duplicate_sku_fails_named_not_500(self):
        Product.objects.create(title="Existing", company=self.company)
        from products.models import ProductVariant

        ProductVariant.objects.create(
            product=Product.objects.get(title="Existing"), sku="W-1"
        )
        run = start_apply_run(self.batch, actor="tester")
        result = apply_chunk(str(run.pk), [])
        by_row = {r["row"]: r["state"] for r in result["results"]}
        self.assertEqual(by_row[1], "failed")
        self.assertIn("uplicate", result["results"][0]["error"])
        self.assertEqual(by_row[2], "applied")

    def test_cancel_stops_future_chunks(self):
        run = start_apply_run(self.batch, actor="tester")
        cancel_run(str(run.pk))
        result = apply_chunk(str(run.pk), [])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(
            Product.objects.filter(company=self.company).count(), 0
        )

    def test_undo_deactivates_never_deletes(self):
        run = start_apply_run(self.batch, actor="tester")
        apply_chunk(str(run.pk), [])
        out = undo_run(str(run.pk), run.undo_token)
        self.assertEqual(out["undone"], 2)
        self.assertEqual(
            Product.objects.filter(company=self.company).count(), 2
        )
        self.assertEqual(
            Product.objects.filter(
                company=self.company, is_active=True
            ).count(),
            0,
        )

    def test_undo_token_mismatch_refuses(self):
        run = start_apply_run(self.batch, actor="tester")
        apply_chunk(str(run.pk), [])
        with self.assertRaises(ValueError):
            undo_run(str(run.pk), "wrong-token")

    def test_run_requires_actor(self):
        with self.assertRaises(ValueError):
            start_apply_run(self.batch, actor="")

    def test_auto_apply_actor_recorded(self):
        run = start_apply_run(
            self.batch, actor=f"system:auto-apply({self.batch.pk}, 0.95)"
        )
        apply_chunk(str(run.pk), [])
        self.assertTrue(run.actor.startswith("system:auto-apply"))
        row = ApprovalRow.objects.filter(run=run).first()
        self.assertEqual(row.state, ApprovalRow.State.APPLIED)
