"""Step 4 queue UI: upload → triage → approve → chunked apply."""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Company, UserProfile
from products.models import Product
from unify.models import ImportBatch, StagedRow

CSV = b"sku,title,retail_price\nQ-1,Quox,9.99\nQ-2,Quux,19.99\n"

MAPPING_JSON = (
    '{"mappings": {"sku": {"field": "sku", "confidence": 0.9}}, '
    '"row_confidence": 0.9}'
)


class QueueUiTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="UI Co")
        self.staff = User.objects.create_user("q", password="p", is_staff=True)
        UserProfile.objects.create(
            user=self.staff, company=self.company
        )
        self.client.login(username="q", password="p")

    def _upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            "/erp/imports/upload/",
            {
                "source_name": "q.csv",
                "csv_file": SimpleUploadedFile(
                    "q.csv", CSV, content_type="text/csv"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        return ImportBatch.objects.get(company=self.company)

    def test_upload_stages_and_template_maps(self):
        batch = self._upload()
        self.assertEqual(batch.rows.count(), 2)
        response = self.client.get(f"/erp/imports/{batch.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Q-1")

    def test_propose_and_approve_flow(self):
        batch = self._upload()
        row = batch.rows.get(row_number=1)
        row.mapping_state = StagedRow.MappingState.UNMAPPED
        row.mapped_values = {}
        row.save(update_fields=["mapping_state", "mapped_values"])
        with patch(
            "unify.propose.complete",
            return_value=SimpleNamespace(
                text=MAPPING_JSON, resolved_model="m",
                route="r", usage={},
            ),
        ):
            response = self.client.post(
                f"/erp/imports/{batch.pk}/", {"action": "propose-mappings"}
            )
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, StagedRow.MappingState.PROPOSED)

        response = self.client.post(
            f"/erp/imports/{batch.pk}/",
            {"action": "approve-selected", "rows": [str(row.pk)]},
        )
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, StagedRow.MappingState.APPROVED)

    def test_apply_chunk_cancel_undo(self):
        batch = self._upload()
        response = self.client.post(
            f"/erp/imports/{batch.pk}/", {"action": "start-apply"}
        )
        self.assertEqual(response.status_code, 302)
        run = batch.approval_runs.get()
        response = self.client.post(
            f"/erp/imports/{batch.pk}/",
            {"action": "apply-chunk", "run": str(run.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Product.objects.filter(company=self.company).count(), 2
        )
        response = self.client.post(
            f"/erp/imports/{batch.pk}/",
            {
                "action": "undo-run", "run": str(run.pk),
                "undo_token": run.undo_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Product.objects.filter(
                company=self.company, is_active=True
            ).count(),
            0,
        )

    def test_cross_company_batch_404s(self):
        other = Company.objects.create(name="Other UI")
        foreign = ImportBatch.objects.create(
            company=other, source_kind="csv", source_name="x.csv"
        )
        response = self.client.get(f"/erp/imports/{foreign.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_non_staff_blocked(self):
        User.objects.create_user("plain", password="p")
        self.client.login(username="plain", password="p")
        response = self.client.get("/erp/imports/")
        self.assertIn(response.status_code, (302, 403))


class UploadPageTests(TestCase):
    """GET /erp/imports/upload/ used to 500 (FormView with no template)."""

    def setUp(self):
        self.company = Company.objects.create(name="Upload Co")
        self.staff = User.objects.create_user("u", password="p", is_staff=True)
        UserProfile.objects.create(user=self.staff, company=self.company)
        self.client.login(username="u", password="p")

    def test_get_renders_the_upload_form(self):
        response = self.client.get("/erp/imports/upload/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "erp/unify_batch_upload.html")
        self.assertContains(response, 'action="/erp/imports/upload/"')
        self.assertContains(response, 'name="source_name"')
        self.assertContains(response, 'name="csv_file"')
        self.assertContains(response, 'enctype="multipart/form-data"')

    def test_list_page_still_carries_the_same_form(self):
        response = self.client.get("/erp/imports/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'action="/erp/imports/upload/"')

    def test_get_is_staff_only(self):
        User.objects.create_user("n", password="p")
        self.client.login(username="n", password="p")
        response = self.client.get("/erp/imports/upload/")
        self.assertNotEqual(response.status_code, 200)
