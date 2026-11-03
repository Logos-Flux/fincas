"""Dedupe engine (parties.dedupe) and party_dedupe_report (phase2-party.md step 4)."""

from __future__ import annotations

import io
import json
import re
import tempfile
import unicodedata
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from core.models import Company, UserProfile
from crm.models import Lead
from customers.models import Customer, CustomerAddress, CustomerContact
from parties.dedupe import (
    BLOCK_DIFFERENT_USERS,
    BLOCK_TWO_ACCOUNT_HOLDERS,
    FLAG_NEEDS_INDIVIDUAL_REVIEW,
    Node,
    cluster_id,
    email_near_misses,
    find_clusters,
    is_mixed_script,
)
from parties.dedupe_legacy import normalize_postcode
from parties.management.commands import party_dedupe_report as report_cmd


def person(key, company="A", **kw):
    return Node(key=key, company=company, kind="person", **kw)


def org(key, company="A", **kw):
    return Node(key=key, company=company, kind="organisation", **kw)


class EngineTests(SimpleTestCase):
    def test_same_phone_and_family_name_is_r2_not_r3(self):
        nodes = [
            person("a", phones={"+14155550100"}, names={("ann", "lee")}),
            person("b", phones={"+14155550100"}, names={("bo", "lee")}),
        ]
        [c] = find_clusters(nodes)
        self.assertEqual((c.rule, c.confidence), ("R2", 0.85))
        self.assertEqual([e.rule for e in c.evidence], ["R2"])

    def test_phone_shared_beyond_the_family_adds_r3(self):
        nodes = [
            person("a", phones={"+14155550100"}, names={("ann", "lee")}),
            person("b", phones={"+14155550100"}, names={("bo", "lee")}),
            person("c", phones={"+14155550100"}, names={("cy", "park")}),
        ]
        [c] = find_clusters(nodes)
        self.assertEqual(c.members, ["a", "b", "c"])
        self.assertEqual(sorted(e.rule for e in c.evidence), ["R2", "R3"])
        self.assertEqual((c.rule, c.weakest_rule), ("R2", "R3"))

    def test_keys_never_cross_tenants(self):
        nodes = [
            person("a", company="A", emails={"x@example.com"}),
            person("b", company="B", emails={"x@example.com"}),
            org("o1", company="A", org_names={"acme"}),
            org("o2", company="B", org_names={"acme"}),
        ]
        self.assertEqual(find_clusters(nodes), [])

    def test_weakest_link_and_r6_never_bulk(self):
        nodes = [
            org("o1", org_names={"acme"}, domains={"acme.test"}),
            org("o2", org_names={"acme"}, domains={"acme.test"}),
            org("o3", org_names={"globex"}, domains={"acme.test"}),
        ]
        [c] = find_clusters(nodes)
        self.assertEqual((c.rule, c.confidence), ("R5", 0.70))
        self.assertEqual((c.weakest_rule, c.weakest_link), ("R6", 0.30))
        self.assertFalse(c.bulk_approvable)
        [pair] = find_clusters(nodes[:2])
        self.assertEqual(pair.weakest_rule, "R5")
        self.assertTrue(pair.bulk_approvable)

    def test_flags_and_blocks(self):
        users = find_clusters([
            person("a", emails={"x@e.test"}, user="u1"),
            person("b", emails={"x@e.test"}, user="u2"),
        ])[0]
        self.assertEqual(users.blocked, [BLOCK_DIFFERENT_USERS])
        self.assertEqual(users.flags, [FLAG_NEEDS_INDIVIDUAL_REVIEW])
        staff = find_clusters([person("a", emails={"x@e.test"}, is_staff=True), person("b", emails={"x@e.test"})])[0]
        self.assertEqual((staff.flags, staff.blocked), ([FLAG_NEEDS_INDIVIDUAL_REVIEW], []))
        holders = find_clusters([
            person("a", emails={"x@e.test"}, is_account_holder=True),
            person("b", emails={"x@e.test"}, is_account_holder=True),
        ])[0]
        self.assertEqual(holders.blocked, [BLOCK_TWO_ACCOUNT_HOLDERS])

    def test_c1_link_to_an_unknown_node_is_dropped(self):
        self.assertEqual(find_clusters([person("lead:1", links={"customer:gone"})]), [])

    def test_c1_link_across_tenants_is_dropped(self):
        nodes = [person("lead:1", company="A", links={"customer:9"}), person("customer:9", company="B")]
        self.assertEqual(find_clusters(nodes), [])

    def test_cluster_id_is_order_independent(self):
        self.assertEqual(cluster_id(["b", "a"]), cluster_id(["a", "b"]))

    def test_near_misses_and_scripts(self):
        nodes = [
            person("a", emails={"first.last@gmail.com"}),
            person("b", emails={"firstlast+x@gmail.com"}),
            person("c", emails={"other@gmail.com"}),
        ]
        self.assertEqual(email_near_misses(nodes), [("a", "b")])
        self.assertTrue(is_mixed_script("pаypal"))  # Cyrillic а
        self.assertFalse(is_mixed_script("josé.1"))

    def test_postcode_key(self):
        self.assertEqual(normalize_postcode("94110-1234", "us"), ("94110", "US"))
        self.assertEqual(normalize_postcode(" sw1a 1aa ", "GB"), ("SW1A1AA", "GB"))
        self.assertEqual(normalize_postcode("", "US"), ("", ""))


class ReportFixture(TestCase):
    """Two tenants with known duplicates. Expected clusters are listed in
    ``expected`` and asserted exactly."""

    @classmethod
    def setUpTestData(cls):
        a = cls.a = Company.objects.create(name="Tenant A")
        b = cls.b = Company.objects.create(name="Tenant B")

        def cust(company, first, last, email, **kw):
            return Customer.objects.create(company=company, first_name=first, last_name=last, email=email, **kw)

        def lead(company, first, last, email, **kw):
            return Lead.objects.create(company=company, first_name=first, last_name=last, email=email, **kw)

        biz = Customer.CustomerType.BUSINESS
        # 1. Unicode names, email case, phone with/without country code (R1, R2, R4).
        cls.c1 = cust(a, "Zoë", "Müller", "Zoe.Mueller@Example.com", phone="(415) 555-0100")
        CustomerAddress.objects.create(
            customer=cls.c1, company=a, address_line_1="1 Main", city="SF", state_province="CA",
            postal_code="94110-1234", country="US",
        )
        cls.l1 = lead(a, "Zoë", "Müller", "zoe.mueller@EXAMPLE.com", phone="+1 415 555 0100")
        # decomposed ë / Ü (NFKC folds them), upper case; matched on name + postcode only
        cls.l2 = lead(a, unicodedata.normalize("NFD", "ZOË"), unicodedata.normalize("NFD", "MÜLLER"), "",
                      postal_code="94110", country="US")
        # 2. Same org under Inc / inc. variants (R5), plus one joined only by domain (R6).
        cls.c2 = cust(a, "Bob", "Ray", "bob@acmewidgets.com", customer_type=biz,
                      company_name="Acme Widgets, Inc.", phone="415-555-0199")
        cls.c3 = cust(a, "Alice", "Ng", "alice@acmewidgets.com", customer_type=biz, company_name="ACME WIDGETS inc")
        cls.c4 = cust(a, "Carol", "Diaz", "carol@acmewidgets.com", customer_type=biz, company_name="Globex LLC")
        # A1: contact on c2 with the customer's own email (case variant).
        cls.k1 = CustomerContact.objects.create(customer=cls.c2, first_name="Bob", last_name="Ray",
                                                email="BOB@acmewidgets.com")
        # A2: two contacts on c3 sharing an email.
        cls.k2 = CustomerContact.objects.create(customer=cls.c3, first_name="Eve", last_name="Ko",
                                                email="eve@acmewidgets.com")
        cls.k3 = CustomerContact.objects.create(customer=cls.c3, first_name="Eve", last_name="Ko",
                                                email="Eve@AcmeWidgets.com")
        # 3. Same E.164 only (R3).
        cls.c5 = cust(a, "Dan", "Smith", "dan@example.org", phone="4155550123")
        cls.l3 = lead(a, "Dana", "Jones", "dana@example.org", phone="+14155550123")
        # 4. Two account holders (each has a portal login) sharing a phone and
        # family name: blocked. (Before step 11 their emails differed only in
        # case; uniq_customer_email_ci_company forbids that now.)
        login = get_user_model().objects.create_user
        cls.c6 = cust(a, "Sam", "Lee", "sam@example.net", phone="4155550177", user=login("sam-portal-1"))
        cls.c7 = cust(a, "Sam", "Lee", "sam.lee@example.net", phone="415-555-0177", user=login("sam-portal-2"))
        # 5. Converted lead with a different email (C1).
        cls.c10 = cust(a, "Pat", "Kim", "pat@example.org")
        cls.l7 = lead(a, "Pat", "Kim", "pkim@other.example", converted_to_customer=cls.c10)
        # Noise: no usable key, null company, gmail-style near miss.
        cls.l4 = lead(a, "No", "Key", "", phone="555-0143")
        lead(None, "Nobody", "Home", "nobody@example.com")
        cls.c9 = cust(a, "First", "Last", "first.last@gmail.com")
        cls.l6 = lead(a, "Other", "Person", "firstlast+x@gmail.com")
        # Tenant B: the same humans and org; must never join tenant A's clusters.
        cls.c8 = cust(b, "Zoë", "Müller", "ZOE.MUELLER@example.com", phone="+14155550100")
        cls.c11 = cust(b, "Ann", "Bo", "ann@acmewidgets.com", customer_type=biz, company_name="Acme Widgets Ltd")
        # A tenant-B lead converted to a tenant-A customer: no cross-tenant C1.
        cls.l8 = lead(b, "Pat", "Kim", "pat.b@other.example", converted_to_customer=cls.c10)

    def expected(self):
        k = lambda prefix, obj: f"{prefix}:{obj.pk}"  # noqa: E731
        return {
            frozenset({k("customer", self.c1), k("lead", self.l1), k("lead", self.l2)}): ("R1", 0.95, "R4", 0.50),
            frozenset({k("org", self.c2), k("org", self.c3), k("org", self.c4)}): ("R5", 0.70, "R6", 0.30),
            frozenset({k("customer", self.c5), k("lead", self.l3)}): ("R3", 0.60, "R3", 0.60),
            frozenset({k("customer", self.c6), k("customer", self.c7)}): ("R2", 0.85, "R2", 0.85),
            frozenset({k("customer", self.c10), k("lead", self.l7)}): ("C1", 0.99, "C1", 0.99),
        }

    def run_report(self, *args):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            # These rows are written through save(), so the step-5 sync has
            # made parties too: pin the legacy builder this fixture describes.
            source = () if "--source" in args else ("--source", "legacy")
            call_command("party_dedupe_report", "--json", str(path), *source, *args, stdout=out)
            return json.loads(path.read_text()), out.getvalue()


class ReportTests(ReportFixture):
    def test_exact_clusters_and_confidences(self):
        report, _ = self.run_report()
        got = {
            frozenset(m["key"] for m in c["members"]): (c["rule"], c["confidence"], c["weakest_rule"], c["weakest_link"])
            for c in report["clusters"]
        }
        self.assertEqual(got, self.expected())
        a = str(self.a.pk)
        self.assertTrue(all(c["company"] == a for c in report["clusters"]))

    def test_cluster_review_state(self):
        report, _ = self.run_report()
        by_rule = {(c["rule"], c["size"]): c for c in report["clusters"]}
        blocked = [c for c in report["clusters"] if c["blocked"]]
        self.assertEqual(len(blocked), 1)
        # Each holds a portal login, so the logins differ too.
        self.assertEqual(blocked[0]["blocked"], [BLOCK_TWO_ACCOUNT_HOLDERS, BLOCK_DIFFERENT_USERS])
        self.assertEqual(blocked[0]["flags"], [FLAG_NEEDS_INDIVIDUAL_REVIEW])
        self.assertFalse(by_rule[("R5", 3)]["bulk_approvable"])  # held by an R6 link
        self.assertTrue(by_rule[("R1", 3)]["bulk_approvable"])
        self.assertEqual(report["review"], {
            "bulk_approvable": 3, "needs_individual_review": 1, "blocked": {"different_users": 1, "two_account_holders": 1},
        })

    def test_counts(self):
        report, _ = self.run_report()
        self.assertEqual(report["rows"], {"customers": 11, "contacts": 3, "leads": 7})
        self.assertEqual(report["applied"], {"A1": 1, "A2": 1})
        self.assertEqual(report["histogram"], {"2": 3, "3": 2})
        rules = report["rules"]
        self.assertEqual({r: rules[r]["clusters"] for r in rules},
                         {"C1": 1, "R1": 1, "R2": 1, "R3": 1, "R4": 0, "R5": 1, "R6": 0})
        self.assertEqual({r: rules[r]["blocks"] for r in rules},
                         {"C1": 1, "R1": 1, "R2": 2, "R3": 1, "R4": 1, "R5": 1, "R6": 1})
        self.assertEqual(report["unusable"]["no_usable_key"], [f"lead:{self.l4.pk}"])
        self.assertEqual(report["unusable"]["unparseable_phone"], {"leads": 1})
        self.assertEqual(report["null_company"], {"customers": 0, "leads": 1})
        self.assertEqual(report["cross_tenant_conversions"], [f"lead:{self.l8.pk}"])
        self.assertEqual(report["near_misses"]["email_folding"],
                         [sorted([f"customer:{self.c9.pk}", f"lead:{self.l6.pk}"])])

    def test_company_scope(self):
        report, _ = self.run_report("--company", str(self.b.pk))
        self.assertEqual(report["clusters"], [])
        self.assertEqual(report["rows"]["customers"], 2)
        report, _ = self.run_report("--company", "Tenant A")
        self.assertEqual(len(report["clusters"]), 5)
        with self.assertRaises(CommandError):
            call_command("party_dedupe_report", "--company", "Nope", stdout=io.StringIO())

    def test_json_schema(self):
        report, _ = self.run_report()
        self.assertEqual(report["schema"], "party_dedupe_report/1")
        self.assertEqual(set(report), {
            "schema", "generated_at", "show_pii", "source", "companies", "rows", "nodes", "applied", "rules",
            "clusters_total", "nodes_in_clusters", "histogram", "review", "unusable", "name_inferred_orgs",
            "mixed_script_emails", "cross_tenant_conversions", "near_misses", "null_company", "clusters",
        })
        for rule in report["rules"].values():
            self.assertEqual(set(rule), {"confidence", "kind", "description", "blocks", "clusters"})
        cluster = report["clusters"][0]
        self.assertEqual(set(cluster), {
            "id", "company", "kind", "size", "rule", "confidence", "weakest_rule", "weakest_link",
            "bulk_approvable", "flags", "blocked", "members", "evidence",
        })
        self.assertEqual(set(cluster["members"][0]), {"key", "kind", "sources"})
        self.assertEqual(set(cluster["evidence"][0]), {"rule", "confidence", "members"})
        self.assertEqual(report["clusters_total"], len(report["clusters"]))

    def test_no_pii_by_default(self):
        report, text = self.run_report()
        blob = json.dumps(report) + text
        # Cluster ids (16 hex chars) and UUIDs are random hex, so a digit-only
        # needle such as a postcode can appear inside one by chance (CI saw
        # "94110" inside cluster id 8b9c94110a5382b8). Redact them first.
        blob = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{16}\b", "<id>", blob)
        for needle in ("mueller", "Müller", "acmewidgets", "Acme", "Tenant A", "+1415", "94110", "pkim", "Zoë"):
            self.assertNotIn(needle.lower(), blob.lower())

    def test_show_pii(self):
        report, text = self.run_report("--show-pii")
        self.assertIn("zoe.mueller@example.com", text)
        member = next(m for c in report["clusters"] for m in c["members"] if m["key"] == f"customer:{self.c1.pk}")
        self.assertEqual(member["name"], "Zoë Müller")
        self.assertIn("value", report["clusters"][0]["evidence"][0])

    def test_text_output(self):
        _, text = self.run_report("--top", "2")
        self.assertIn("Top 2 clusters:", text)
        self.assertIn("Cluster sizes: 2: 3, 3: 2", text)
        self.assertIn("No company (skipped): 0 customers, 1 leads", text)


class ReadOnlyTests(ReportFixture):
    WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE)\b", re.I)

    def test_issues_no_write_statements(self):
        staff = get_user_model().objects.create_user("ops", email="sam@example.net", is_staff=True)
        UserProfile.objects.create(user=staff, company=self.a)
        with CaptureQueriesContext(connection) as ctx:
            call_command("party_dedupe_report", "--show-pii", stdout=io.StringIO())
        self.assertGreater(len(ctx.captured_queries), 0)
        writes = [q["sql"] for q in ctx.captured_queries if self.WRITE.match(q["sql"])]
        self.assertEqual(writes, [])

    def test_a_write_during_the_run_is_refused(self):
        def writes(*args, **kwargs):
            Company.objects.create(name="sneaky")

        with mock.patch.object(report_cmd, "build_report", side_effect=writes):
            with self.assertRaises(report_cmd.WriteAttempted):
                call_command("party_dedupe_report", stdout=io.StringIO())

    def zoe_cluster(self, report):
        key = f"customer:{self.c1.pk}"
        [c] = [c for c in report["clusters"] if key in {m["key"] for m in c["members"]}]
        return c

    def test_staff_email_flags_the_cluster(self):
        before = self.zoe_cluster(report_cmd.build_report([self.a]))
        self.assertEqual((before["flags"], before["bulk_approvable"]), ([], True))
        staff = get_user_model().objects.create_user("ops", email="ZOE.MUELLER@example.com", is_staff=True)
        UserProfile.objects.create(user=staff, company=self.a)
        after = self.zoe_cluster(report_cmd.build_report([self.a]))
        self.assertEqual((after["flags"], after["bulk_approvable"]), ([FLAG_NEEDS_INDIVIDUAL_REVIEW], False))

    def test_staff_of_another_tenant_does_not_flag(self):
        staff = get_user_model().objects.create_user("ops", email="zoe.mueller@example.com", is_staff=True)
        UserProfile.objects.create(user=staff, company=self.b)
        self.assertEqual(self.zoe_cluster(report_cmd.build_report([self.a]))["flags"], [])

    def test_guard_refuses_disguised_writes(self):
        for sql in ("/* x */ INSERT INTO t VALUES (1)", "WITH x AS (SELECT 1) DELETE FROM t",
                    "SELECT * FROM t FOR UPDATE", "UPDATE t SET a = 1"):
            with self.subTest(sql=sql), self.assertRaises(report_cmd.WriteAttempted):
                report_cmd._refuse_writes(None, sql, (), False, {})
        self.assertEqual(report_cmd._refuse_writes(lambda *a: "ok", "-- c\n SELECT 1", (), False, {}), "ok")
