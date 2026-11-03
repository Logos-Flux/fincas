"""Step 6: merge / unmerge, the dedupe queue on ``unify`` (phase2-party.md 3.4)."""

from __future__ import annotations

import datetime
import uuid
from io import StringIO
from unittest import skipUnless

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core import serializers
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.models import Company, ExternalRef
from core.tests.helpers import LOGIN_LOCK_ORDER, login_lock_order
from crm.models import Lead
from customers.models import Customer, CustomerAddress, CustomerContact
from parties import merge_queue
from parties.merge import MergeBlocked, MergeError, merge, unmerge, unmerge_group
from parties.models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyMergeLog,
    PartyPhone,
    PartyRelationship,
    PartyRole,
)
from unify.apply import apply_chunk, start_apply_run, undo_run
from unify.models import ApprovalRow, ImportBatch, StagedRow

SNAPSHOT_MODELS = [
    Party, PartyEmail, PartyPhone, PartyAddress, PartyRole, PartyRelationship, ConsentRecord,
    ExternalRef, Customer, CustomerContact, CustomerAddress, Lead,
]


def snapshot() -> dict:
    """Every row of every table a merge can touch, serialised field by field."""
    out = {}
    for model in SNAPSHOT_MODELS:
        rows = model._base_manager.order_by("pk")
        out[model._meta.label] = serializers.serialize("json", rows)
    return out


class _Base(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def customer(self, **kw):
        kw.setdefault("first_name", "Ada")
        kw.setdefault("last_name", "Lovelace")
        kw.setdefault("email", f"{uuid.uuid4().hex[:8]}@example.com")
        kw.setdefault("company", self.company)
        c = Customer.objects.create(**kw)
        c.refresh_from_db()
        return c

    @staticmethod
    def also_reachable_at(customer, email):
        PartyEmail.objects.create(party=customer.party, email=email, purpose="personal")
        return customer

    def pair(self, email="ada@example.com"):
        """Two individual customers whose people share an email: two parties,
        one R1 cluster. Since step 11 two customers of a tenant cannot share
        one (``uniq_customer_email_ci_company``), so the second person carries
        it as an extra address of its own."""
        a = self.customer(email=email, phone="2125550143")
        b = self.also_reachable_at(self.customer(email=f"alt.{email}", phone="2125550199"), email.upper())
        self.assertNotEqual(a.party_id, b.party_id)
        return a, b


class MergeUndoTests(_Base):
    def build_graph(self):
        a, b = self.pair()
        pa, pb = a.party, b.party
        CustomerAddress.objects.create(
            customer=a, address_line_1="1 Main St", city="Springfield", state_province="IL",
            postal_code="62701", country="US", address_type="billing", is_default=True,
        )
        # Same address on b: deduplicated onto a's copy, b's mirror re-pointed.
        CustomerAddress.objects.create(
            customer=b, address_line_1="1 main st ", city="Springfield", state_province="IL",
            postal_code="62701", country="us", address_type="billing", is_default=True,
        )
        CustomerAddress.objects.create(
            customer=b, address_line_1="9 Elm Rd", city="Shelbyville", state_province="IL",
            postal_code="62565", country="US", address_type="shipping",
        )
        PartyEmail.objects.create(party=pb, email="b.work@example.com", purpose="work")
        org = Party.objects.create(company=self.company, kind="organisation", display_name="Org", origin="erp")
        PartyRelationship.objects.create(from_party=pb, to_party=org, kind="employment", title="CTO")
        PartyRelationship.objects.create(from_party=pa, to_party=org, kind="employment")  # duplicate after merge
        PartyRelationship.objects.create(from_party=pa, to_party=pb, kind="referral")  # becomes a self-relation
        PartyRole.objects.create(party=pb, role="lead", valid_from=datetime.date(2020, 1, 1),
                                 valid_to=datetime.date(2021, 1, 1))
        PartyRole.objects.create(party=pa, role="lead", valid_from=datetime.date(2020, 6, 1),
                                 valid_to=datetime.date(2022, 1, 1))
        ConsentRecord.objects.create(
            party=pb, channel="email", purpose="marketing", state="granted",
            occurred_at=datetime.datetime(2024, 5, 1, 12, 0, 0, 123456, tzinfo=datetime.UTC),
            source="erp", address_normalized="ada@example.com",
        )
        ExternalRef.objects.create(
            company=self.company, source="bigcommerce", object_type="buyer", source_id="77",
            content_type=ContentType.objects.get_for_model(Party), object_id=pb.pk,
        )
        other = self.customer(email="other@example.com", customer_type="business", company_name="Other Ltd")
        contact = CustomerContact.objects.create(customer=other, first_name="Ada", last_name="Lovelace",
                                                 email="contact-only@example.com")
        CustomerContact.objects.filter(pk=contact.pk).update(party=pb)
        lead = Lead.objects.create(company=self.company, first_name="Ada", last_name="Lovelace",
                                   lead_number="LD-M-1", email="lead-only@example.com")
        Lead.objects.filter(pk=lead.pk).update(party=pb)
        return a, b, pa, pb, org

    def test_merge_then_undo_restores_byte_identical_rows(self):
        a, b, pa, pb, org = self.build_graph()
        before = snapshot()

        with self.captureOnCommitCallbacks(execute=True):
            (log,) = merge(pa, [pb], actor="tester")
        mid = snapshot()
        self.assertNotEqual(before, mid)

        pb.refresh_from_db()
        self.assertEqual((pb.merged_into_id, pb.is_active), (pa.pk, False))
        self.assertEqual(Customer.objects.get(pk=b.pk).party_id, pa.pk)
        self.assertFalse(PartyEmail.objects.filter(party=pb).exists())
        self.assertEqual(PartyEmail.objects.filter(party=pa, email_normalized="ada@example.com").count(), 1)
        self.assertEqual(PartyEmail.objects.filter(party=pa, is_primary=True).count(), 1)
        self.assertEqual(PartyAddress.objects.filter(party=pa).count(), 2)  # 1 Main St once, 9 Elm Rd
        self.assertEqual(
            {ca.party_address.party_id for ca in CustomerAddress.objects.filter(customer=b)}, {pa.pk})
        self.assertFalse(PartyRelationship.objects.filter(from_party=pa, to_party=pa).exists())
        self.assertEqual(PartyRelationship.objects.filter(from_party=pa, to_party=org).count(), 1)
        lead_role = PartyRole.objects.get(party=pa, role="lead")
        self.assertEqual((lead_role.valid_from, lead_role.valid_to),
                         (datetime.date(2020, 1, 1), datetime.date(2022, 1, 1)))
        self.assertEqual(PartyRole.objects.filter(party=pa, role="customer", valid_to__isnull=True).count(), 1)
        self.assertEqual(ConsentRecord.objects.get().party_id, pa.pk)
        self.assertEqual(ExternalRef.objects.get(source="bigcommerce").object_id, pa.pk)
        self.assertEqual(Lead.objects.get().party_id, pa.pk)
        self.assertEqual(CustomerContact.objects.get().party_id, pa.pk)
        self.assertEqual(set(log.removed), {"parties.PartyEmail", "parties.PartyAddress", "parties.PartyRole",
                                            "parties.PartyRelationship"})
        out = StringIO()
        call_command("check_tenancy", "--app", "parties", stdout=out)

        with self.captureOnCommitCallbacks(execute=True):
            unmerge(log, actor="tester")
        self.assertEqual(snapshot(), before)
        log.refresh_from_db()
        self.assertIsNotNone(log.undone_at)

    def test_three_way_merge_and_group_undo(self):
        a, b = self.pair()
        c = self.also_reachable_at(self.customer(email="ada.c@example.com"), "ADA@example.com")
        before = snapshot()
        logs = merge(a.party, [b.party, c.party], actor="tester")
        self.assertEqual(len(logs), 2)
        self.assertEqual(len({log.merge_id for log in logs}), 1)
        self.assertEqual(Party.objects.filter(merged_into=a.party).count(), 2)
        with self.assertRaises(MergeError):
            unmerge(logs[0])  # newest first
        unmerge_group(logs[0].merge_id, actor="tester")
        self.assertEqual(snapshot(), before)

    def test_undo_refused_after_a_newer_merge_of_the_survivor(self):
        a, b = self.pair()
        c = self.customer(first_name="Grace", last_name="Hopper")
        (first,) = merge(a.party, [b.party], actor="tester")
        merge(a.party, [c.party], actor="tester")
        with self.assertRaisesMessage(MergeError, "newer merge"):
            unmerge(first)

    def test_undo_refused_when_moved_rows_changed_since(self):
        a, b = self.pair()
        (log,) = merge(a.party, [b.party], actor="tester")
        PartyPhone.objects.filter(party=a.party, number_e164="+12125550199").update(extension="9")
        with self.assertRaisesMessage(MergeError, "changed since"):
            unmerge(log)
        unmerge(log, force=True)
        self.assertIsNone(Party.objects.get(pk=b.party_id).merged_into_id)

    def test_field_picks_choose_names_and_primary(self):
        a, b = self.pair()
        Party.objects.filter(pk=b.party_id).update(given_name="Augusta", display_name="Augusta Lovelace")
        loser_phone = PartyPhone.objects.get(party=b.party)
        merge(a.party, [b.party], actor="tester",
              field_picks={"given_name": str(b.party_id), "primary_phone": str(loser_phone.pk)})
        survivor = Party.objects.get(pk=a.party_id)
        self.assertEqual((survivor.given_name, survivor.display_name), ("Augusta", "Augusta Lovelace"))
        self.assertEqual(survivor.primary_phone_e164, "+12125550199")
        self.assertEqual(PartyPhone.objects.filter(party=survivor, is_primary=True).count(), 1)

    def test_login_moves_to_a_survivor_without_one(self):
        a, b = self.pair()
        user = User.objects.create_user("ada")
        Party.objects.filter(pk=b.party_id).update(user=user)
        merge(a.party, [b.party], actor="tester")
        self.assertEqual(Party.objects.get(pk=a.party_id).user_id, user.pk)
        self.assertIsNone(Party.objects.get(pk=b.party_id).user_id)

    def test_symmetric_relationship_restored_in_canonical_order(self):
        a, b = self.pair()
        x = self.customer(first_name="Xavier", last_name="Q").party
        lo, hi = sorted([b.party, x], key=lambda p: p.pk)
        PartyRelationship.objects.create(from_party=lo, to_party=hi, kind="spouse")
        merge(a.party, [b.party], actor="tester")
        rel = PartyRelationship.objects.get(kind="spouse")
        self.assertLess(rel.from_party_id, rel.to_party_id)
        self.assertEqual({rel.from_party_id, rel.to_party_id}, {a.party_id, x.pk})

    def test_refusals(self):
        a, b = self.pair()
        with self.assertRaises(MergeError):
            merge(a.party, [], actor="tester")
        with self.assertRaises(MergeError):
            merge(a.party, [a.party], actor="tester")
        with self.assertRaises(MergeError):
            merge(a.party, [b.party], actor="")
        other = Company.objects.create(name="Other")
        foreign = Party.objects.create(company=other, kind="person", given_name="Ada", display_name="Ada",
                                       origin="erp")
        with self.assertRaisesMessage(MergeError, "different companies"):
            merge(a.party, [foreign], actor="tester")
        merge(a.party, [b.party], actor="tester")
        with self.assertRaisesMessage(MergeError, "already been merged"):
            merge(a.party, [b.party], actor="tester")

    def test_party_merged_signal_if_defined(self):
        from core.plugins import signals

        signal = getattr(signals, "party_merged", None)
        if signal is None:
            self.skipTest("party_merged arrives with step 10")
        calls = []

        def receiver(sender, **kwargs):
            calls.append(kwargs)

        signal.connect(receiver, weak=False)
        self.addCleanup(signal.disconnect, receiver)
        a, b = self.pair()
        with self.captureOnCommitCallbacks(execute=True):
            (log,) = merge(a.party, [b.party], actor="tester")
        self.assertEqual(calls[0]["merge_id"], log.merge_id)
        self.assertEqual([p.pk for p in calls[0]["merged"]], [b.party_id])


class BlockedTests(_Base):
    def blocked_pair(self):
        a, b = self.pair()
        Party.objects.filter(pk=a.party_id).update(user=User.objects.create_user("u1"))
        Party.objects.filter(pk=b.party_id).update(user=User.objects.create_user("u2"))
        return a, b

    def test_merge_refuses_two_logins(self):
        a, b = self.blocked_pair()
        with self.assertRaises(MergeBlocked) as caught:
            merge(a.party, [b.party], actor="tester")
        self.assertEqual(caught.exception.reasons, ["different_users"])

    def test_merge_refuses_self_party(self):
        self_party = Party.objects.create(company=self.company, kind="organisation", display_name="Acme",
                                          origin="system", is_self=True)
        org = Party.objects.create(company=self.company, kind="organisation", display_name="Acme",
                                   origin="erp")
        with self.assertRaises(MergeBlocked):
            merge(org, [self_party], actor="tester")

    def test_blocked_cluster_cannot_be_approved(self):
        self.blocked_pair()
        out = merge_queue.scan(self.company)
        row = out["batch"].rows.get()
        self.assertEqual(row.raw["blocked"], ["different_users"])
        self.assertFalse(row.raw["bulk_approvable"])
        with self.assertRaisesMessage(MergeError, "Blocked"):
            merge_queue.approve(row, actor="tester")
        self.assertEqual(merge_queue.bulk_approve([row], actor="tester"), {"approved": 0, "skipped": 1})
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, StagedRow.MappingState.PROPOSED)

    def test_block_found_live_after_the_scan(self):
        a, b = self.pair()
        row = merge_queue.scan(self.company)["batch"].rows.get()
        self.assertEqual(row.raw["blocked"], [])
        Party.objects.filter(pk=a.party_id).update(user=User.objects.create_user("u1"))
        Party.objects.filter(pk=b.party_id).update(user=User.objects.create_user("u2"))
        with self.assertRaisesMessage(MergeError, "Blocked"):
            merge_queue.approve(row, actor="tester")


class QueueTests(_Base):
    def test_scan_queues_a_cluster_with_masked_evidence(self):
        a, b = self.pair()
        out = merge_queue.scan(self.company)
        batch = out["batch"]
        self.assertEqual((batch.source_kind, batch.entity_kind), ("dedupe", "party_merge"))
        row = batch.rows.get()
        self.assertEqual(sorted(row.raw["party_ids"]), sorted([str(a.party_id), str(b.party_id)]))
        self.assertEqual(row.content_hash, merge_queue.cluster_hash([a.party_id, b.party_id]))
        self.assertEqual(row.mapping_confidence, merge_queue.BULK_THRESHOLD)
        self.assertIn({"rule": "R1", "value": "a…@example.com"}, row.raw["evidence"])
        self.assertNotIn("ada@example.com", str(row.raw))
        self.assertEqual(row.proposed_mapping["survivor"], str(a.party_id))  # oldest

    def test_default_survivor_prefers_the_account_holder(self):
        a, b = self.pair()
        b.user = User.objects.create_user("holder")
        b.save()
        b.refresh_from_db()
        self.assertEqual(b.account_holder_id, b.party_id)
        row = merge_queue.scan(self.company)["batch"].rows.get()
        self.assertEqual(row.proposed_mapping["survivor"], str(b.party_id))

    def test_dismissed_cluster_is_not_reproposed(self):
        self.pair()
        row = merge_queue.scan(self.company)["batch"].rows.get()
        merge_queue.dismiss(row, actor="tester")
        again = merge_queue.scan(self.company)
        self.assertEqual((again["clusters"], again["new"], again["suppressed"]), (1, 0, 1))
        self.assertEqual(again["batch"].rows.count(), 0)

    def test_queued_cluster_is_not_queued_twice(self):
        self.pair()
        merge_queue.scan(self.company)
        self.assertEqual(merge_queue.scan(self.company)["new"], 0)
        self.assertEqual(StagedRow.objects.filter(batch__entity_kind="party_merge").count(), 1)

    def test_vanished_cluster_goes_stale_and_can_return(self):
        a, b = self.pair()
        merge_queue.scan(self.company)
        PartyEmail.objects.filter(party=b.party).delete()
        out = merge_queue.scan(self.company)
        self.assertEqual(out["stale"], 1)
        self.assertEqual(StagedRow.objects.get().mapping_state, StagedRow.MappingState.FAILED)
        PartyEmail.objects.create(party=b.party, email="ada@example.com", purpose="personal")
        self.assertEqual(merge_queue.scan(self.company)["new"], 1)

    def test_scan_never_crosses_tenants(self):
        self.customer(email="ada@example.com")
        other = Company.objects.create(name="Other")
        # uniq_customer_email_ci_company is per tenant (2.2): the same address is fine here.
        Customer.objects.create(company=other, first_name="Ada", last_name="L", email="ada@example.com")
        self.assertEqual(merge_queue.scan(self.company)["clusters"], 0)

    def test_approve_then_apply_then_undo_run(self):
        a, b = self.pair()
        before = snapshot()
        row = merge_queue.scan(self.company)["batch"].rows.get()
        merge_queue.approve(row, actor="tester", survivor=b.party_id)
        run = start_apply_run(row.batch, actor="tester")
        out = apply_chunk(str(run.pk), [])
        self.assertEqual(out["results"][0]["state"], "applied", out)
        row.refresh_from_db()
        self.assertTrue(row.applied_ref.startswith("party_merge:"))
        self.assertEqual(Party.objects.get(pk=a.party_id).merged_into_id, b.party_id)
        log = PartyMergeLog.objects.get()
        self.assertEqual(log.approval_row.run_id, run.pk)
        undone = undo_run(str(run.pk), run.undo_token)
        self.assertEqual((undone["undone"], undone["refused"]), (1, []))
        self.assertEqual(snapshot(), before)

    def test_apply_fails_the_row_when_a_party_was_merged_meanwhile(self):
        a, b = self.pair()
        row = merge_queue.scan(self.company)["batch"].rows.get()
        merge_queue.approve(row, actor="tester")
        merge(a.party, [b.party], actor="someone-else")
        run = start_apply_run(row.batch, actor="tester")
        out = apply_chunk(str(run.pk), [])
        self.assertEqual(out["results"][0]["state"], "failed")
        self.assertIn("already been merged", out["results"][0]["error"])

    def test_propose_command(self):
        self.pair()
        out = StringIO()
        call_command("party_dedupe_propose", "--dry-run", stdout=out)
        self.assertIn("1 clusters, 1 queued", out.getvalue())
        self.assertFalse(ImportBatch.objects.exists())
        call_command("party_dedupe_propose", "--company", str(self.company.pk), stdout=out)
        self.assertEqual(StagedRow.objects.count(), 1)


class _QueryCounter:
    """Counts every statement (CaptureQueriesContext keeps only the last 9000)."""

    def __init__(self):
        self.count = 0

    def __call__(self, execute, sql, params, many, context):
        self.count += 1
        return execute(sql, params, many, context)


class TenThousandCandidateRunTests(_Base):
    """A 10k-candidate run applies through ``unify_apply`` in chunks (step 6 done-when)."""

    PAIRS = 5_000  # 10,000 candidate parties
    MAX_QUERIES_PER_CLUSTER = 60

    def test_10k_candidates_apply_through_unify_apply_with_bounded_queries(self):
        company = self.company
        parties = Party.objects.bulk_create([
            Party(company=company, kind="person", given_name=f"P{i // 2}", family_name="Dup",
                  display_name=f"P{i // 2} Dup", origin="import")
            for i in range(self.PAIRS * 2)
        ], batch_size=2000)
        PartyEmail.objects.bulk_create([
            PartyEmail(party=p, company=company, email=f"p{i // 2}@example.com",
                       email_normalized=f"p{i // 2}@example.com", purpose="personal", is_primary=True)
            for i, p in enumerate(parties)
        ], batch_size=2000)
        out = merge_queue.scan(company)
        self.assertEqual(out["new"], self.PAIRS)
        batch = out["batch"]
        approved = merge_queue.bulk_approve(batch.rows.all(), actor="tester")
        self.assertEqual(approved, {"approved": self.PAIRS, "skipped": 0})
        run = start_apply_run(batch, actor="tester")

        # One chunk alone: the per-row query cost is a constant.
        first = _QueryCounter()
        with connection.execute_wrapper(first):
            apply_chunk(str(run.pk), [], chunk_size=200)
        per_row = first.count / 200
        self.assertLess(per_row, self.MAX_QUERIES_PER_CLUSTER, f"{per_row:.1f} queries per merged cluster")

        stdout = StringIO()
        rest = _QueryCounter()
        with connection.execute_wrapper(rest):
            call_command("unify_apply", "--run", str(run.pk), stdout=stdout)
        remaining = self.PAIRS - 200
        self.assertLessEqual(rest.count, self.MAX_QUERIES_PER_CLUSTER * remaining + 100)
        self.assertIn(f"{remaining} applied, 0 failed", stdout.getvalue())
        self.assertGreaterEqual(stdout.getvalue().count("chunk "), remaining // 200)
        self.assertEqual(ApprovalRow.objects.filter(run=run, state="applied").count(), self.PAIRS)
        self.assertEqual(Party.objects.filter(company=company, merged_into__isnull=False).count(), self.PAIRS)
        self.assertEqual(PartyMergeLog.objects.count(), self.PAIRS)


class ReviewFixRegressionTests(_Base):
    """Shapes found in review: survivor-only role history, generic FKs, undo state, late dismissal."""

    def test_survivor_open_role_overlap_merges_and_undoes(self):
        a, b = self.pair()
        pa, pb = a.party, b.party
        PartyRole.objects.create(party=pa, role="lead", valid_from=datetime.date(2020, 1, 1),
                                 valid_to=datetime.date(2022, 1, 1))
        PartyRole.objects.create(party=pa, role="lead", valid_from=datetime.date(2021, 1, 1))
        PartyRole.objects.create(party=pb, role="lead", valid_from=datetime.date(2019, 1, 1),
                                 valid_to=datetime.date(2020, 6, 1))
        before = snapshot()
        (log,) = merge(pa, [pb], actor="tester")
        (kept,) = PartyRole.objects.filter(party=pa, role="lead")
        self.assertEqual((kept.valid_from, kept.valid_to), (datetime.date(2019, 1, 1), None))
        unmerge(log)
        self.assertEqual(snapshot(), before)

    def test_survivor_only_role_history_is_left_alone(self):
        a, b = self.pair()
        PartyRole.objects.create(party=a.party, role="lead", valid_from=datetime.date(2020, 1, 1),
                                 valid_to=datetime.date(2022, 1, 1))
        PartyRole.objects.create(party=a.party, role="lead", valid_from=datetime.date(2021, 1, 1),
                                 valid_to=datetime.date(2023, 1, 1))
        merge(a.party, [b.party], actor="tester")
        self.assertEqual(PartyRole.objects.filter(party=a.party, role="lead").count(), 2)

    def test_an_unnamed_fk_to_party_is_repointed(self):
        from unittest import mock

        from parties import merge as merge_module

        self.assertEqual(merge_module._related_party_fks(), [])  # every FK today is handled by name
        with mock.patch.object(merge_module, "HANDLED_TABLES", merge_module.HANDLED_TABLES - {"crm.Lead"}):
            self.assertEqual(merge_module._related_party_fks(), [(Lead, "party")])
            a, b = self.pair()
            lead = Lead.objects.create(company=self.company, first_name="L", lead_number="LD-G-1",
                                       email="g@example.com")
            Lead.objects.filter(pk=lead.pk).update(party=b.party)
            before = snapshot()
            (log,) = merge(a.party, [b.party], actor="tester")
            self.assertEqual(Lead.objects.get(pk=lead.pk).party_id, a.party_id)
            unmerge(log)
            self.assertEqual(snapshot(), before)

    def test_undone_merge_returns_to_proposed_and_is_not_reapplied(self):
        a, b = self.pair()
        row = merge_queue.scan(self.company)["batch"].rows.get()
        merge_queue.approve(row, actor="tester")
        run = start_apply_run(row.batch, actor="tester")
        apply_chunk(str(run.pk), [])
        undo_run(str(run.pk), run.undo_token)
        row.refresh_from_db()
        self.assertEqual((row.mapping_state, row.mapped_values), ("proposed", {}))
        second = start_apply_run(row.batch, actor="someone-else")
        self.assertEqual(second.rows.count(), 0)
        self.assertIsNone(Party.objects.get(pk=b.party_id).merged_into_id)

    def test_dismissal_after_the_run_opened_is_honoured(self):
        self.pair()
        row = merge_queue.scan(self.company)["batch"].rows.get()
        merge_queue.approve(row, actor="tester")
        run = start_apply_run(row.batch, actor="tester")
        merge_queue.dismiss(row, actor="tester")
        out = apply_chunk(str(run.pk), [])
        self.assertEqual(out["results"][0]["state"], "skipped")
        row.refresh_from_db()
        self.assertEqual(row.mapping_state, "na")
        self.assertFalse(PartyMergeLog.objects.exists())
        self.assertEqual(merge_queue.scan(self.company)["new"], 0)

    def test_imports_screen_approval_helpers_ignore_merge_batches(self):
        from unify.propose import approve_high_confidence, approve_rows

        self.pair()
        row = merge_queue.scan(self.company)["batch"].rows.get()
        self.assertEqual(approve_rows(row.batch, [row.pk], actor="tester"), 0)
        self.assertEqual(approve_high_confidence(row.batch, actor="tester"), 0)


@skipUnless(connection.features.has_select_for_update, "FOR UPDATE is a no-op on this backend")
class LockOrderTests(_Base):
    """Merge and undo lock Customer → Party → User, the order portal login and
    erasure take (core.identity.resolve.lock_login_rows); Party first would
    deadlock against them on Postgres."""

    def test_merge_and_undo_lock_in_the_login_order(self):
        a, b = self.pair()
        Customer.objects.filter(pk=b.pk).update(user=User.objects.create_user("merge-login"))
        with CaptureQueriesContext(connection) as queries:
            logs = merge(a.party, [b.party], actor="test")
        self.assertEqual(login_lock_order(queries), list(LOGIN_LOCK_ORDER))
        with CaptureQueriesContext(connection) as queries:
            unmerge(logs[0], actor="test")
        self.assertEqual(login_lock_order(queries), list(LOGIN_LOCK_ORDER))
