"""Step 6: ``/erp/parties/merge-review/`` (phase2-party.md 6.3)."""

from __future__ import annotations

import uuid

from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.models import Company, UserProfile
from customers.models import Customer
from parties import merge_queue
from parties.models import Party, PartyEmail, PartyMergeLog
from unify.models import StagedRow

URL = "/erp/parties/merge-review/"


class MergeReviewViewTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.staff = self.user("staff", merge=True)
        self.client.force_login(self.staff)

    def user(self, name, *, merge=False, staff=True, company=None):
        user = User.objects.create_user(name, password="pw", is_staff=staff)
        UserProfile.objects.create(user=user, company=company or self.company)
        if merge:
            user.user_permissions.add(Permission.objects.get(codename="merge_party"))
        return User.objects.get(pk=user.pk)

    def customer(self, email, **kw):
        kw.setdefault("first_name", "Ada")
        kw.setdefault("last_name", "Lovelace")
        c = Customer.objects.create(company=self.company, email=email, **kw)
        c.refresh_from_db()
        return c

    def cluster(self, n=1, start=0):
        pairs = []
        for i in range(start, start + n):
            a = self.customer(f"dup{i}@example.com")
            # Two customers of a tenant cannot share an email (step 11): the
            # second person carries it as an extra address.
            b = self.customer(f"alt{i}@example.com")
            PartyEmail.objects.create(party=b.party, email=f"DUP{i}@example.com", purpose="personal")
            pairs.append((a, b))
        merge_queue.scan(self.company)
        return pairs

    def test_page_lists_clusters_with_evidence_and_comparison(self):
        self.cluster()
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Same email d…@example.com")
        self.assertContains(response, "dup0@example.com")
        self.assertContains(response, 'value="approve"')
        self.assertContains(response, 'form="bulk-form"')

    def test_empty_state(self):
        response = self.client.get(URL)
        self.assertContains(response, "No duplicate candidates. Last scan never.")

    def test_non_staff_is_refused(self):
        self.client.force_login(User.objects.create_user("plain", password="pw"))
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_anonymous_redirects_to_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_actions_need_merge_party(self):
        self.cluster()
        viewer = self.user("viewer")
        self.client.force_login(viewer)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'value="approve"')
        row = StagedRow.objects.get()
        self.assertEqual(self.client.post(URL, {"action": "dismiss", "row": row.pk}).status_code, 403)

    def test_scan_approve_apply_undo_through_the_screen(self):
        (a, b), = self.cluster()
        row = StagedRow.objects.get()
        self.client.post(URL, {"action": "approve", "row": row.pk, "survivor": str(b.party_id)})
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, "approved")
        self.assertEqual(row.mapped_values["survivor"], str(b.party_id))
        self.assertEqual(row.mapping_actor, "staff")

        self.client.post(URL, {"action": "apply"})
        self.assertEqual(Party.objects.get(pk=a.party_id).merged_into_id, b.party_id)
        log = PartyMergeLog.objects.get()

        response = self.client.get(URL)
        self.assertContains(response, 'value="undo-merge"')
        self.client.post(URL, {"action": "undo-merge", "merge_id": str(log.merge_id)})
        self.assertIsNone(Party.objects.get(pk=a.party_id).merged_into_id)
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, "proposed")  # an undo clears the decision

    def test_undo_last_run(self):
        (a, b), = self.cluster()
        row = StagedRow.objects.get()
        merge_queue.approve(row, actor="staff")
        self.client.post(URL, {"action": "apply"})
        run = row.batch.approval_runs.get()
        self.assertContains(self.client.get(URL), "Undo last run")
        self.client.post(URL, {"action": "undo-run", "run": run.pk, "undo_token": run.undo_token})
        self.assertIsNone(Party.objects.get(pk=b.party_id).merged_into_id)

    def test_dismiss_is_remembered(self):
        self.cluster()
        row = StagedRow.objects.get()
        self.client.post(URL, {"action": "dismiss", "row": row.pk})
        self.client.post(URL, {"action": "scan"})
        self.assertEqual(StagedRow.objects.count(), 1)
        self.assertEqual(StagedRow.objects.get().mapping_state, "na")

    def test_blocked_cluster_shows_reason_and_refuses_approval(self):
        (a, b), = self.cluster()
        Party.objects.filter(pk=a.party_id).update(user=User.objects.create_user("u1"))
        Party.objects.filter(pk=b.party_id).update(user=User.objects.create_user("u2"))
        response = self.client.get(URL)
        self.assertContains(response, "Cannot be merged here.")
        self.assertContains(response, "different logins")
        self.assertNotContains(response, 'value="approve"')
        self.assertNotContains(response, 'form="bulk-form"')
        row = StagedRow.objects.get()
        response = self.client.post(URL, {"action": "approve", "row": row.pk}, follow=True)
        self.assertContains(response, "Blocked")
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, "proposed")
        self.client.post(URL, {"action": "bulk-approve", "rows": [str(row.pk)]})
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, "proposed")

    def test_bulk_approve_takes_only_bulk_approvable_rows(self):
        self.cluster(2)
        rows = list(StagedRow.objects.all())
        flagged = rows[1]
        flagged.raw = {**flagged.raw, "flags": ["needs_individual_review"], "bulk_approvable": False}
        flagged.save()
        self.client.post(URL, {"action": "bulk-approve", "rows": [str(r.pk) for r in rows]})
        states = {r.pk: r.mapping_state for r in StagedRow.objects.all()}
        self.assertEqual(states[rows[0].pk], "approved")
        self.assertEqual(states[flagged.pk], "proposed")

    def test_sorted_by_confidence_descending_and_ascending(self):
        self.cluster()
        a = self.customer("x1@example.com", first_name="Zed", last_name="Q", phone="2125550111")
        self.customer("x2@example.com", first_name="Zed", last_name="Q", phone="2125550111")  # R2 0.85
        merge_queue.scan(self.company)
        self.assertTrue(a.party_id)
        desc = [c["row"].mapping_confidence for c in self.client.get(URL).context["clusters"]]
        asc = [c["row"].mapping_confidence for c in self.client.get(URL + "?order=asc").context["clusters"]]
        self.assertEqual(desc, sorted(desc, reverse=True))
        self.assertEqual(asc, sorted(asc))
        self.assertEqual(len(desc), 2)
        high = self.client.get(URL + "?band=high").context["clusters"]
        self.assertEqual([str(c["row"].mapping_confidence) for c in high], ["0.950"])

    def test_query_count_is_constant_in_the_page_size(self):
        self.cluster(2)
        with CaptureQueriesContext(connection) as small:
            self.client.get(URL)
        self.cluster(8, start=2)
        with CaptureQueriesContext(connection) as large:
            self.client.get(URL)
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))

    def test_other_company_rows_are_invisible(self):
        self.cluster()
        other = Company.objects.create(name="Other")
        outsider = self.user("outsider", merge=True, company=other)
        self.client.force_login(outsider)
        response = self.client.get(URL)
        self.assertContains(response, "No duplicate candidates")
        row = StagedRow.objects.get()
        self.assertEqual(self.client.post(URL, {"action": "dismiss", "row": row.pk}).status_code, 404)
        self.assertEqual(
            self.client.post(URL, {"action": "undo-merge", "merge_id": str(uuid.uuid4())}).status_code, 404)


class MergeReviewReachableTests(TestCase):
    """R8.03 (``R8-merge-review-unreachable``): the Parties list links the merge
    review for a user who may merge; the link is hidden otherwise (decision 10:
    denied actions are hidden)."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    user = MergeReviewViewTests.user

    def test_parties_list_links_the_merge_review_for_a_merger(self):
        self.client.force_login(self.user("merger", merge=True))
        response = self.client.get("/erp/parties/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="party-merge-review-link"')
        self.assertContains(response, URL)

    def test_parties_list_hides_the_link_without_the_permission(self):
        self.client.force_login(self.user("reader"))
        response = self.client.get("/erp/parties/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="party-merge-review-link"')
