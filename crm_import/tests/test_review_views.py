"""Step 9: the review screens (docs/crm/phase5-importer.md 4.6-4.9).

Job list, job overview, the queue (ascending sort, filters, chips, keyset
pages, the side-by-side row, PII masking), one-row decisions (JSON for the
keyboard, redirects without JS), bulk actions, claims, the runner actions,
each 4.7 empty state, cross-tenant 404s on every route, CSRF on every POST,
and a constant query count on the queue.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest import mock

from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog, Company, UserProfile
from core.tests.helpers import make_staff_user
from crm_import import review
from crm_import import stage as staging
from crm_import.models import ImportJob, ImportJobBatch
from unify.models import StagedRow

from .staging_helpers import ExportDir, company, contact, make_org, make_person, owner

MS = StagedRow.MappingState


def grant(user, *codenames):
    for code in codenames:
        user.user_permissions.add(Permission.objects.get(content_type__app_label="crm_import", codename=code))


class ReviewBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Review Co")
        self.other = Company.objects.create(name="Other Co")
        self.user = self.staff("rev", self.company, "apply_import", "view_import_pii")
        self.client.force_login(self.user)

    def staff(self, name, company, *perms):
        user = make_staff_user(name, f"{name}@example.com", "pw-not-used-1")
        UserProfile.objects.update_or_create(user=user, defaults={"company": company, "display_name": name})
        grant(user, *perms)
        return user

    def make_job(self, records, company_=None, **kwargs) -> ImportJob:
        export = ExportDir(self, records)
        return staging.stage(company_ or self.company, export.path, **kwargs)

    def batch(self, job, otype):
        return ImportJobBatch._base_manager.get(job=job, object_type=otype).batch

    def contacts_job(self, n=3, company_=None, *, linked=True):
        """``n`` contacts; with ``linked`` each shares an email with a party
        of the tenant (an R1 link), so the golden side has a candidate."""
        co = company_ or self.company
        records = []
        for i in range(n):
            email = f"person{i}@example.com"
            if linked:
                make_person(co, given=f"Pat{i}", family="Example", emails=[email])
            records.append(contact(f"c{i}", given=f"Pat{i}", family="Example", emails=[email],
                                   phones=[f"+1 555 010 {i:04d}"[-14:]]))
        job = self.make_job({"contact": records}, co)
        # Back to the queue: every row undecided, as a reviewer finds them.
        StagedRow.objects.filter(batch=self.batch(job, "contact")).update(mapping_state=MS.PROPOSED,
                                                                          mapped_values={})
        return job

    def queue_url(self, job, otype="contact", query=""):
        return reverse("erp-crm-import-queue", args=[job.pk, otype]) + (f"?{query}" if query else "")

    def decide_url(self, job, row, otype="contact"):
        return reverse("erp-crm-import-decide", args=[job.pk, otype, row.pk])

    def bulk_url(self, job, otype="contact"):
        return reverse("erp-crm-import-bulk", args=[job.pk, otype])


# --- the job list ------------------------------------------------------------------------


class JobListTests(ReviewBase):
    def test_no_jobs_empty_state_has_runbook_and_no_upload(self):
        response = self.client.get(reverse("erp-crm-import-job-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No CRM imports yet.")
        self.assertContains(response, "5.5")
        self.assertContains(response, "crm_import stage")
        self.assertNotContains(response, 'type="file"')
        self.assertNotContains(response, "Upload")

    def test_lists_own_jobs_only(self):
        mine = self.contacts_job(1, linked=False)
        theirs = self.make_job({"contact": [contact("x1")]}, self.other)
        response = self.client.get(reverse("erp-crm-import-job-list"))
        self.assertContains(response, reverse("erp-crm-import-job", args=[mine.pk]))
        self.assertNotContains(response, reverse("erp-crm-import-job", args=[theirs.pk]))
        self.assertNotContains(response, "No CRM imports yet.")

    def test_filter_without_matches(self):
        self.contacts_job(1, linked=False)
        response = self.client.get(reverse("erp-crm-import-job-list") + "?status=undone")
        self.assertContains(response, "No CRM imports match these filters.")


# --- the overview -------------------------------------------------------------------------


class OverviewTests(ReviewBase):
    def test_cards_in_step_order_with_counts_and_actions(self):
        job = self.contacts_job(2)
        response = self.client.get(reverse("erp-crm-import-job", args=[job.pk]))
        self.assertEqual(response.status_code, 200)
        types = [c["object_type"] for c in response.context["cards"]]
        self.assertEqual(types, ["owner", "pipeline", "company", "contact", "deal", "association",
                                 "relationship", "task", "communication", "consent"])
        card = next(c for c in response.context["cards"] if c["object_type"] == "contact")
        self.assertEqual((card["total"], card["proposed"], card["primary"]), (2, 2, "review"))
        self.assertContains(response, self.queue_url(job))
        self.assertContains(response, reverse("erp-crm-import-report", args=[job.pk]))

    def test_empty_type_with_complete_coverage(self):
        job = self.contacts_job(1, linked=False)
        response = self.client.get(reverse("erp-crm-import-job", args=[job.pk]))
        self.assertContains(response, "The source has no tasks.")
        response = self.client.get(self.queue_url(job, "task"))
        self.assertContains(response, "The source has no tasks.")

    def test_empty_type_with_unknown_coverage_is_a_warning(self):
        job = self.contacts_job(1, linked=False)
        manifest = job.manifest
        manifest["objects"]["relationship"] = {"coverage": "unknown",
                                               "reasons": [{"code": "forbidden", "detail": "403 on tickets"}]}
        ImportJob.objects.filter(pk=job.pk).update(manifest=manifest)
        for url in (reverse("erp-crm-import-job", args=[job.pk]), self.queue_url(job, "relationship")):
            response = self.client.get(url)
            self.assertContains(response, "Partner relationships: coverage unknown")
            self.assertContains(response, "403 on tickets")
            self.assertContains(response, "0 rows here does not mean the source has none.")
            self.assertContains(response, "alert-warning")

    def test_applied_with_merge_hints(self):
        job = self.contacts_job(2, linked=False)
        rows = StagedRow.objects.filter(batch=self.batch(job, "contact"))
        for row in rows:
            row.mapping_state = MS.APPLIED
            row.proposed_mapping = {**row.proposed_mapping, "possible_duplicate_of": ["c9"]}
            row.save()
        ImportJob.objects.filter(pk=job.pk).update(status=ImportJob.Status.APPLIED)
        response = self.client.get(reverse("erp-crm-import-job", args=[job.pk]))
        self.assertContains(response, "Applied. 2 imported records look like duplicates of each other")
        self.assertContains(response, reverse("erp-party-merge-review"))

    def test_progress_json(self):
        job = self.contacts_job(1, linked=False)
        response = self.client.get(reverse("erp-crm-import-progress", args=[job.pk]))
        self.assertEqual(response.json(), {"status": "reviewing", "runs": []})

    def test_actions_need_the_permission(self):
        job = self.contacts_job(1, linked=False)
        viewer = self.staff("viewer", self.company)
        client = Client()
        client.force_login(viewer)
        response = client.get(reverse("erp-crm-import-job", args=[job.pk]))
        self.assertContains(response, "need the “Can apply and undo CRM imports” permission")
        self.assertNotContains(response, 'value="undo_job"')
        response = client.post(reverse("erp-crm-import-action", args=[job.pk]),
                               {"action": "cancel"})
        self.assertEqual(response.status_code, 403)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.REVIEWING)

    def test_apply_then_undo_step_through_the_runner(self):
        job = self.make_job({"owner": [owner("o1", email="olive@example.com")]})
        self.assertEqual(StagedRow.objects.get(batch=self.batch(job, "owner")).mapping_state, MS.APPROVED)
        url = reverse("erp-crm-import-action", args=[job.pk])
        response = self.client.post(url, {"action": "apply", "object_type": "owner"})
        self.assertRedirects(response, reverse("erp-crm-import-job", args=[job.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(StagedRow.objects.get(batch=self.batch(job, "owner")).mapping_state, MS.APPLIED)
        self.assertTrue(UserProfile.objects.filter(company=self.company, user=None).exists())
        self.client.post(url, {"action": "undo_step", "object_type": "owner"})
        self.assertEqual(StagedRow.objects.get(batch=self.batch(job, "owner")).mapping_state, MS.APPROVED)
        self.assertFalse(UserProfile.objects.filter(company=self.company, user=None).exists())

    def test_large_runs_are_sent_to_the_cli(self):
        job = self.make_job({"owner": [owner("o1", email="olive@example.com")]})
        with mock.patch("crm_import.views.review.WEB_RUN_LIMIT", 0), \
                mock.patch("crm_import.runner.run_job") as run:
            response = self.client.post(reverse("erp-crm-import-action", args=[job.pk]),
                                        {"action": "apply", "object_type": "owner"}, follow=True)
        run.assert_not_called()
        self.assertContains(response, f"crm_import run --job {job.pk} --through owner")

    def test_cancel(self):
        job = self.contacts_job(1, linked=False)
        self.client.post(reverse("erp-crm-import-action", args=[job.pk]), {"action": "cancel"})
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.CANCELLED)

    def test_unknown_action_is_400(self):
        job = self.contacts_job(1, linked=False)
        response = self.client.post(reverse("erp-crm-import-action", args=[job.pk]), {"action": "drop"})
        self.assertEqual(response.status_code, 400)


# --- the queue ---------------------------------------------------------------------------


class QueueTests(ReviewBase):
    def set_conf(self, job, values: dict, otype="contact"):
        for row in StagedRow.objects.filter(batch=self.batch(job, otype)):
            if row.raw["source_id"] in values:
                row.mapping_confidence = values[row.raw["source_id"]]
                row.save(update_fields=["mapping_confidence"])

    def test_sorted_by_confidence_ascending_then_source_order(self):
        job = self.contacts_job(4)
        self.set_conf(job, {"c0": "0.950", "c1": "0.300", "c2": "0.950", "c3": "0.000"})
        response = self.client.get(self.queue_url(job))
        names = [i["row"].raw["source_id"] for i in response.context["items"]]
        self.assertEqual(names, ["c3", "c1", "c0", "c2"])
        response = self.client.get(self.queue_url(job, query="order=desc"))
        self.assertEqual([i["row"].raw["source_id"] for i in response.context["items"]], ["c2", "c0", "c1", "c3"])

    def test_keyset_pages(self):
        job = self.contacts_job(5, linked=False)
        with mock.patch("crm_import.review.PAGE_SIZE", 2):
            first = self.client.get(self.queue_url(job))
            self.assertEqual(len(first.context["items"]), 2)
            nxt = first.context["next_query"]
            self.assertIn("after=", nxt)
            second = self.client.get(self.queue_url(job, query=nxt))
            third = self.client.get(self.queue_url(job, query=second.context["next_query"]))
        seen = [i["row"].row_number for r in (first, second, third) for i in r.context["items"]]
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(set(seen)), 5)
        self.assertEqual(third.context["next_query"], "")

    def test_a_broken_cursor_shows_the_first_page(self):
        job = self.contacts_job(2, linked=False)
        response = self.client.get(self.queue_url(job, query="after=zz"), follow=True)
        self.assertEqual(len(response.context["items"]), 2)
        self.assertContains(response, "That page link is not valid")

    def test_filters_and_chip_counts(self):
        job = self.contacts_job(3)
        rows = list(StagedRow.objects.filter(batch=self.batch(job, "contact")).order_by("row_number"))
        rows[0].proposed_mapping = {**rows[0].proposed_mapping, "flags": ["phone_only"], "decision": "link"}
        rows[0].mapping_confidence = "0.850"
        rows[0].save()
        response = self.client.get(self.queue_url(job, query="flag=phone_only"))
        self.assertEqual([i["row"].pk for i in response.context["items"]], [rows[0].pk])
        self.assertContains(response, "Phone only: 1 left")
        response = self.client.get(self.queue_url(job, query="band=mid"))
        self.assertEqual([i["row"].pk for i in response.context["items"]], [rows[0].pk])
        response = self.client.get(self.queue_url(job, query="band=high"))
        self.assertEqual(len(response.context["items"]), 2)
        response = self.client.get(self.queue_url(job, query="decision=create"))
        self.assertEqual(response.context["items"], [])

    def test_filter_without_matches_empty_state(self):
        job = self.contacts_job(2)
        response = self.client.get(self.queue_url(job, query="decision=conflict"))
        self.assertContains(response, "No rows match these filters.")
        self.assertContains(response, f'href="{self.queue_url(job)}"')
        self.assertContains(response, "Clear filters")

    def test_nothing_undecided_empty_state(self):
        job = self.contacts_job(3, linked=False)
        rows = list(StagedRow.objects.filter(batch=self.batch(job, "contact")).order_by("row_number"))
        staging.approve_row(rows[0], "rev")
        staging.approve_row(rows[1], "rev")
        staging.skip_row(rows[2], "rev", "rejected")
        response = self.client.get(self.queue_url(job))
        self.assertContains(response, "All 3 people are decided. 2 approved, 1 skipped.")
        self.assertContains(response, 'value="apply"')
        self.assertNotContains(response, "No rows match these filters.")

    def test_ai_off_note_and_no_ai_column(self):
        job = self.contacts_job(1)
        response = self.client.get(self.queue_url(job))
        self.assertContains(response, "AI suggestions are off for this job")
        self.assertContains(response, "crm_match_propose")
        self.assertNotContains(response, "AI suggestion:")

    def test_ai_on_shows_the_suggestion(self):
        job = self.contacts_job(1)
        ImportJob.objects.filter(pk=job.pk).update(options={"ai_match": True})
        row = StagedRow.objects.get(batch=self.batch(job, "contact"))
        row.proposed_mapping = {**row.proposed_mapping, "ai": {"suggested": "create", "confidence": 0.4,
                                                              "reason": "different city", "disagrees": True}}
        row.save()
        response = self.client.get(self.queue_url(job))
        self.assertNotContains(response, "AI suggestions are off")
        self.assertContains(response, "AI suggestion:")
        self.assertContains(response, "different city")

    def test_side_by_side_and_sentence(self):
        job = self.contacts_job(1)
        response = self.client.get(self.queue_url(job))
        item = response.context["items"][0]
        self.assertTrue(item["sentence"].startswith("Link to Pat0 Example — same email p"))
        labels = {line["label"]: line for line in item["comparison"]}
        self.assertEqual(labels["Emails"]["source"], "person0@example.com")
        self.assertFalse(labels["Given name"]["differs"])
        self.assertContains(response, "Fincas: Pat0 Example")

    def test_pii_is_masked_without_the_permission(self):
        job = self.contacts_job(1)
        viewer = self.staff("viewer", self.company, "apply_import")
        client = Client()
        client.force_login(viewer)
        response = client.get(self.queue_url(job))
        self.assertNotContains(response, "person0@example.com")
        self.assertContains(response, "p***@example.com")
        self.assertNotContains(response, "Raw source record")
        self.assertContains(response, "unmasking needs")
        self.assertContains(response, "Pat0")  # names stay visible
        response = self.client.get(self.queue_url(job))
        self.assertContains(response, "person0@example.com")
        self.assertContains(response, "Raw source record")

    def test_read_only_without_apply_permission(self):
        job = self.contacts_job(1)
        viewer = self.staff("viewer", self.company)
        client = Client()
        client.force_login(viewer)
        response = client.get(self.queue_url(job))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="bulk-form"')
        self.assertNotContains(response, 'value="approve"')

    def test_header_counts(self):
        job = self.contacts_job(4, linked=False)
        rows = list(StagedRow.objects.filter(batch=self.batch(job, "contact")).order_by("row_number"))
        staging.approve_row(rows[0], "rev")
        staging.skip_row(rows[1], "rev", "rejected")
        response = self.client.get(self.queue_url(job))
        h = response.context["header"]
        self.assertEqual((h["total"], h["decided"], h["remaining"], h["percent"]), (4, 2, 2, 50))
        self.assertEqual((h["you_today"], h["everyone_today"]), (2, 2))
        self.assertContains(response, "2 of 4 decided (50%)")

    def test_constant_queries(self):
        small = self.contacts_job(3)
        other = Company.objects.create(name="Big Co")
        big_user = self.staff("big", other, "apply_import", "view_import_pii")
        big = self.contacts_job(40, other)
        counts = []
        for user, job in ((self.user, small), (big_user, big)):
            client = Client()
            client.force_login(user)
            client.get(self.queue_url(job))  # warm caches (labels, content types)
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(self.queue_url(job))
            self.assertEqual(response.status_code, 200)
            counts.append(len(ctx.captured_queries))
        self.assertEqual(len(response.context["items"]), 40)
        self.assertEqual(counts[0], counts[1], counts)


# --- one decision ---------------------------------------------------------------------------


class DecisionTests(ReviewBase):
    def setUp(self):
        super().setUp()
        self.job = self.contacts_job(3)
        self.rows = list(StagedRow.objects.filter(batch=self.batch(self.job, "contact")).order_by("row_number"))

    def test_approve_returns_header_and_row_json(self):
        response = self.client.post(self.decide_url(self.job, self.rows[0]), {"action": "approve"},
                                    HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        out = response.json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "approved")
        self.assertEqual(out["header"]["decided"], 1)
        self.assertIn(f'id="row-{self.rows[0].pk}"', out["row_html"])
        self.assertIn("1 of 3 decided", out["header_html"])
        self.rows[0].refresh_from_db()
        self.assertEqual((self.rows[0].mapping_state, self.rows[0].mapping_actor), (MS.APPROVED, "rev"))

    def test_without_js_redirects_back_to_the_view(self):
        response = self.client.post(self.decide_url(self.job, self.rows[0]),
                                    {"action": "skip", "reason": "out_of_scope", "query": "band=high"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("band=high", response["Location"])
        self.rows[0].refresh_from_db()
        self.assertEqual(self.rows[0].proposed_mapping["reason"], "out_of_scope")

    def test_pick_candidate_and_create_new(self):
        target = self.rows[1].proposed_mapping["candidates"][0]["id"]
        self.client.post(self.decide_url(self.job, self.rows[1]), {"pick": target})
        self.rows[1].refresh_from_db()
        self.assertEqual(self.rows[1].mapped_values["target"]["id"], target)
        self.client.post(self.decide_url(self.job, self.rows[2]), {"action": "create"})
        self.rows[2].refresh_from_db()
        self.assertEqual(self.rows[2].mapped_values["decision"], "create")

    def test_conflict_needs_every_field_settled(self):
        row = self.rows[0]
        row.proposed_mapping = {**row.proposed_mapping, "decision": "conflict",
                                "diff": {"fields": {"given_name": "conflict"}}}
        row.save()
        response = self.client.post(self.decide_url(self.job, row), {"action": "approve"},
                                    HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Settle every conflicting field", response.json()["message"])
        response = self.client.post(self.decide_url(self.job, row), {"action": "approve", "field:given_name": "keep"},
                                    HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.mapped_values["keep"], ["given_name"])

    def test_a_row_claimed_by_someone_else_is_refused(self):
        row = self.rows[0]
        row.proposed_mapping = {**row.proposed_mapping, "claim": {"by": "someone", "until": review.epoch() + 600}}
        row.save()
        response = self.client.post(self.decide_url(self.job, row), {"action": "approve"},
                                    HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertIn("claimed by someone", response.json()["message"])
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, MS.PROPOSED)

    def test_an_expired_claim_lapses(self):
        row = self.rows[0]
        row.proposed_mapping = {**row.proposed_mapping, "claim": {"by": "someone", "until": review.epoch() - 1}}
        row.save()
        self.client.post(self.decide_url(self.job, row), {"action": "approve"})
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, MS.APPROVED)
        self.assertNotIn("claim", row.proposed_mapping)

    def test_needs_the_apply_permission(self):
        viewer = self.staff("viewer", self.company, "view_import_pii")
        client = Client()
        client.force_login(viewer)
        response = client.post(self.decide_url(self.job, self.rows[0]), {"action": "approve"})
        self.assertEqual(response.status_code, 403)
        self.rows[0].refresh_from_db()
        self.assertEqual(self.rows[0].mapping_state, MS.PROPOSED)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.decide_url(self.job, self.rows[0])).status_code, 405)
        self.assertEqual(self.client.get(self.bulk_url(self.job)).status_code, 405)
        self.assertEqual(self.client.get(reverse("erp-crm-import-action", args=[self.job.pk])).status_code, 405)

    def test_pipeline_closed_stages_need_an_outcome(self):
        from .staging_helpers import pipeline

        job = self.make_job({"pipeline": [pipeline("p1", key="imported", stages=("new", "ended"),
                                                   outcomes={"ended": "closed"})]})
        row = StagedRow.objects.get(batch=self.batch(job, "pipeline"))
        self.assertEqual(row.mapping_state, MS.PROPOSED)
        url = self.decide_url(job, row, "pipeline")
        response = self.client.post(url, {"action": "approve"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("ended", response.json()["message"])
        self.client.post(url, {"action": "approve", "stage_outcome:ended": "lost"})
        row.refresh_from_db()
        self.assertEqual(row.mapped_values["stage_outcomes"], {"ended": "lost"})


class CsrfTests(ReviewBase):
    def test_every_post_needs_the_token(self):
        job = self.contacts_job(1)
        row = StagedRow.objects.get(batch=self.batch(job, "contact"))
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for url, data in ((self.decide_url(job, row), {"action": "approve"}),
                          (self.bulk_url(job), {"action": "claim"}),
                          (reverse("erp-crm-import-action", args=[job.pk]), {"action": "cancel"})):
            self.assertEqual(client.post(url, data).status_code, 403, url)
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, MS.PROPOSED)
        self.assertNotIn("claim", row.proposed_mapping)


# --- bulk actions and claims -------------------------------------------------------------


class BulkTests(ReviewBase):
    def setUp(self):
        super().setUp()
        self.job = self.contacts_job(4)
        self.rows = list(StagedRow.objects.filter(batch=self.batch(self.job, "contact")).order_by("row_number"))

    def post(self, data, client=None):
        return (client or self.client).post(self.bulk_url(self.job), data)

    def states(self):
        return [StagedRow.objects.get(pk=r.pk).mapping_state for r in self.rows]

    def test_approve_eligible_skips_flags_r6_and_other_claims(self):
        r = self.rows
        r[1].proposed_mapping = {**r[1].proposed_mapping, "flags": ["name_mismatch"]}
        r[1].save()
        r[2].proposed_mapping = {**r[2].proposed_mapping, "rule": "R6"}
        r[2].save()
        r[3].proposed_mapping = {**r[3].proposed_mapping, "claim": {"by": "someone", "until": review.epoch() + 600}}
        r[3].save()
        response = self.client.get(self.queue_url(self.job))
        self.assertEqual(response.context["eligible"], 1)
        self.assertContains(response, "Approve 1 row")
        self.post({"action": "approve_eligible"})
        self.assertEqual(self.states(), [MS.APPROVED, MS.PROPOSED, MS.PROPOSED, MS.PROPOSED])
        log = AuditLog.objects.filter(model_name="unify.ImportBatch").latest("created_at")
        self.assertEqual((log.changes["bulk"], log.changes["done"], log.changes["actor"]),
                         ("approve_eligible", 1, "rev"))

    def test_approve_eligible_respects_the_view(self):
        self.post({"action": "approve_eligible", "query": "decision=create"})
        self.assertEqual(self.states(), [MS.PROPOSED] * 4)

    def test_approve_selected_confirms_flagged_rows(self):
        r = self.rows
        r[0].proposed_mapping = {**r[0].proposed_mapping, "flags": ["phone_only"]}
        r[0].save()
        response = self.post({"action": "approve_selected", "ids": [r[0].pk, r[1].pk]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Phone only")
        self.assertEqual(self.states()[:2], [MS.PROPOSED, MS.PROPOSED])
        self.post({"action": "approve_selected", "ids": [r[0].pk, r[1].pk], "confirm": "1"})
        self.assertEqual(self.states()[:2], [MS.APPROVED, MS.APPROVED])

    def test_an_empty_selection_approves_nothing(self):
        self.post({"action": "approve_selected"})
        self.assertEqual(self.states(), [MS.PROPOSED] * 4)

    def test_selection_cannot_cross_tenants_or_batches(self):
        foreign = self.contacts_job(1, self.other)
        theirs = StagedRow.objects.get(batch=self.batch(foreign, "contact"))
        self.post({"action": "approve_selected", "ids": [theirs.pk, "not-a-uuid"], "confirm": "1"})
        theirs.refresh_from_db()
        self.assertEqual(theirs.mapping_state, MS.PROPOSED)
        # And another tenant's job is a 404 before anything is read.
        response = self.client.post(reverse("erp-crm-import-bulk", args=[foreign.pk, "contact"]),
                                    {"action": "approve_eligible"})
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.mapping_state, MS.PROPOSED)

    def test_bulk_leaves_rows_claimed_by_others(self):
        r = self.rows
        r[0].proposed_mapping = {**r[0].proposed_mapping, "claim": {"by": "someone", "until": review.epoch() + 600}}
        r[0].save()
        response = self.client.post(self.bulk_url(self.job), {"action": "create_selected",
                                                              "ids": [r[0].pk, r[1].pk]}, follow=True)
        self.assertContains(response, "1 claimed by another reviewer left alone")
        self.assertEqual(self.states()[:2], [MS.PROPOSED, MS.APPROVED])

    def test_skip_selected_needs_a_reason(self):
        response = self.client.post(self.bulk_url(self.job), {"action": "skip_selected", "ids": [self.rows[0].pk]},
                                    follow=True)
        self.assertContains(response, "Skipping needs a reason")
        self.assertEqual(self.states()[0], MS.PROPOSED)
        self.post({"action": "skip_selected", "ids": [self.rows[0].pk], "reason": "out_of_scope"})
        self.assertEqual(self.states()[0], MS.NA)

    def test_claim_next_then_assigned_to_me_then_release(self):
        def mine():
            return [r for r in StagedRow.objects.filter(batch=self.batch(self.job, "contact")).order_by("row_number")
                    if (r.proposed_mapping.get("claim") or {}).get("by") == "rev"]

        with mock.patch("crm_import.review.CLAIM_BATCH", 2):
            response = self.post({"action": "claim"})
            self.assertIn("mine=1", response["Location"])
            self.assertEqual([r.pk for r in mine()], [r.pk for r in self.rows[:2]])  # the next two, in queue order
            self.post({"action": "claim"})
        claimed = mine()
        self.assertEqual(len(claimed), 4)
        until = claimed[0].proposed_mapping["claim"]["until"]
        self.assertAlmostEqual(until, review.epoch() + 3600, delta=5)
        response = self.client.get(self.queue_url(self.job, query="mine=1"))
        self.assertEqual(len(response.context["items"]), 4)
        self.assertContains(response, "Claimed by you")
        # Another reviewer sees the marker and claims nothing of these.
        other = self.staff("other", self.company, "apply_import")
        client = Client()
        client.force_login(other)
        response = client.get(self.queue_url(self.job))
        self.assertContains(response, "Claimed by rev")
        client.post(self.bulk_url(self.job), {"action": "claim"})
        self.assertFalse(StagedRow.objects.filter(batch=self.batch(self.job, "contact"),
                                                  proposed_mapping__claim__by="other").exists())
        # An hour later the claims lapse: the other reviewer can take them.
        later = timezone.now() + dt.timedelta(hours=1, minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            client.post(self.bulk_url(self.job), {"action": "claim"})
        self.assertEqual(StagedRow.objects.filter(batch=self.batch(self.job, "contact"),
                                                  proposed_mapping__claim__by="other").count(), 4)
        self.post({"action": "release"})
        self.assertEqual(StagedRow.objects.filter(batch=self.batch(self.job, "contact"),
                                                  proposed_mapping__claim__by="other").count(), 4)
        client.post(self.bulk_url(self.job), {"action": "release"})
        self.assertFalse(StagedRow.objects.filter(batch=self.batch(self.job, "contact"),
                                                  proposed_mapping__claim__isnull=False).exists())

    def test_rematch_keeps_claims_and_reproposes_undecided_only(self):
        r = self.rows
        staging.approve_row(r[0], "rev")
        r[1].refresh_from_db()
        r[1].proposed_mapping = {**r[1].proposed_mapping, "claim": {"by": "rev", "until": review.epoch() + 600},
                                 "decision": None}
        r[1].save()
        self.post({"action": "rematch_batch"})
        r[1].refresh_from_db()
        self.assertEqual(r[1].proposed_mapping["decision"], "link")
        self.assertEqual(r[1].proposed_mapping["claim"]["by"], "rev")
        self.assertEqual(StagedRow.objects.get(pk=r[0].pk).mapping_state, MS.APPROVED)

    def test_bulk_needs_the_apply_permission(self):
        viewer = self.staff("viewer", self.company)
        client = Client()
        client.force_login(viewer)
        self.assertEqual(self.post({"action": "claim"}, client).status_code, 403)


# --- tenancy --------------------------------------------------------------------------------


class TenancyTests(ReviewBase):
    def test_every_route_is_a_404_for_another_tenant(self):
        job = self.contacts_job(1, self.other)
        row = StagedRow.objects.get(batch=self.batch(job, "contact"))
        gets = [reverse("erp-crm-import-job", args=[job.pk]), reverse("erp-crm-import-progress", args=[job.pk]),
                self.queue_url(job), reverse("erp-crm-import-report", args=[job.pk])]
        for url in gets:
            self.assertEqual(self.client.get(url).status_code, 404, url)
        posts = [(self.decide_url(job, row), {"action": "approve"}), (self.bulk_url(job), {"action": "claim"}),
                 (reverse("erp-crm-import-action", args=[job.pk]), {"action": "cancel"})]
        for url, data in posts:
            self.assertEqual(self.client.post(url, data).status_code, 404, url)
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, MS.PROPOSED)

    def test_a_row_of_another_batch_is_a_404(self):
        job = self.make_job({"contact": [contact("c1")], "company": [company("k1")]})
        org_row = StagedRow.objects.get(batch=self.batch(job, "company"))
        response = self.client.post(self.decide_url(job, org_row, "contact"), {"action": "approve"})
        self.assertEqual(response.status_code, 404)

    def test_unknown_object_type_is_a_404(self):
        job = self.contacts_job(1, linked=False)
        self.assertEqual(self.client.get(self.queue_url(job, "property_def")).status_code, 404)

    def test_superuser_without_a_company_sees_every_tenant(self):
        job = self.contacts_job(1, self.other)
        admin = make_staff_user("root", "root@example.com", "pw-not-used-2", is_superuser=True)
        client = Client()
        client.force_login(admin)
        self.assertEqual(client.get(self.queue_url(job)).status_code, 200)


class OrgRowTests(ReviewBase):
    def test_company_side_by_side(self):
        make_org(self.company, "Acme Widgets", domain="acme.example")
        job = self.make_job({"company": [company("k1", name="Acme Widgets", domains=["acme.example"])]})
        StagedRow.objects.filter(batch=self.batch(job, "company")).update(mapping_state=MS.PROPOSED)
        response = self.client.get(self.queue_url(job, "company"))
        item = response.context["items"][0]
        labels = {line["label"]: line for line in item["comparison"]}
        self.assertEqual(labels["Name"]["golden"], "Acme Widgets")
        self.assertFalse(labels["Name"]["differs"])
        self.assertTrue(json.dumps(item["sentence"]))


# --- review hardening ------------------------------------------------------------------------


class HardeningTests(ReviewBase):
    def setUp(self):
        super().setUp()
        self.job = self.contacts_job(3)
        self.rows = list(StagedRow.objects.filter(batch=self.batch(self.job, "contact")).order_by("row_number"))

    def test_json_row_is_masked_without_pii(self):
        viewer = self.staff("viewer", self.company, "apply_import")
        client = Client()
        client.force_login(viewer)
        response = client.post(self.decide_url(self.job, self.rows[0]), {"action": "skip", "reason": "rejected"},
                               HTTP_ACCEPT="application/json")
        html = response.json()["row_html"]
        self.assertNotIn("person0@example.com", html)
        self.assertIn("p***@example.com", html)

    def test_a_communication_subject_is_withheld_without_pii(self):
        raw = {"source_id": "m1", "data": {"kind": "note", "subject": "Divorce papers for Pat"}}
        self.assertEqual(review.source_name("communication", raw, can_pii=False), "Note m1")
        self.assertEqual(review.source_name("communication", raw), "Divorce papers for Pat")

    def test_a_frozen_job_cannot_be_decided(self):
        for status in (ImportJob.Status.CANCELLED, ImportJob.Status.APPLYING, ImportJob.Status.UNDOING):
            ImportJob.objects.filter(pk=self.job.pk).update(status=status)
            response = self.client.post(self.decide_url(self.job, self.rows[0]), {"action": "approve"},
                                        HTTP_ACCEPT="application/json")
            self.assertEqual(response.status_code, 409, status)
            self.client.post(self.bulk_url(self.job), {"action": "approve_eligible"})
            self.client.post(self.bulk_url(self.job), {"action": "claim"})
            self.assertEqual({StagedRow.objects.get(pk=r.pk).mapping_state for r in self.rows}, {MS.PROPOSED})
            self.assertFalse(StagedRow.objects.filter(batch=self.batch(self.job, "contact"),
                                                      proposed_mapping__claim__isnull=False).exists())
            response = self.client.get(self.queue_url(self.job))
            self.assertContains(response, "its rows cannot be decided now")
            self.assertNotContains(response, 'id="bulk-form"')

    def test_a_changed_proposal_is_refused(self):
        seen = review.fingerprint(self.rows[0].proposed_mapping)
        row = self.rows[0]
        row.proposed_mapping = {**row.proposed_mapping, "decision": "create", "target": None}
        row.save()
        response = self.client.post(self.decide_url(self.job, row), {"action": "approve", "seen": seen},
                                    HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("changed since the page was loaded", response.json()["message"])
        response = self.client.post(self.decide_url(self.job, row),
                                    {"action": "approve", "seen": review.fingerprint(row.proposed_mapping)},
                                    HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)

    def test_the_page_carries_the_fingerprint(self):
        response = self.client.get(self.queue_url(self.job))
        self.assertContains(response, f'name="seen" value="{review.fingerprint(self.rows[0].proposed_mapping)}"')

    def test_rematch_leaves_rows_another_reviewer_holds(self):
        row = self.rows[0]
        row.proposed_mapping = {**row.proposed_mapping, "decision": None,
                                "claim": {"by": "someone", "until": review.epoch() + 600}}
        row.save()
        self.client.post(self.bulk_url(self.job), {"action": "rematch_batch"})
        row.refresh_from_db()
        self.assertIsNone(row.proposed_mapping["decision"])

    def test_rematch_is_refused_above_the_limit(self):
        with mock.patch("crm_import.views.review.WEB_RUN_LIMIT", 1):
            response = self.client.post(self.bulk_url(self.job), {"action": "rematch_batch"}, follow=True)
        self.assertContains(response, "Re-matching more than 1 rows from the browser is refused")

    def test_skipping_an_approved_row_waits_for_an_open_run(self):
        from unify.apply import start_apply_run

        staging.approve_row(self.rows[0], "rev")
        start_apply_run(self.batch(self.job, "contact"), "rev")
        response = self.client.post(self.decide_url(self.job, self.rows[0]), {"action": "skip", "reason": "rejected"},
                                    HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("apply run of this step is open", response.json()["message"])

    def test_rows_without_flags_count_as_unflagged(self):
        row = self.rows[0]
        row.proposed_mapping = {k: v for k, v in row.proposed_mapping.items() if k not in ("flags", "ai")}
        row.save()
        qs = StagedRow.objects.filter(batch=self.batch(self.job, "contact"))
        self.assertEqual(qs.filter(review.eligible_q("rev")).count(), 3)

    def test_bulk_audit_names_the_user(self):
        self.client.post(self.bulk_url(self.job), {"action": "claim"})
        log = AuditLog.objects.filter(model_name="unify.ImportBatch").latest("created_at")
        self.assertEqual(log.user, self.user.userprofile)

    def test_retry_by_code_is_offered(self):
        StagedRow.objects.filter(pk=self.rows[0].pk).update(mapping_state=MS.FAILED,
                                                             error="dependency_missing: company 7 not applied")
        response = self.client.get(self.queue_url(self.job, query="state=failed"))
        self.assertContains(response, 'value="dependency_missing"')
        with mock.patch("crm_import.runner.retry_job") as retry:
            retry.return_value = mock.Mock(steps=[], blocked=None)
            self.client.post(reverse("erp-crm-import-action", args=[self.job.pk]),
                             {"action": "retry", "object_type": "contact", "code": "dependency_missing"})
        self.assertEqual(retry.call_args.kwargs["codes"], ["dependency_missing"])


class ReportRouteTests(ReviewBase):
    def test_the_report_lives_under_the_job_and_the_old_path_redirects(self):
        job = self.contacts_job(1, linked=False)
        new = reverse("erp-crm-import-report", args=[job.pk])
        self.assertEqual(new, f"/erp/imports/crm/{job.pk}/report/")
        response = self.client.get(f"/erp/imports/{job.pk}/report/")
        self.assertRedirects(response, new, fetch_redirect_response=False)
        response = self.client.get(new)
        self.assertContains(response, reverse("erp-crm-import-job", args=[job.pk]))

    def test_the_old_path_still_404s_another_tenant(self):
        job = self.contacts_job(1, self.other, linked=False)
        response = self.client.get(f"/erp/imports/{job.pk}/report/", follow=True)
        self.assertEqual(response.status_code, 404)


class FieldDiffTests(ReviewBase):
    def conflict_row(self):
        job = self.contacts_job(1)
        row = StagedRow.objects.get(batch=self.batch(job, "contact"))
        row.proposed_mapping = {**row.proposed_mapping, "decision": "conflict",
                                "diff": {"fields": {"given_name": "conflict", "emails": "update"}}}
        row.save()
        return job, row

    def test_old_to_new_in_full_with_the_permission(self):
        job, row = self.conflict_row()
        item = self.client.get(self.queue_url(job)).context["items"][0]
        diff = {d["field"]: d for d in item["diff"]}
        self.assertEqual((diff["given_name"]["golden"], diff["given_name"]["source"]), ("Pat0", "Pat0"))
        self.assertIn("person0@example.com", diff["emails"]["source"])
        self.assertTrue(diff["given_name"]["conflict"])

    def test_masked_without_the_permission(self):
        job, row = self.conflict_row()
        viewer = self.staff("viewer", self.company, "apply_import")
        client = Client()
        client.force_login(viewer)
        response = client.get(self.queue_url(job))
        diff = {d["field"]: d for d in response.context["items"][0]["diff"]}
        self.assertEqual((diff["given_name"]["golden"], diff["given_name"]["source"]), ("P…", "P…"))
        self.assertEqual(diff["emails"]["source"], "p***@example.com")
        self.assertEqual(diff["emails"]["golden"], "no golden column")

    def test_mask_value(self):
        self.assertEqual(review.mask_value("+12025550143"), "***0143")
        self.assertEqual(review.mask_value("raw:555 0143"), "***0143")
        self.assertEqual(review.mask_value("2026-10-01"), "•••")
        self.assertEqual(review.mask_value(None), "—")
        self.assertEqual(review.mask_value(["a@example.com"]), "a***@example.com")
