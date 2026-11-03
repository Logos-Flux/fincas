"""Step 6: the applier registry (phase2-party.md 3.4)."""

from django.contrib.auth.models import Permission, User
from django.test import TestCase

from core.models import Company, UserProfile
from unify import appliers
from unify.apply import _apply_row, apply_chunk, start_apply_run
from unify.models import ImportBatch, StagedRow


class RegistryTests(TestCase):
    def test_product_and_party_merge_are_registered(self):
        self.assertIn("product", appliers.registered_kinds())
        self.assertIn("party_merge", appliers.registered_kinds())
        self.assertIs(appliers.get_applier("product").apply, appliers.apply_product)
        self.assertIs(_apply_row, appliers.apply_product)  # Steps 3-4 name kept
        self.assertEqual(appliers.get_applier("party_merge").permission, "parties.merge_party")

    def test_unknown_kind_is_named(self):
        with self.assertRaisesMessage(LookupError, "'vendor'"):
            appliers.get_applier("vendor")

    def test_reregistering_a_kind_with_other_functions_refuses(self):
        product = appliers.get_applier("product")
        appliers.register_applier("product", product.apply, product.undo)  # idempotent
        with self.assertRaises(ValueError):
            appliers.register_applier("product", lambda *a, **k: "", product.undo)

    def test_run_uses_the_batch_kind(self):
        company = Company.objects.create(name="Reg Co")
        calls = []

        def fake_apply(company, staged, *, run, approval_row):
            calls.append((staged.pk, run.pk, approval_row.pk))
            return f"fake:{staged.row_number}"

        appliers._REGISTRY.pop("fake", None)
        appliers.register_applier("fake", fake_apply, lambda row: 1)
        self.addCleanup(appliers._REGISTRY.pop, "fake", None)
        batch = ImportBatch.objects.create(company=company, source_kind="csv", source_name="x",
                                           entity_kind="fake")
        StagedRow.objects.create(batch=batch, row_number=1, raw={}, content_hash="h",
                                 mapping_state="approved", mapped_values={"a": 1})
        run = start_apply_run(batch, actor="tester")
        out = apply_chunk(str(run.pk), [])
        self.assertEqual(out["results"][0]["ref"], "fake:1")
        self.assertEqual(len(calls), 1)


class ApplyPermissionTests(TestCase):
    """The generic imports screen applies a party_merge batch only with parties.merge_party."""

    def setUp(self):
        self.company = Company.objects.create(name="Perm Co")
        self.user = User.objects.create_user("staffer", password="pw", is_staff=True)
        UserProfile.objects.create(user=self.user, company=self.company)
        self.client.force_login(self.user)
        self.batch = ImportBatch.objects.create(
            company=self.company, source_kind="dedupe", source_name="scan", entity_kind="party_merge",
        )

    def test_start_apply_needs_merge_party(self):
        self.client.post(f"/erp/imports/{self.batch.pk}/", {"action": "start-apply"})
        self.assertFalse(self.batch.approval_runs.exists())
        self.user.user_permissions.add(Permission.objects.get(codename="merge_party"))
        self.user = User.objects.get(pk=self.user.pk)
        self.client.force_login(self.user)
        self.client.post(f"/erp/imports/{self.batch.pk}/", {"action": "start-apply"})
        self.assertTrue(self.batch.approval_runs.exists())
