"""CRM Phase 3 step 7: the staff API for pipelines, engagements, tasks,
history, touchpoints, the ``me`` queue and communications
(docs/crm/phase3-engagement.md 5.2).

The auth matrix: staff pass, the storefront identity 403, a customer-only
account 403, a service principal asking for ``me`` 400 ``me_needs_human``.
Two tenants throughout: another tenant's rows are invisible (404 / empty)
and never linkable (400).
"""

from __future__ import annotations

import datetime
import secrets

from django.contrib.auth.models import Permission
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from communications import ingest
from communications.models import Communication, CommunicationRaw
from core.authentication import issue_api_jwt
from core.models import UserProfile
from core.tests.helpers import make_portal_customer, make_staff_user
from crm import engagements, tasks
from crm.models import Engagement, EngagementContact, Pipeline, Stage, Task, TaskEvent, Touchpoint

from .helpers import CrmTestCase, person, profile

API_KEY = "test-" + secrets.token_hex(16)
CRM = "/api/v1/crm/"
COMMS = "/api/v1/communications/communications/"

COLLECTIONS = [
    f"{CRM}pipelines/", f"{CRM}stages/", f"{CRM}engagements/", f"{CRM}engagement-contacts/",
    f"{CRM}stage-changes/", f"{CRM}tasks/", f"{CRM}touchpoints/", f"{CRM}me/", COMMS,
]


def staff_client(company, username, *perms):
    user = make_staff_user(username, password="pw")
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label,
                                                         codename=codename))
    prof = UserProfile.objects.create(user=user, company=company, display_name=username)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, prof


@override_settings(API_KEY=API_KEY)
class _Api(CrmTestCase):
    def setUp(self):
        self.client, self.me = staff_client(self.company, "staffer")
        self.other_client, self.other_me = staff_client(self.other_company, "rival-staffer")
        self.service = APIClient(HTTP_AUTHORIZATION=f"Api-Key {API_KEY}")

    def ok(self, response, code=200):
        self.assertEqual(response.status_code, code, getattr(response, "data", response))
        return response.json() if response.content else None

    def rows(self, response):
        body = self.ok(response)
        return body["results"] if isinstance(body, dict) and "results" in body else body

    def eng(self, **kwargs):
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("party", self.org)
        return engagements.create(**kwargs)


class AuthMatrixTests(_Api):
    def test_staff_read_every_collection(self):
        for url in COLLECTIONS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_storefront_identity_is_403_everywhere(self):
        client = APIClient(HTTP_AUTHORIZATION=f"Bearer {issue_api_jwt(sub='storefront')}")
        eng = self.eng()
        for url in COLLECTIONS + [f"{CRM}engagements/{eng.pk}/", f"{CRM}engagements/{eng.pk}/timeline/"]:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403, url)
                self.assertEqual(client.post(url, {}, format="json").status_code, 403, url)
        self.assertEqual(client.post(f"{CRM}engagements/{eng.pk}/win/", {}, format="json").status_code, 403)

    def test_customer_only_account_is_403_everywhere(self):
        _customer, _sub = make_portal_customer("cust@example.com", company=self.company)
        user = _customer.user
        client = APIClient()
        client.force_authenticate(user=user)
        for url in COLLECTIONS:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403, url)
                self.assertEqual(client.post(url, {}, format="json").status_code, 403, url)

    def test_anonymous_is_401(self):
        for url in COLLECTIONS:
            with self.subTest(url=url):
                self.assertEqual(APIClient().get(url).status_code, 401, url)

    def test_service_me_is_400_never_an_empty_list(self):
        for url in (f"{CRM}me/", f"{CRM}tasks/?assignee=me", f"{CRM}engagements/?owner=me"):
            with self.subTest(url=url):
                response = self.service.get(url)
                self.assertEqual(response.status_code, 400, url)
                self.assertEqual(response.json()["code"], "me_needs_human")

    def test_per_key_service_account_me_is_400_and_outside_its_contract_403(self):
        from core.service_accounts import ensure_service_account, issue_api_key

        svc = ensure_service_account("crm-reader", company=self.company)
        for codename in ("view_task", "view_engagement"):
            svc.user_permissions.add(Permission.objects.get(content_type__app_label="crm",
                                                            codename=codename))
        _key, raw = issue_api_key(svc, name="crm-reader")
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")
        for url in (f"{CRM}me/", f"{CRM}tasks/?assignee=me", f"{CRM}engagements/?owner=me"):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual((response.status_code, response.json().get("code")),
                                 (400, "me_needs_human"), url)
        self.assertEqual(client.get(f"{CRM}tasks/").status_code, 200)
        self.assertEqual(client.get(COMMS).status_code, 403)  # no view_communication
        eng = self.eng()
        self.assertEqual(client.post(f"{CRM}engagements/{eng.pk}/win/", {}, format="json").status_code, 403)

    def test_service_reads_every_tenant(self):
        self.eng()
        engagements.create(company=self.other_company, party=person(self.other_company))
        self.assertEqual(len(self.rows(self.service.get(f"{CRM}engagements/"))), 2)


class PipelineStageTests(_Api):
    def test_staff_read_but_cannot_write_without_manage_pipelines(self):
        rows = self.rows(self.client.get(f"{CRM}pipelines/"))
        self.assertEqual([r["key"] for r in rows], ["sales"])
        stages = self.rows(self.client.get(f"{CRM}stages/?pipeline={self.pipeline.pk}"))
        self.assertEqual(stages[0]["key"], "new")
        self.assertEqual(self.client.post(f"{CRM}pipelines/", {"key": "x", "name": "X"},
                                          format="json").status_code, 403)
        self.assertEqual(self.client.patch(f"{CRM}pipelines/{self.pipeline.pk}/", {"name": "Y"},
                                           format="json").status_code, 403)

    def test_manager_creates_edits_and_cannot_rekey(self):
        client, _ = staff_client(self.company, "mgr", "crm.manage_pipelines")
        body = self.ok(client.post(f"{CRM}pipelines/", {"key": "renewals", "name": "Renewals"},
                                   format="json"), 201)
        self.assertEqual(body["company"], str(self.company.pk))
        url = f"{CRM}pipelines/{body['id']}/"
        self.ok(client.patch(url, {"name": "Renewal deals"}, format="json"))
        response = client.patch(url, {"key": "other"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("key", response.json())
        stage = self.ok(client.post(f"{CRM}stages/", {"pipeline": body["id"], "key": "talk",
                                                      "name": "Talk", "sort_order": 1},
                                    format="json"), 201)
        self.assertEqual(stage["company"], str(self.company.pk))
        dup = client.post(f"{CRM}pipelines/", {"key": "renewals", "name": "Again"}, format="json")
        self.assertEqual(dup.status_code, 400)

    def test_a_new_default_demotes_the_old_one(self):
        client, _ = staff_client(self.company, "mgr", "crm.manage_pipelines")
        self.ok(client.post(f"{CRM}pipelines/", {"key": "b", "name": "B", "is_default": True},
                            format="json"), 201)
        self.pipeline.refresh_from_db()
        self.assertFalse(self.pipeline.is_default)
        self.assertEqual(Pipeline.objects.filter(company=self.company, is_default=True).count(), 1)

    def test_delete_refused_while_referenced(self):
        client, _ = staff_client(self.company, "mgr", "crm.manage_pipelines")
        stage = self.stage("negotiation")
        self.eng(stage=stage)
        response = client.delete(f"{CRM}stages/{stage.pk}/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "referenced")
        self.assertEqual(client.delete(f"{CRM}pipelines/{self.pipeline.pk}/").status_code, 400)
        spare = Stage.objects.create(pipeline=self.pipeline, key="spare", name="Spare", sort_order=99)
        self.assertEqual(client.delete(f"{CRM}stages/{spare.pk}/").status_code, 204)

    def test_another_tenants_pipeline_is_invisible_and_unlinkable(self):
        client, _ = staff_client(self.company, "mgr", "crm.manage_pipelines")
        self.assertEqual(client.get(f"{CRM}pipelines/{self.other_pipeline.pk}/").status_code, 404)
        response = client.post(f"{CRM}stages/", {"pipeline": str(self.other_pipeline.pk), "key": "x",
                                                 "name": "X", "sort_order": 1}, format="json")
        self.assertEqual(response.status_code, 400)


class EngagementTests(_Api):
    def test_create_through_the_service(self):
        body = self.ok(self.client.post(f"{CRM}engagements/", {
            "party": str(self.org.pk), "owner": str(self.me.pk), "expected_value": "1200.00",
            "source": "referral", "note": "from the trade show",
        }, format="json"), 201)
        eng = Engagement.objects.get(pk=body["id"])
        self.assertEqual((eng.company, eng.origin, eng.status, eng.stage.key),
                         (self.company, "api", "open", "new"))
        self.assertTrue(body["number"].startswith("EN-"))
        change = eng.stage_changes.get()
        self.assertEqual((change.source, change.changed_by, change.note), ("api", self.me, "from the trade show"))

    def test_create_refuses_another_tenants_party_and_unknown_source(self):
        rex = person(self.other_company, "Rex", "Rival")
        response = self.client.post(f"{CRM}engagements/", {"party": str(rex.pk)}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("party", response.json())
        response = self.client.post(f"{CRM}engagements/", {"source": "carrier_pigeon"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_service_create_names_the_tenant(self):
        body = self.ok(self.service.post(f"{CRM}engagements/", {
            "company": str(self.other_company.pk), "party": str(person(self.other_company).pk),
        }, format="json"), 201)
        self.assertEqual(body["company"], str(self.other_company.pk))
        response = self.service.post(f"{CRM}engagements/", {"party": str(self.org.pk)}, format="json")
        self.assertEqual(response.status_code, 400)  # two tenants and none named
        cross = self.service.post(f"{CRM}engagements/", {
            "company": str(self.other_company.pk), "party": str(self.org.pk)}, format="json")
        self.assertEqual(cross.status_code, 400)

    def test_patch_refuses_stage_and_status(self):
        eng = self.eng()
        for payload in ({"stage": str(self.stage("negotiation").pk)}, {"status": "won"},
                        {"loss_reason": "price"}):
            with self.subTest(payload=payload):
                response = self.client.patch(f"{CRM}engagements/{eng.pk}/", payload, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "use_action")
        body = self.ok(self.client.patch(f"{CRM}engagements/{eng.pk}/",
                                         {"title": "Bigger deal", "probability": 40}, format="json"))
        self.assertEqual((body["title"], body["effective_probability"]), ("Bigger deal", 40))

    def test_reassigning_someone_elses_engagement_needs_the_permission(self):
        mine = self.eng(owner=self.me)
        theirs = self.eng(owner=self.rep)
        unowned = self.eng()
        url = f"{CRM}engagements/"
        self.ok(self.client.patch(f"{url}{mine.pk}/", {"owner": str(self.rep.pk)}, format="json"))
        self.ok(self.client.patch(f"{url}{unowned.pk}/", {"owner": str(self.me.pk)}, format="json"))
        self.assertEqual(self.client.patch(f"{url}{theirs.pk}/", {"owner": str(self.me.pk)},
                                           format="json").status_code, 403)
        boss, _ = staff_client(self.company, "boss", "crm.reassign_engagement")
        self.ok(boss.patch(f"{url}{theirs.pk}/", {"owner": str(self.me.pk)}, format="json"))

    def test_move_win_lose_reopen_actions(self):
        eng = self.eng()
        url = f"{CRM}engagements/{eng.pk}/"
        body = self.ok(self.client.post(f"{url}move/", {"stage": str(self.stage("negotiation").pk),
                                                        "note": "pricing"}, format="json"))
        self.assertEqual((body["engagement"]["stage_key"], body["needs_close"]), ("negotiation", ""))
        self.assertIsNotNone(body["change"])
        self.assertEqual(self.client.post(f"{url}lose/", {}, format="json").status_code, 400)
        body = self.ok(self.client.post(f"{url}lose/", {"loss_reason": "price", "loss_notes": "dear"},
                                        format="json"))
        self.assertEqual((body["engagement"]["status"], body["engagement"]["stage_key"]),
                         ("lost", "negotiation"))
        self.assertEqual(self.client.post(f"{url}reopen/", {}, format="json").status_code, 403)
        reopener, _ = staff_client(self.company, "boss", "crm.reopen_engagement")
        body = self.ok(reopener.post(f"{url}reopen/", {}, format="json"))
        self.assertEqual(body["engagement"]["status"], "open")
        body = self.ok(self.client.post(f"{url}win/", {"note": "signed"}, format="json"))
        self.assertEqual(body["engagement"]["status"], "won")
        self.assertEqual(self.client.post(f"{url}move/", {"stage": str(self.stage("new").pk)},
                                          format="json").status_code, 400)
        changes = self.rows(self.client.get(f"{CRM}stage-changes/?engagement={eng.pk}"))
        self.assertEqual(len(changes), 5)
        self.assertTrue(all(c["source"] == "api" for c in changes[:4]))

    def test_move_to_an_outcome_stage_returns_needs_close_and_does_not_close(self):
        hinted = Stage.objects.create(pipeline=self.pipeline, key="closed_won", name="Closed won",
                                      sort_order=50, outcome_hint="won")
        eng = self.eng()
        body = self.ok(self.client.post(f"{CRM}engagements/{eng.pk}/move/", {"stage": str(hinted.pk)},
                                        format="json"))
        self.assertEqual((body["needs_close"], body["engagement"]["status"]), ("won", "open"))

    def test_move_to_another_pipeline_changes_it(self):
        other = Pipeline.objects.create(company=self.company, key="renewals", name="Renewals")
        target = Stage.objects.create(pipeline=other, key="open", name="Open", sort_order=1)
        eng = self.eng()
        body = self.ok(self.client.post(f"{CRM}engagements/{eng.pk}/move/", {"stage": str(target.pk)},
                                        format="json"))
        self.assertEqual((body["engagement"]["pipeline_key"], body["engagement"]["stage_key"]),
                         ("renewals", "open"))

    def test_list_filters_and_attention_order(self):
        now = timezone.now()
        idle_old = self.eng(owner=self.me)
        idle_new = self.eng(owner=self.me)
        overdue = self.eng(owner=self.me)
        tasks.create(engagement=overdue, title="late", due=now - datetime.timedelta(days=2))
        upcoming = self.eng(owner=self.rep)
        tasks.create(engagement=upcoming, title="soon", due=now + datetime.timedelta(days=2))
        orphan = engagements.create(company=self.company, party=None)
        stale = self.eng()
        Engagement.objects.filter(pk=stale.pk).update(last_activity_at=now - datetime.timedelta(days=40))
        Engagement.objects.filter(pk=idle_old.pk).update(created_at=now - datetime.timedelta(days=3))
        won = self.eng()
        engagements.win(won)
        engagements.create(company=self.other_company, party=person(self.other_company))

        def ids(query):
            return [r["id"] for r in self.rows(self.client.get(f"{CRM}engagements/{query}"))]

        all_rows = ids("?status=open")
        self.assertEqual(len(all_rows), 6)  # never another tenant's
        self.assertEqual(all_rows[0], str(idle_old.pk))  # no next action, oldest first
        self.assertLess(all_rows.index(str(overdue.pk)), all_rows.index(str(upcoming.pk)))
        self.assertEqual(set(ids("?owner=me")), {str(idle_old.pk), str(idle_new.pk), str(overdue.pk)})
        self.assertEqual(ids(f"?owner={self.rep.pk}"), [str(upcoming.pk)])
        self.assertNotIn(str(won.pk), ids("?needs_next_action=true"))
        self.assertIn(str(idle_new.pk), ids("?needs_next_action=true"))
        self.assertEqual(ids("?overdue=true"), [str(overdue.pk)])
        self.assertEqual(ids("?no_party=true"), [str(orphan.pk)])
        self.assertEqual(ids("?stale=true"), [str(stale.pk)])
        self.assertEqual(ids("?status=won"), [str(won.pk)])
        self.assertEqual(ids(f"?search={overdue.number}"), [str(overdue.pk)])
        self.assertEqual(len(ids("?search=Acme")), 6)  # party name prefix, any status

    def test_another_tenants_engagement_is_404(self):
        theirs = engagements.create(company=self.other_company, party=person(self.other_company))
        self.assertEqual(self.client.get(f"{CRM}engagements/{theirs.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"{CRM}engagements/{theirs.pk}/win/", {},
                                          format="json").status_code, 404)
        mine = self.eng()
        response = self.client.post(f"{CRM}engagements/{mine.pk}/move/",
                                    {"stage": str(self.other_pipeline.stages.first().pk)}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_timeline_merges_sources_and_pages_with_a_cursor(self):
        eng = self.eng()
        engagements.move(eng, self.stage("negotiation"))
        task = tasks.create(engagement=eng, title="Call")
        tasks.complete(task)
        for n in range(3):
            ingest.record(self.company, "note", direction="internal", subject=f"note {n}",
                          occurred_at=timezone.now() - datetime.timedelta(minutes=n), engagement=eng)
        tasks.create(engagement=eng, title="Next week", due=timezone.now() + datetime.timedelta(days=7))
        url = f"{CRM}engagements/{eng.pk}/timeline/"
        body = self.ok(self.client.get(url))
        kinds = [i["kind"] for i in body["items"]]
        self.assertEqual(sorted(set(kinds)), ["communication", "stage", "task"])
        self.assertEqual(len(body["items"]), 6)  # 2 stage rows, 3 notes, 1 done task
        self.assertEqual([u["title"] for u in body["upcoming"]], ["Next week"])
        self.assertIsNone(body["next_before"])
        seen, before = [], None
        while True:
            page = self.ok(self.client.get(url, {"limit": 2, **({"before": before} if before else {})}))
            seen += [i["id"] for i in page["items"]]
            before = page["next_before"]
            if not before:
                break
        self.assertEqual(seen, [i["id"] for i in body["items"]])
        notes = self.ok(self.client.get(url, {"kinds": "note"}))
        self.assertEqual([i["kind"] for i in notes["items"]], ["communication"] * 3)
        self.assertEqual(self.client.get(url, {"before": "garbage"}).status_code, 400)
        impossible = f"2026-13-45T00:00:00+00:00|stage|{eng.pk}"
        self.assertEqual(self.client.get(url, {"before": impossible}).status_code, 400)


class ContactTests(_Api):
    def test_contacts_crud_and_one_primary(self):
        eng = self.eng()
        bob = person(self.company, "Bob", "Builder")
        first = self.ok(self.client.post(f"{CRM}engagement-contacts/", {
            "engagement": str(eng.pk), "party": str(self.ada.pk), "role": "champion", "is_primary": True,
        }, format="json"), 201)
        self.ok(self.client.post(f"{CRM}engagement-contacts/", {
            "engagement": str(eng.pk), "party": str(bob.pk), "role": "billing", "is_primary": True,
        }, format="json"), 201)
        self.assertFalse(EngagementContact.objects.get(pk=first["id"]).is_primary)
        dup = self.client.post(f"{CRM}engagement-contacts/", {"engagement": str(eng.pk),
                                                              "party": str(bob.pk)}, format="json")
        self.assertEqual(dup.status_code, 400)
        org = self.client.post(f"{CRM}engagement-contacts/", {"engagement": str(eng.pk),
                                                              "party": str(self.org.pk)}, format="json")
        self.assertEqual(org.status_code, 400)  # not a person (and the counterparty)
        rex = person(self.other_company, "Rex", "Rival")
        cross = self.client.post(f"{CRM}engagement-contacts/", {"engagement": str(eng.pk),
                                                                "party": str(rex.pk)}, format="json")
        self.assertEqual(cross.status_code, 400)
        rows = self.rows(self.client.get(f"{CRM}engagement-contacts/?engagement={eng.pk}"))
        self.assertEqual(len(rows), 2)
        self.ok(self.client.patch(f"{CRM}engagement-contacts/{first['id']}/", {"role": "technical"},
                                  format="json"))
        self.assertEqual(self.client.delete(f"{CRM}engagement-contacts/{first['id']}/").status_code, 204)
        self.assertEqual(self.rows(self.other_client.get(f"{CRM}engagement-contacts/")), [])


class ReadOnlyHistoryTests(_Api):
    def test_stage_changes_and_touchpoints_are_read_only(self):
        eng = self.eng()
        Touchpoint.objects.create(company=self.company, party=self.ada, engagement=eng,
                                  occurred_at=timezone.now(), channel="referral", source="lead_form")
        self.assertEqual(len(self.rows(self.client.get(f"{CRM}stage-changes/"))), 1)
        self.assertEqual(len(self.rows(self.client.get(f"{CRM}touchpoints/?engagement={eng.pk}"))), 1)
        self.assertEqual(self.rows(self.other_client.get(f"{CRM}touchpoints/")), [])
        self.assertEqual(self.client.post(f"{CRM}stage-changes/", {}, format="json").status_code, 405)
        # Touchpoints take a POST from a caller with crm.add_touchpoint only
        # (Phase 6 step 6, crm/tests/test_touchpoints.py); never PATCH or DELETE.
        self.assertEqual(self.client.post(f"{CRM}touchpoints/", {}, format="json").status_code, 403)
        touch = Touchpoint.objects.get()
        self.assertEqual(self.client.patch(f"{CRM}touchpoints/{touch.pk}/", {}, format="json").status_code, 405)


class TaskTests(_Api):
    def test_create_with_an_all_day_due_and_a_timed_due(self):
        eng = self.eng()
        body = self.ok(self.client.post(f"{CRM}tasks/", {
            "title": "Send pack", "engagement": str(eng.pk), "due": "2026-11-02",
            "due_tz": "Europe/Berlin", "assignee": str(self.me.pk), "kind": "follow_up",
        }, format="json"), 201)
        task = Task.objects.get(pk=body["id"])
        self.assertTrue(task.all_day)
        self.assertEqual(task.due_at.astimezone(datetime.UTC).isoformat(), "2026-11-02T22:59:59.999999+00:00")
        self.assertEqual((task.source, task.created_by, task.party), ("api", self.me, self.org))
        eng.refresh_from_db()
        self.assertEqual(eng.next_task, task)
        body = self.ok(self.client.post(f"{CRM}tasks/", {
            "title": "Call", "due": "2026-11-02T15:00:00+00:00"}, format="json"), 201)
        self.assertFalse(body["all_day"])
        bad = self.client.post(f"{CRM}tasks/", {"title": "x", "kind": "nap"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_patch_refuses_status_and_reschedules_and_reassigns_through_the_service(self):
        task = tasks.create(company=self.company, title="Call", assignee=self.me,
                            due=timezone.now() - datetime.timedelta(hours=1))
        Task.objects.filter(pk=task.pk).update(due_notified_at=timezone.now())
        url = f"{CRM}tasks/{task.pk}/"
        response = self.client.patch(url, {"status": "done"}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "use_action"))
        self.ok(self.client.patch(url, {"due": "2026-12-01T09:00:00Z", "assignee": str(self.rep.pk)},
                                  format="json"))
        task.refresh_from_db()
        self.assertIsNone(task.due_notified_at)
        self.assertEqual(task.assignee, self.rep)
        self.assertEqual(sorted(task.events.values_list("event", flat=True)),
                         ["assigned", "created", "rescheduled"])
        rex = profile(self.other_company, "rex")
        self.assertEqual(self.client.patch(url, {"assignee": str(rex.pk)}, format="json").status_code, 400)

    def test_complete_reopen_cancel_actions_write_task_events(self):
        task = tasks.create(company=self.company, title="Call")
        url = f"{CRM}tasks/{task.pk}/"
        self.assertEqual(self.ok(self.client.post(f"{url}complete/"))["status"], "done")
        self.assertEqual(self.ok(self.client.post(f"{url}reopen/", {"status": "waiting"},
                                                  format="json"))["status"], "waiting")
        self.assertEqual(self.ok(self.client.post(f"{url}cancel/"))["status"], "cancelled")
        self.assertEqual(list(TaskEvent.objects.filter(task=task).order_by("at", "created_at")
                              .values_list("event", flat=True)),
                         ["created", "completed", "reopened", "cancelled"])
        events = TaskEvent.objects.filter(task=task, event="completed").get()
        self.assertEqual(events.by, self.me)

    def test_queues(self):
        now = timezone.now()
        late = tasks.create(company=self.company, title="late", assignee=self.me,
                            due=now - datetime.timedelta(hours=2))
        later_today = tasks.create(company=self.company, title="tonight", assignee=self.me,
                                   due=now + datetime.timedelta(minutes=5))
        next_week = tasks.create(company=self.company, title="week", assignee=self.me,
                                 due=now + datetime.timedelta(days=3))
        unassigned = tasks.create(company=self.company, title="nobody", due=now - datetime.timedelta(days=1))
        done = tasks.create(company=self.company, title="done", assignee=self.me,
                            due=now - datetime.timedelta(days=1))
        tasks.complete(done)
        tasks.create(company=self.other_company, title="theirs", due=now - datetime.timedelta(days=1))

        def ids(query):
            return {r["id"] for r in self.rows(self.client.get(f"{CRM}tasks/{query}"))}

        self.assertEqual(ids("?due=overdue"), {str(late.pk), str(unassigned.pk)})
        self.assertEqual(ids("?overdue=true&assignee=me"), {str(late.pk)})
        self.assertEqual(ids("?assignee=none"), {str(unassigned.pk)})
        self.assertIn(str(later_today.pk), ids("?due=week"))
        self.assertIn(str(next_week.pk), ids("?due=week"))
        self.assertNotIn(str(done.pk), ids("?due=week"))
        self.assertEqual(ids("?status=done"), {str(done.pk)})
        self.assertEqual(len(ids("?status=open")), 4)
        self.assertEqual(self.client.get(f"{CRM}tasks/?due=someday").status_code, 400)
        self.assertEqual(self.client.get(f"{CRM}tasks/?due_before=2026-13-45T00:00:00").status_code, 400)
        self.assertEqual(self.rows(self.client.get(f"{CRM}tasks/?engagement=nope")), [])
        self.assertEqual(self.other_client.get(f"{CRM}tasks/{late.pk}/").status_code, 404)

    def test_me_queue(self):
        now = timezone.now()
        late = tasks.create(company=self.company, title="late", assignee=self.me,
                            due=now - datetime.timedelta(hours=2))
        tasks.create(company=self.company, title="someone else's", assignee=self.rep,
                     due=now - datetime.timedelta(hours=2))
        idle = self.eng(owner=self.me)
        busy = self.eng(owner=self.me)
        tasks.create(engagement=busy, title="next", assignee=self.me, due=now + datetime.timedelta(days=9))
        body = self.ok(self.client.get(f"{CRM}me/"))
        self.assertEqual(body["profile"], str(self.me.pk))
        self.assertEqual([t["id"] for t in body["overdue"]], [str(late.pk)])
        self.assertEqual([e["id"] for e in body["needs_next_action"]], [str(idle.pk)])


class CommunicationApiTests(_Api):
    def test_log_through_ingest_and_filter(self):
        eng = self.eng()
        body = self.ok(self.client.post(COMMS, {
            "kind": "call", "direction": "outbound", "occurred_at": "2026-10-01T10:00:00Z",
            "duration_seconds": 300, "summary": "Talked pricing", "engagement": str(eng.pk),
            "participant_input": [{"role": "callee", "address": "+1 212 555 0100"}],
        }, format="json"), 201)
        comm = Communication.objects.get(pk=body["id"])
        self.assertEqual((comm.provider, comm.performed_by, comm.is_editable, comm.engagement),
                         ("manual", self.me, True, eng))
        self.assertEqual(len(body["participants"]), 1)
        self.assertNotIn("participant_input", body)
        self.assertFalse(body["has_raw"])
        self.assertEqual(len(self.rows(self.client.get(f"{COMMS}?engagement={eng.pk}&kind=call"))), 1)
        self.assertEqual(self.rows(self.client.get(f"{COMMS}?since=2026-10-02T00:00:00Z")), [])
        self.assertEqual(self.client.get(f"{COMMS}?since=2026-13-45T00:00:00").status_code, 400)
        self.assertEqual(self.rows(self.other_client.get(COMMS)), [])
        bad = self.client.post(COMMS, {"kind": "note", "direction": "outbound",
                                       "occurred_at": "2026-10-01T10:00:00Z"}, format="json")
        self.assertEqual(bad.status_code, 400)  # a note is internal-only
        theirs = engagements.create(company=self.other_company, party=person(self.other_company))
        cross = self.client.post(COMMS, {"kind": "note", "direction": "internal",
                                         "occurred_at": "2026-10-01T10:00:00Z",
                                         "engagement": str(theirs.pk)}, format="json")
        self.assertEqual(cross.status_code, 400)

    def test_list_query_count_is_constant(self):
        eng = self.eng()

        def log(n):
            for i in range(n):
                ingest.record(self.company, "email", direction="inbound", occurred_at=timezone.now(),
                              provider="mailer", external_id=f"q-{n}-{i}", known_max_version=1,
                              engagement=eng, participants=[{"role": "from", "address": f"x{i}@ex.com"}],
                              envelope={"schema_version": 1, "provider": "mailer", "payload": {"i": i}})

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        log(2)
        self.ok(self.client.get(COMMS))  # warm per-request caches (content types, perms)
        with CaptureQueriesContext(connection) as small:
            self.ok(self.client.get(COMMS))
        log(6)
        with CaptureQueriesContext(connection) as large:
            self.ok(self.client.get(COMMS))
        self.assertEqual(len(small), len(large))

    def test_patch_only_where_editable(self):
        manual = ingest.record(self.company, "note", direction="internal", occurred_at=timezone.now(),
                               subject="draft").communication
        self.assertEqual(self.ok(self.client.patch(f"{COMMS}{manual.pk}/", {"subject": "final"},
                                                   format="json"))["subject"], "final")
        self.client.patch(f"{COMMS}{manual.pk}/", {"kind": "email"}, format="json")
        manual.refresh_from_db()
        self.assertEqual(manual.kind, "note")  # kind is fixed at logging (read-only on PATCH)
        record = ingest.record(self.company, "email", direction="inbound", occurred_at=timezone.now(),
                               provider="mailer", external_id="m-1", known_max_version=1,
                               envelope={"schema_version": 1, "provider": "mailer",
                                         "payload": {"headers": {"x": "y"}}}).communication
        response = self.client.patch(f"{COMMS}{record.pk}/", {"subject": "edited"}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "not_editable"))

    def test_raw_needs_its_permission(self):
        comm = ingest.record(self.company, "email", direction="inbound", occurred_at=timezone.now(),
                             provider="mailer", external_id="m-2", known_max_version=1,
                             envelope={"schema_version": 1, "provider": "mailer",
                                       "payload": {"body": "secret transcript"}}).communication
        detail = self.ok(self.client.get(f"{COMMS}{comm.pk}/"))
        self.assertTrue(detail["has_raw"])
        self.assertNotIn("secret transcript", str(detail))
        self.assertEqual(self.client.get(f"{COMMS}{comm.pk}/raw/").status_code, 403)
        reader, _ = staff_client(self.company, "auditor", "communications.view_raw_communication")
        raw = self.ok(reader.get(f"{COMMS}{comm.pk}/raw/"))
        self.assertEqual(raw["payload"], {"body": "secret transcript"})
        manual = ingest.record(self.company, "note", direction="internal",
                               occurred_at=timezone.now()).communication
        self.assertEqual(reader.get(f"{COMMS}{manual.pk}/raw/").status_code, 404)
        other_reader, _ = staff_client(self.other_company, "auditor2",
                                       "communications.view_raw_communication")
        self.assertEqual(other_reader.get(f"{COMMS}{comm.pk}/raw/").status_code, 404)
        self.assertTrue(CommunicationRaw.objects.filter(communication=comm).exists())


class OrderEngagementLinkTests(_Api):
    def test_an_order_sold_without_a_quote_can_join_a_thread_of_its_tenant(self):
        from customers.models import Customer
        from orders.models import Order

        customer = Customer.objects.create(first_name="A", last_name="B", email="ab@example.com",
                                           company=self.company)
        order = Order.objects.create(customer=customer, company=self.company)
        eng = self.eng()
        url = f"/api/v1/orders/orders/{order.pk}/"
        self.ok(self.client.patch(url, {"engagement": str(eng.pk)}, format="json"))
        order.refresh_from_db()
        self.assertEqual(order.engagement, eng)
        theirs = engagements.create(company=self.other_company, party=person(self.other_company))
        self.assertEqual(self.client.patch(url, {"engagement": str(theirs.pk)}, format="json").status_code, 400)
