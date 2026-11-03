"""AI-assisted matching (phase5-importer.md 3.5, 5.3 "AI"; step 10).

Every call goes through the real ``llmhub`` client into ``mock_http``: the
tests see the exact bytes a gateway would receive, and answer from recorded
outputs (``fixtures/ai/recorded.json``, keyed by the SHA-256 of the request's
user message, which also pins those bytes: a change to the payload fails
here until the recording is redone). Nothing opens a socket
(``core.testing.no_network``), so the recording is authored, not captured
from a live model: ``oracle`` below wrote it. To redo it after a deliberate
payload change, run this module with ``CRM_AI_RECORD=1`` and review the diff
of ``recorded.json``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from decimal import Decimal
from pathlib import Path

import httpx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from core.models import Company
from crm_import import ai, match
from crm_import import stage as staging
from crm_import.models import ImportJob, ImportJobBatch
from llmhub.tests.helpers import ISOLATED, HubTestCase, completion_body, json_response, mock_http, save_gateway
from parties.models import Party
from unify.models import ImportBatch, StagedRow

from .staging_helpers import ExportDir, company, contact, make_org, make_person

FIXTURE = Path(__file__).parent / "fixtures" / "ai" / "recorded.json"
LANE = "crm-import-lane"
PURPOSES = {
    **ISOLATED["LLMHUB_PURPOSES"],
    "crm_match_propose": {"label": "CRM import match suggestions", "kind": "chat"},
    "crm_dedupe_hint": {"label": "CRM import duplicate hints", "kind": "chat"},
}
SETTINGS = {**ISOLATED, "LLMHUB_PURPOSES": PURPOSES, "GOLDEN_CRM_IMPORT": {"ai_budget_per_batch": 500}}
LANES = {"crm_match_propose": LANE, "crm_dedupe_hint": LANE}

# The people and organisations of the fixtures. None of these strings (nor
# any part of an address or number) may reach the gateway.
GOLDEN_PEOPLE = [
    dict(given="Grace", family="Hopper", emails=["grace.h@example.com"], phones=["+1 202-555-0101"]),
    dict(given="Alan", family="Turing", postcode="02139", country="US"),
    dict(given="Edsger", family="Dijkstra", emails=["ed.b.desk@example.org"]),
    dict(given="Barbara", family="Liskov", emails=["ed.b.desk@example.org"]),
]
GOLDEN_ORGS = [dict(name="Widget Works", domain="widgetworks.example")]


def incoming():
    return {
        "contact": [
            contact("hs-grace-7001", given="Grace", family="Hopper", emails=["gh@example.net"],
                    phones=["+1 202 555 0101"]),
            contact("hs-alan-7002", given="Alan", family="Turing",
                    addresses=[{"postal_code": "02139", "country": "US"}]),
            contact("hs-edsger-7003", given="Edsger", family="Dijkstra", emails=["ed.b.desk@example.org"]),
            contact("hs-ada-7004", given="Ada", family="Lovelace", emails=["ada@example.com"]),
            contact("hs-mary-7005", given="Mary", family="Hopper", phones=["+1 (202) 555-0101"]),
            contact("hs-kath-7006", given="Katherine", family="Johnson",
                    addresses=[{"postal_code": "23666", "country": "US"}]),
            contact("hs-kath-7007", given="Katherine", family="Johnson",
                    addresses=[{"postal_code": "23666", "country": "US"}]),
        ],
        "company": [company("hs-widget-8001", name="Widget Works Ltd", domains=["widgetworks.example"])],
    }


def secrets_of_fixtures() -> set[str]:
    """Every value of the fixtures that is personal data or an identifier."""
    out = set()
    for records in incoming().values():
        for r in records:
            out.add(r["source_id"])
            d = r["data"]
            for key in ("given_name", "family_name", "name"):
                if d.get(key):
                    out.add(d[key])
            for e in d.get("emails") or []:
                local, _, domain = e["address"].partition("@")
                out |= {e["address"], local, domain}
            for p in d.get("phones") or []:
                digits = re.sub(r"\D", "", p["raw"])
                out |= {p["raw"], digits, digits[-4:], digits[-7:]}
            for a in d.get("addresses") or []:
                out.add(a["postal_code"])
            for dom in d.get("domains") or []:
                out.add(dom)
    for p in GOLDEN_PEOPLE:
        out |= {p["given"], p["family"], *(p.get("emails") or []), p.get("postcode") or ""}
    out |= {o["name"] for o in GOLDEN_ORGS} | {o["domain"] for o in GOLDEN_ORGS}
    return {s for s in out if s}


def content_key(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


RECORD = os.environ.get("CRM_AI_RECORD") == "1"


def oracle(system: str, payload: dict) -> str:
    """The answers ``recorded.json`` holds: what a careful reviewer would say
    from the features alone."""
    if system == ai.MATCH_SYSTEM:
        for cand in payload["candidates"]:
            f = cand["features"]
            if f.get("org_name_same") or (f.get("family_name_same") and f.get("given_name") in ("same", "initial")):
                return json.dumps({"suggested": cand["handle"], "confidence": 0.9,
                                   "reason": "same name and the rule's evidence"})
        return json.dumps({"suggested": "create", "confidence": 0.8, "reason": "names differ"})
    return json.dumps({"pairs": [
        {"pair": p["handle"], "same": p["features"].get("name_similarity") == "exact"
         and bool(p["features"].get("postcode_same") or p["features"].get("email_same")),
         "confidence": 0.85, "reason": "name and place agree" if p["features"].get("name_similarity") == "exact"
         else "names differ"}
        for p in payload["pairs"]]})


class Gateway:
    """Answers each request from the recording; logs every request body."""

    def __init__(self, override=None):
        self.recorded = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}
        self.override = override  # (purpose system prompt, payload) -> text, or None for the recording
        self.bodies: list[bytes] = []
        self.missing: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        body = json.loads(request.content)
        system, user = body["messages"][0]["content"], body["messages"][-1]["content"]
        if self.override is not None:
            text = self.override(system, json.loads(user))
            if isinstance(text, httpx.Response):
                return text
            if text is not None:
                return json_response(body=completion_body(text, model="recorded-model"))
        entry = self.recorded.get(content_key(user))
        if RECORD and entry is None:
            entry = {"request": user, "response": oracle(system, json.loads(user))}
            self.recorded[content_key(user)] = entry
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(json.dumps(self.recorded, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        if entry is None or entry["request"] != user:
            self.missing.append(user)
            return json_response(500, {"error": "no recording for this request"})
        return json_response(body=completion_body(entry["response"], model="recorded-model"),
                             headers={"x-litellm-model-name": "recorded-model"})

    def payloads(self):
        return [json.loads(json.loads(b)["messages"][-1]["content"]) for b in self.bodies]


def proposal_core(row: StagedRow) -> dict:
    """What a decision depends on: everything but the AI's own keys."""
    p = dict(row.proposed_mapping or {})
    for key in ("ai", "ai_duplicates", "possible_duplicate_of"):
        p.pop(key, None)
    p["flags"] = [f for f in p.get("flags") or [] if f != ai.FLAG_AI_DISAGREES]
    return {"proposal": p, "confidence": row.mapping_confidence, "state": row.mapping_state,
            "mapped_values": row.mapped_values, "error": row.error}


@override_settings(**SETTINGS)
class Base(HubTestCase):
    def setUp(self):
        super().setUp()
        self.co = Company.objects.create(name="Tenant A")
        self.other = Company.objects.create(name="Tenant B")
        self.people = [make_person(self.co, **p) for p in GOLDEN_PEOPLE]
        self.orgs = [make_org(self.co, o["name"], domain=o["domain"]) for o in GOLDEN_ORGS]

    def stage(self, *, ai_match=True, co=None, lanes=LANES, gateway=None, records=None):
        if lanes is not None:
            save_gateway(lane_map={"chat_default": "erp-lane", **lanes})
        export = ExportDir(self, records or incoming())
        gateway = gateway or Gateway()
        with mock_http(gateway):
            job = staging.stage(co or self.co, export.path, options={"ai_match": ai_match})
        return job, gateway

    def rows(self, job, otype="contact"):
        return {r.raw["source_id"]: r for r in StagedRow.objects.filter(staging.job_rows_filter([job.pk], otype))}


class OffByDefaultTests(Base):
    def test_job_without_opt_in_sends_nothing(self):
        job, gw = self.stage(ai_match=False)
        self.assertEqual(gw.bodies, [])
        self.assertNotIn("ai", job.report)
        self.assertTrue(all(r.proposed_mapping.get("ai") is None for r in self.rows(job).values()))
        with mock_http(gw):
            self.assertEqual(ai.run(job)["enabled"], False)
        self.assertEqual(gw.bodies, [])

    def test_opt_in_without_a_route_sends_nothing_and_says_why(self):
        job, gw = self.stage(lanes={})
        self.assertEqual(gw.bodies, [])
        contact_ai = job.report["ai"]["contact"]["match"]
        self.assertEqual(contact_ai["skipped"], "no_route")
        self.assertFalse(contact_ai["route"]["ok"])
        self.assertIn("No route for purpose 'crm_match_propose'", contact_ai["route"]["reason"])

    def test_route_status_names_the_lane_without_calling_it(self):
        save_gateway(lane_map={"chat_default": "erp-lane", **LANES})
        with mock_http(Gateway()) as calls:
            status = ai.route_status()
        self.assertEqual((status.ok, status.route, status.mode), (True, LANE, "gateway"))
        self.assertEqual(calls, [])

    def test_staging_sets_the_batch_budget_from_settings(self):
        with self.settings(GOLDEN_CRM_IMPORT={"ai_budget_per_batch": 7}):
            job, _gw = self.stage(ai_match=False)
        budgets = set(ImportJobBatch.objects.filter(job=job).values_list("batch__llm_call_budget", flat=True))
        self.assertEqual(budgets, {7})


class SuggestionTests(Base):
    def test_recorded_suggestions_are_stored_and_flag_disagreement(self):
        job, gw = self.stage()
        self.assertEqual(gw.missing, [], "a request has no recording: see the module docstring (CRM_AI_RECORD=1)")
        rows = self.rows(job)
        grace = rows["hs-grace-7001"].proposed_mapping
        self.assertEqual(grace["decision"], "link")
        self.assertEqual(grace["ai"]["suggested"], grace["target"]["id"])
        self.assertFalse(grace["ai"]["disagrees"])
        self.assertNotIn("ai_disagrees", grace["flags"])
        mary = rows["hs-mary-7005"]
        self.assertEqual(mary.proposed_mapping["decision"], "link")  # the rule's decision stands
        self.assertEqual(mary.proposed_mapping["ai"]["suggested"], "create")
        self.assertTrue(mary.proposed_mapping["ai"]["disagrees"])
        self.assertIn("ai_disagrees", mary.proposed_mapping["flags"])
        self.assertFalse(match.bulk_approvable(mary.proposed_mapping, mary.mapping_confidence))
        edsger = rows["hs-edsger-7003"].proposed_mapping
        self.assertIsNone(edsger["decision"])  # still undecided: a suggestion is not a decision
        self.assertEqual(edsger["ai"]["suggested"], str(self.people[2].pk))
        self.assertFalse(edsger["ai"]["disagrees"])
        self.assertIsNone(rows["hs-ada-7004"].proposed_mapping["ai"])  # create 1.0: never sent
        widget = self.rows(job, "company")["hs-widget-8001"].proposed_mapping
        self.assertEqual(widget["ai"]["suggested"], str(self.orgs[0].pk))
        summary = job.report["ai"]["contact"]["match"]
        self.assertEqual((summary["eligible"], summary["suggested"], summary["disagrees"]), (4, 4, 1))
        self.assertFalse(job.report["ai"]["contact"]["budget_exhausted"])

    def test_the_ai_never_changes_a_decision(self):
        job, _gw = self.stage(ai_match=False)
        before = {k: proposal_core(r) for k, r in self.rows(job).items()}
        before.update({k: proposal_core(r) for k, r in self.rows(job, "company").items()})
        approved = set(StagedRow.objects.filter(staging.job_rows_filter([job.pk], "contact"),
                                                mapping_state="approved").values_list("pk", flat=True))
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])

        def confident(system, payload):  # the most forceful answer possible, for every row
            if system == ai.MATCH_SYSTEM:
                return json.dumps({"suggested": payload["candidates"][-1]["handle"], "confidence": 1.0,
                                   "reason": "certain"})
            return json.dumps({"pairs": [{"pair": p["handle"], "same": True, "confidence": 1.0}
                                         for p in payload["pairs"]]})

        with mock_http(Gateway(confident)) as calls:
            ai.run(job)
        self.assertTrue(calls)
        after = {k: proposal_core(r) for k, r in self.rows(job).items()}
        after.update({k: proposal_core(r) for k, r in self.rows(job, "company").items()})
        self.assertEqual(after, before)
        self.assertEqual(set(StagedRow.objects.filter(staging.job_rows_filter([job.pk], "contact"),
                                                      mapping_state="approved").values_list("pk", flat=True)),
                         approved)
        self.assertEqual(Party.objects.filter(company=self.co).count(), len(GOLDEN_PEOPLE) + len(GOLDEN_ORGS))

    def test_recorded_outputs_are_byte_stable(self):
        _job, first = self.stage()
        other = Company.objects.create(name="Tenant C")
        for p in GOLDEN_PEOPLE:
            make_person(other, **p)
        for o in GOLDEN_ORGS:
            make_org(other, o["name"], domain=o["domain"])
        _job2, second = self.stage(co=other)
        self.assertEqual([json.loads(b)["messages"] for b in first.bodies],
                         [json.loads(b)["messages"] for b in second.bodies])
        self.assertEqual(first.missing + second.missing, [])
        self.assertEqual(len(first.bodies), len(json.loads(FIXTURE.read_text())))

    def test_suggestion_on_a_row_decided_meanwhile_is_not_written(self):
        job, _gw = self.stage(ai_match=False)
        grace = self.rows(job)["hs-grace-7001"]
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])

        original = ai._call

        def reviewer_acts_first(purpose, system, payload, result, limits=None):
            first = (payload.get("candidates") or [{}])[0]
            if first.get("rule") == "R2" and first["features"]["given_name"] == "same":
                staging.skip_row(StagedRow.objects.get(pk=grace.pk), "staff:reviewer")
            return original(purpose, system, payload, result, limits)

        from unittest import mock

        # The skipped row leaves the duplicate pairs too: that dedupe payload has no recording.
        gw = Gateway(lambda system, payload: oracle(system, payload) if system == ai.DEDUPE_SYSTEM else None)
        with mock.patch.object(ai, "_call", reviewer_acts_first), mock_http(gw):
            summary = ai.run(job)
        grace.refresh_from_db()
        self.assertEqual(grace.mapping_state, "na")
        self.assertIsNone(grace.proposed_mapping.get("ai"))
        self.assertEqual(summary["contact"]["match"]["changed"], 1)


class MinimisationTests(Base):
    def test_no_payload_carries_personal_data_or_free_text(self):
        job, gw = self.stage()
        self.assertGreaterEqual(len(gw.bodies), 6)
        secrets = secrets_of_fixtures() | {str(p.pk) for p in Party.objects.all()} | {str(job.pk)}
        email = re.compile(r"[^\s\"@]+@[^\s\"@]+")
        phone = re.compile(r"\d[\d ().-]{5,}\d")
        for raw in gw.bodies:
            text = raw.decode()
            body = json.loads(raw)
            self.assertEqual(set(body), {"model", "messages", "stream", "max_tokens", "temperature"})
            self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
            self.assertIn(body["messages"][0]["content"], (ai.MATCH_SYSTEM, ai.DEDUPE_SYSTEM))
            user = body["messages"][1]["content"]
            self.assertFalse(email.search(user), user)
            self.assertFalse(phone.search(user), user)
            self.assertFalse(re.search(r"\d{4,}", user), user)
            payload = json.loads(user)
            self.assertEqual(ai.payload_violations(payload), [])
            for value in _strings(payload):
                # Every string is a closed-vocabulary token: no names, no prose.
                self.assertRegex(value, r"^[a-z0-9_]{1,40}$|^R[1-6]$")
            for secret in secrets:
                # Short values ("gh", "0101") as whole tokens: "high" is not a leak of "gh".
                pattern = re.escape(secret.lower()) if len(secret) >= 5 else rf"\b{re.escape(secret.lower())}\b"
                self.assertIsNone(re.search(pattern, text.lower()), f"{secret!r} reached the gateway")

    def test_violations_catch_what_must_never_be_sent(self):
        self.assertIn(ai.FLAG_AI_DISAGREES, match.FLAGS)
        cand = {"handle": "c1", "rule": "R2", "rule_confidence": 0.85,
                "features": {"phone_same": True, "given_name": "same", "name_similarity": None}}
        ok = {"kind": "person", "rule_decision": "none", "rule_pick": None, "flags": [], "candidates": [cand]}
        self.assertEqual(ai.payload_violations(ok), [])
        for bad in ({**ok, "name": "x"}, {**ok, "kind": "Grace Hopper"}, {**ok, "flags": ["gh@example.net"]},
                    {**ok, "kind": "high"},  # a real token, under the wrong key
                    {**ok, "rule_pick": "c1\n"},
                    {**ok, "candidates": [{**cand, "rule_confidence": 2025}]},
                    {**ok, "candidates": [{**cand, "rule_confidence": 0.2025550101}]},
                    {**ok, "candidates": [{**cand, "handle": "x" * 50}]},
                    {**ok, "candidates": [{**cand, "features": {"given_name": "Grace"}}]},
                    {**ok, "candidates": [{**cand, "features": {"phone_same": 1}}]},
                    {**ok, "candidates": [{**cand, "features": {"phone_same": "true"}}]}):
            problems = ai.payload_violations(bad)
            self.assertTrue(problems, bad)
            self.assertNotIn("Grace", " ".join(problems))

    def test_a_payload_that_fails_minimisation_is_never_sent(self):
        job, _gw = self.stage(ai_match=False)
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])
        original = ai.match_payload

        def leaky(*args, **kwargs):
            return {**original(*args, **kwargs), "note": "called Grace about the renewal"}

        from unittest import mock

        with mock.patch.object(ai, "match_payload", leaky), mock_http(Gateway()) as calls:
            summary = ai.run(job)
        self.assertEqual([c for c in calls if b"candidates" in c.content], [])
        self.assertIn("failed minimization", summary["contact"]["error"])
        self.assertNotIn("renewal", summary["contact"]["error"])  # the refusal names paths, not values
        self.assertEqual(job.report["ai"], summary)

    def test_candidates_of_another_tenant_are_never_described(self):
        job, _gw = self.stage(ai_match=False)
        stranger = make_person(self.other, given="Grace", family="Hopper", phones=["+1 202-555-0101"])
        row = self.rows(job)["hs-grace-7001"]
        tampered = {**row.proposed_mapping}
        tampered["candidates"] = [{"id": str(stranger.pk), "rule": "R2", "confidence": 0.85}]
        StagedRow.objects.filter(pk=row.pk).update(proposed_mapping=tampered)
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])
        gw = Gateway(lambda system, payload: None)
        with mock_http(gw):
            ai.run(job)
        for body in gw.bodies:
            self.assertNotIn(str(stranger.pk).encode(), body)
        for payload in gw.payloads():
            for cand in payload.get("candidates") or []:  # Grace's row (R2, same given name) is never sent
                self.assertFalse(cand["rule"] == "R2" and cand["features"]["given_name"] == "same")
        row.refresh_from_db()
        self.assertIsNone(row.proposed_mapping.get("ai"))  # nothing of its own tenant to compare: not sent

    def test_a_batch_of_another_job_is_refused(self):
        job, _gw = self.stage(ai_match=False)
        other_job, _ = self.stage(ai_match=False, co=self.other)
        foreign = ImportJobBatch.objects.get(job=other_job, object_type="contact").batch
        with mock_http(Gateway()), self.assertRaisesMessage(ValueError, "does not belong"):
            ai.propose(job, foreign, "contact")
        # Same company, another job: refused too.
        ImportJob.objects.filter(pk=job.pk).update(status="applied")  # free the source for a second job
        sibling, _ = self.stage(ai_match=False)
        theirs = ImportJobBatch.objects.get(job=sibling, object_type="contact").batch
        with mock_http(Gateway()) as calls, self.assertRaisesMessage(ValueError, "does not belong"):
            ai.dedupe_hints(job, theirs, "contact")
        self.assertEqual(calls, [])


    def test_a_forced_rerun_does_not_show_the_model_its_own_verdict(self):
        job, _gw = self.stage()
        self.assertIn(ai.FLAG_AI_DISAGREES, self.rows(job)["hs-mary-7005"].proposed_mapping["flags"])
        gw = Gateway()
        with mock_http(gw):
            ai.run(job, force=True)
        self.assertEqual(gw.missing, [])  # the same bytes as the first run: recorded
        for payload in gw.payloads():
            self.assertNotIn(ai.FLAG_AI_DISAGREES, payload.get("flags") or [])


class LimitTests(Base):
    def test_a_failing_gateway_stops_the_pass_after_three_failures(self):
        job, _gw = self.stage(ai_match=False)
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])
        with mock_http(Gateway(lambda s, p: json_response(502, {"error": "down"}))) as calls:
            summary = ai.run(job)
        contact_match = summary["contact"]["match"]
        self.assertEqual((contact_match["failed"], contact_match["stopped"]), (3, "gateway_failing"))
        self.assertEqual(summary["contact"]["dedupe"]["skipped"], "gateway_failing")
        self.assertEqual(ImportJobBatch.objects.get(job=job, object_type="contact").batch.llm_calls_used, 3)
        self.assertEqual(len([c for c in calls if b"candidates" in c.content]), 3 + 1)  # + the company's one

    def test_a_deadline_stops_the_pass_before_any_call(self):
        job, _gw = self.stage(ai_match=False)
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])
        batch = ImportJobBatch.objects.get(job=job, object_type="contact").batch
        with mock_http(Gateway()) as calls:
            result = ai.propose(job, batch, "contact", limits=ai.Limits(deadline_s=0))
        self.assertEqual((calls, result.stopped, result.calls), ([], "deadline", 0))


class OutputValidationTests(Base):
    def run_with(self, answer):
        job, _gw = self.stage(ai_match=False)
        before = {k: r.proposed_mapping for k, r in self.rows(job).items()}
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])
        with mock_http(Gateway(lambda system, payload: answer if system == ai.MATCH_SYSTEM else '{"pairs": []}')):
            summary = ai.run(job)
        after = {k: r.proposed_mapping for k, r in self.rows(job).items()}
        return before, after, summary["contact"]["match"]

    def test_malformed_output_leaves_rows_as_they_were(self):
        for answer in ("not json", "[1, 2]", '{"suggested": "c1", "confidence": 7}',
                       '{"suggested": "c1", "confidence": "high"}'):
            with self.subTest(answer=answer):
                before, after, summary = self.run_with(answer)
                self.assertEqual(after, before)
                self.assertEqual(summary["malformed"], 4)
                self.assertEqual(summary["suggested"], 0)

    def test_an_invented_handle_is_dropped(self):
        before, after, summary = self.run_with('{"suggested": "c9", "confidence": 0.9}')
        self.assertEqual(after, before)
        self.assertEqual(summary["dropped"], 4)

    def test_a_gateway_error_leaves_rows_and_is_reported(self):
        job, _gw = self.stage(ai_match=False)
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])
        with mock_http(Gateway(lambda s, p: json_response(503, {"error": "warming"}))):
            summary = ai.run(job)
        self.assertEqual(summary["contact"]["match"]["failed"], 3)  # then the pass stops
        self.assertEqual(summary["contact"]["match"]["stopped"], "gateway_failing")
        self.assertIn("cold_lane", summary["contact"]["match"]["errors"][0])
        self.assertTrue(all(r.proposed_mapping.get("ai") is None for r in self.rows(job).values()))


class BudgetTests(Base):
    def test_budget_stops_calls_at_the_limit_and_the_report_says_so(self):
        with self.settings(GOLDEN_CRM_IMPORT={"ai_budget_per_batch": 2}):
            job, gw = self.stage()
        contact_link = ImportJobBatch.objects.get(job=job, object_type="contact")
        self.assertEqual(contact_link.batch.llm_calls_used, 2)  # never past the budget
        match_calls = [p for p in gw.payloads() if "candidates" in p]
        self.assertEqual(len(match_calls), 2 + 1)  # two contacts + the one company (its own batch)
        report = job.report["ai"]["contact"]
        self.assertTrue(report["budget_exhausted"])
        self.assertEqual((report["match"]["suggested"], report["match"]["budget"]), (2, 2))
        suggested = [r for r in self.rows(job).values() if r.proposed_mapping.get("ai")]
        self.assertEqual(len(suggested), 2)

    def test_two_runs_on_stale_copies_cannot_overspend(self):
        job, _gw = self.stage(ai_match=False)
        batch = ImportJobBatch.objects.get(job=job, object_type="contact").batch
        ImportBatch.objects.filter(pk=batch.pk).update(llm_call_budget=1, llm_calls_used=0)
        first, second = ImportBatch.objects.get(pk=batch.pk), ImportBatch.objects.get(pk=batch.pk)
        takes = [ai._Budget(b, ai.Result("contact")).take() for b in (first, second)]
        self.assertEqual(takes, [True, False])
        self.assertEqual(ImportBatch.objects.get(pk=batch.pk).llm_calls_used, 1)

    def test_a_spent_budget_stays_spent_across_runs(self):
        with self.settings(GOLDEN_CRM_IMPORT={"ai_budget_per_batch": 2}):
            job, _gw = self.stage()
        with mock_http(Gateway()):
            summary = ai.run(job, force=True)
        self.assertTrue(summary["contact"]["budget_exhausted"])
        self.assertEqual(summary["contact"]["match"]["calls"] + summary["contact"]["dedupe"]["calls"], 0)
        contact_link = ImportJobBatch.objects.get(job=job, object_type="contact")
        self.assertEqual(contact_link.batch.llm_calls_used, 2)

    def test_command_exits_non_zero_when_the_budget_runs_out(self):
        with self.settings(GOLDEN_CRM_IMPORT={"ai_budget_per_batch": 1}):
            job, _gw = self.stage(ai_match=False)
        out = io.StringIO()
        with mock_http(Gateway()), self.assertRaisesMessage(CommandError, "budget ran out"):
            call_command("crm_import", "ai", "--job", str(job.pk), "--enable", stdout=out)
        self.assertIn("BUDGET EXHAUSTED", out.getvalue())


class ReviewerTests(Base):
    def test_a_reviewer_can_still_approve_an_ai_disagrees_row(self):
        job, _gw = self.stage()
        mary = self.rows(job)["hs-mary-7005"]
        self.assertIn(ai.FLAG_AI_DISAGREES, mary.proposed_mapping["flags"])
        staging.approve_row(mary, "staff:reviewer")
        mary.refresh_from_db()
        self.assertEqual((mary.mapping_state, mary.mapped_values["decision"]), ("approved", "link"))
        self.assertEqual(mary.mapped_values["target"], mary.proposed_mapping["target"])

    def test_a_staging_run_can_skip_ai_for_a_dry_run(self):
        from crm_import.models import ImportJob as Job

        save_gateway(lane_map={"chat_default": "erp-lane", **LANES})
        export = ExportDir(self, incoming())
        job = Job.objects.create(company=self.co, source="examplecrm:4242", provider="examplecrm",
                                 options={"ai_match": True}, status=Job.Status.VALIDATING)
        with mock_http(Gateway()) as calls:
            staging.stage_job(job, export.path, run_ai=False)
        self.assertEqual(calls, [])
        self.assertNotIn("ai", job.report)


class DryRunTests(Base):
    def test_inside_a_rolled_back_transaction_nothing_is_sent_or_spent(self):
        from django.db import transaction

        job, _gw = self.stage(ai_match=False)
        job.options = {"ai_match": True}
        job.save(update_fields=["options"])
        used = dict(ImportJobBatch.objects.filter(job=job).values_list("object_type", "batch__llm_calls_used"))
        with mock_http(Gateway()) as calls, transaction.atomic():
            summary = ai.run(job)
            transaction.set_rollback(True)
        self.assertEqual(calls, [])
        self.assertFalse(summary["enabled"])
        self.assertEqual(
            dict(ImportJobBatch.objects.filter(job=job).values_list("object_type", "batch__llm_calls_used")), used)


class DedupeHintTests(Base):
    def test_a_same_verdict_becomes_a_hint_and_never_a_merge(self):
        job, _gw = self.stage()
        rows = self.rows(job)
        later = rows["hs-kath-7007"].proposed_mapping
        self.assertIn("hs-kath-7006", later["possible_duplicate_of"])  # name + postcode: no rule hint
        self.assertEqual([d["of"] for d in later["ai_duplicates"]], ["hs-kath-7006"])
        self.assertTrue(later["ai_duplicates"][0]["same"])
        self.assertEqual(later["decision"], "create")
        self.assertEqual(rows["hs-kath-7007"].mapping_state, "approved")
        mary = rows["hs-mary-7005"].proposed_mapping
        self.assertIn("hs-grace-7001", mary["possible_duplicate_of"])  # the rule's hint stays
        verdict = next(d for d in mary["ai_duplicates"] if d["of"] == "hs-grace-7001")
        self.assertFalse(verdict["same"])
        self.assertEqual(job.report["ai"]["contact"]["dedupe"]["hints_added"], 1)
        self.assertEqual(Party.objects.filter(company=self.co).count(), len(GOLDEN_PEOPLE) + len(GOLDEN_ORGS))


class CommandTests(Base):
    def test_stage_flag_opts_the_job_in(self):
        save_gateway(lane_map={"chat_default": "erp-lane", **LANES})
        export = ExportDir(self, incoming())
        out = io.StringIO()
        with mock_http(Gateway()) as calls:
            call_command("crm_import", "stage", "--company", str(self.co.pk), "--dir", str(export.path),
                         "--ai-hints", stdout=out)
        job = ImportJob.objects.get(company=self.co)
        self.assertTrue(job.options["ai_match"])
        self.assertTrue(calls)
        self.assertIn("suggested 4 of 4 (disagree 1)", out.getvalue())

    def test_ai_refuses_a_job_that_did_not_opt_in(self):
        job, _gw = self.stage(ai_match=False)
        with self.assertRaisesMessage(CommandError, "did not opt in"):
            call_command("crm_import", "ai", "--job", str(job.pk), stdout=io.StringIO())

    def test_ai_refuses_without_a_route(self):
        job, _gw = self.stage(ai_match=True, lanes={})
        with self.assertRaisesMessage(CommandError, "AI suggestions are off"):
            call_command("crm_import", "ai", "--job", str(job.pk), stdout=io.StringIO())


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


class RunnerDryRunTests(Base):
    """Step 8's rolled-back dry run never reaches the gateway and never spends."""

    def test_a_dry_run_makes_no_gateway_call_and_leaves_the_budget(self):
        from crm_import import runner

        job, first = self.stage()  # opted in, route configured: staging made its calls
        self.assertTrue(first.bodies)
        used = dict(ImportJobBatch.objects.filter(job=job).values_list("object_type", "batch__llm_calls_used"))
        with mock_http(Gateway()) as calls:
            result = runner.run_job(job, actor="cli:test", dry_run=True)
            call_command("crm_import", "run", "--job", str(job.pk), "--dry-run", "--no-report-file",
                         stdout=io.StringIO())
        self.assertTrue(result.dry_run)
        self.assertEqual(calls, [])
        self.assertEqual(
            dict(ImportJobBatch.objects.filter(job=job).values_list("object_type", "batch__llm_calls_used")), used)

    def test_the_ai_pass_refuses_inside_the_dry_run_transaction(self):
        from unittest import mock

        from crm_import import runner

        job, _gw = self.stage()
        seen = []
        original = runner._run

        def run_and_try_ai(job_, **kw):
            out = original(job_, **kw)
            seen.append(ai.run(job_, force=True))  # what a future hook inside the dry run would do
            return out

        with mock.patch.object(runner, "_run", run_and_try_ai), mock_http(Gateway()) as calls:
            runner.run_job(job, actor="cli:test", dry_run=True)
        self.assertEqual(calls, [])
        self.assertFalse(seen[0]["enabled"])


@override_settings(**SETTINGS)
class ReviewScreenTests(Base):
    """Step 9's queue with step 10's content: the AI column, the chip, the action."""

    def setUp(self):
        super().setUp()
        from core.models import UserProfile
        from core.tests.helpers import make_staff_user

        from .test_review_views import grant

        self.user = make_staff_user("rev", "rev@example.com", "pw-not-used-1")
        UserProfile.objects.update_or_create(user=self.user, defaults={"company": self.co, "display_name": "rev"})
        grant(self.user, "apply_import", "view_import_pii")
        self.client.force_login(self.user)

    def queue(self, job, otype="contact", query=""):
        from django.urls import reverse

        return reverse("erp-crm-import-queue", args=[job.pk, otype]) + (f"?{query}" if query else "")

    def bulk(self, job, otype="contact"):
        from django.urls import reverse

        return reverse("erp-crm-import-bulk", args=[job.pk, otype])

    def test_the_queue_shows_the_suggestion_and_the_ai_disagrees_chip(self):
        job, _gw = self.stage()
        response = self.client.get(self.queue(job))
        self.assertContains(response, "AI disagrees")  # the chip, and the row's flag badge
        self.assertContains(response, "AI suggestion:</strong> Create new")  # Mary: the AI says create
        self.assertContains(response, "AI suggestion:</strong> Grace Hopper (candidate 1)")
        self.assertContains(response, "AI's pick")
        self.assertContains(response, "disagrees with the rule")
        flagged = self.client.get(self.queue(job, query="flag=ai_disagrees"))
        self.assertEqual([i["row"].raw["source_id"] for i in flagged.context["items"]], ["hs-mary-7005"])

    def test_run_ai_suggestions_from_the_queue(self):
        job, _gw = self.stage(ai_match=False)
        ImportJob.objects.filter(pk=job.pk).update(options={"ai_match": True})
        before = {k: proposal_core(r) for k, r in self.rows(job).items()}
        page = self.client.get(self.queue(job))
        self.assertContains(page, 'value="ai_suggest"')
        self.assertEqual(page.context["ai_pending"], 4)
        self.assertContains(page, "Budget: 500 of 500 calls left")
        with mock_http(Gateway()) as calls:
            response = self.client.post(self.bulk(job), {"action": "ai_suggest", "query": ""}, follow=True)
        self.assertEqual(len(calls), 4)  # one per eligible row; the dedupe pass is the CLI's
        self.assertContains(response, "AI suggestions for 4 of 4 rows (1 disagrees with the rule")
        after = {k: proposal_core(r) for k, r in self.rows(job).items()}
        self.assertEqual(after, before)
        self.assertEqual(sum(1 for r in self.rows(job).values() if r.proposed_mapping.get("ai")), 4)
        from core.models import AuditLog

        log = AuditLog.objects.filter(model_name="unify.ImportBatch", changes__bulk="ai_suggest").get()
        self.assertEqual((log.changes["done"], log.changes["calls"]), (4, 4))

    def test_the_action_refuses_a_job_that_did_not_opt_in(self):
        job, _gw = self.stage(ai_match=False)
        page = self.client.get(self.queue(job))
        self.assertNotContains(page, 'value="ai_suggest"')
        self.assertContains(page, "--enable")
        with mock_http(Gateway()) as calls:
            response = self.client.post(self.bulk(job), {"action": "ai_suggest", "query": ""}, follow=True)
        self.assertEqual(calls, [])
        self.assertContains(response, "AI suggestions are off for this job")

    def test_a_route_missing_is_said_and_nothing_is_sent(self):
        job, _gw = self.stage(lanes={})
        page = self.client.get(self.queue(job))
        self.assertContains(page, "AI suggestions are on for this job but cannot run")
        self.assertNotContains(page, 'value="ai_suggest"')

    def test_another_tenants_job_is_not_found(self):
        other_job, _ = self.stage(ai_match=True, co=self.other)
        with mock_http(Gateway()) as calls:
            response = self.client.post(self.bulk(other_job), {"action": "ai_suggest", "query": ""})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(calls, [])

    def test_a_reviewer_without_the_apply_permission_cannot_spend_calls(self):
        from core.models import UserProfile
        from core.tests.helpers import make_staff_user

        job, _gw = self.stage(ai_match=False)
        ImportJob.objects.filter(pk=job.pk).update(options={"ai_match": True})
        viewer = make_staff_user("look", "look@example.com", "pw-not-used-2")
        UserProfile.objects.update_or_create(user=viewer, defaults={"company": self.co, "display_name": "look"})
        self.client.force_login(viewer)
        with mock_http(Gateway()) as calls:
            response = self.client.post(self.bulk(job), {"action": "ai_suggest", "query": ""})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(calls, [])

    def many_graces(self, n):
        """``n`` incoming people linked by phone to Grace (R2 0.85, eligible), and
        ``n`` linked by email with another given name (R1 0.95 + name_mismatch:
        proposed, flagged, above the threshold, never sent)."""
        people = [contact(f"hs-g-{i}", given="Grace", family="Hopper", phones=["+1 202 555 0101"])
                  for i in range(n)]
        people += [contact(f"hs-e-{i}", given="Mary", family="Dijkstra", emails=["ed.b.desk.solo@example.org"])
                   for i in range(n)]
        make_person(self.co, given="Edsger", family="Dijkstra", emails=["ed.b.desk.solo@example.org"])
        job, _gw = self.stage(ai_match=False, records={"contact": people})
        ImportJob.objects.filter(pk=job.pk).update(options={"ai_match": True})
        job.refresh_from_db()
        return job

    def test_one_click_sends_at_most_the_web_limit(self):
        from crm_import import review

        job = self.many_graces(review.AI_WEB_LIMIT + 3)
        with mock_http(Gateway(oracle)) as calls:
            response = self.client.post(self.bulk(job), {"action": "ai_suggest", "query": ""}, follow=True)
        self.assertEqual(len(calls), review.AI_WEB_LIMIT)
        self.assertContains(response, f"AI suggestions for {review.AI_WEB_LIMIT} of {review.AI_WEB_LIMIT} rows")
        job.refresh_from_db()
        self.assertEqual(job.report["ai"]["contact"]["web"]["calls"], review.AI_WEB_LIMIT)

    def test_rows_above_the_threshold_never_starve_the_button(self):
        from crm_import import review

        job = self.many_graces(review.AI_WEB_LIMIT + 3)
        high = [r for r in self.rows(job).values() if r.mapping_confidence >= Decimal("0.95")]
        self.assertTrue(high and all(r.mapping_state == "proposed" for r in high))  # flagged, so not approved
        with mock_http(Gateway(oracle)) as calls:
            self.client.post(self.bulk(job), {"action": "ai_suggest", "query": "order=desc"})
        self.assertEqual(len(calls), review.AI_WEB_LIMIT)
        self.assertTrue(all(r.proposed_mapping.get("ai") is None for r in self.rows(job).values()
                            if r.mapping_confidence >= Decimal("0.95")))

    def test_the_rule_order_and_keys_stay_and_the_ai_pick_is_marked(self):
        job, _gw = self.stage()
        response = self.client.get(self.queue(job))
        edsger = next(i for i in response.context["items"] if i["row"].raw["source_id"] == "hs-edsger-7003")
        stored = [str(c["id"]) for c in edsger["row"].proposed_mapping["candidates"]]
        self.assertEqual([str(c["id"]) for c in edsger["candidates"]], stored)  # the rule's order
        picked = [c for c in edsger["candidates"] if c["ai_pick"]]
        self.assertEqual([str(c["id"]) for c in picked], [str(self.people[2].pk)])
        self.assertIn(f"(candidate {picked[0]['key']})", edsger["ai"]["label"])
        self.assertContains(response, f'name="pick" value="{picked[0]["id"]}"')
