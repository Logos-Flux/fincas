"""CRM Phase 4 step 4: owner-team stamping (docs/crm/phase4-fields.md 2.1).

``owner_team`` on Party and Engagement is stamped from the owner's
``UserProfile.team`` on every path that changes the owner, and is never
recomputed when the owner later moves team. Each owner-change path has a
test here: the ORM, ``crm.engagements.create`` / ``reassign``, the ERP
forms and bulk action, the API (create and PATCH), lead intake, the quote
engagement, and the ``bulk_create`` backfill.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import Team, UserProfile
from core.teams import backfill_owner_teams
from core.tests.helpers import make_staff_user
from crm import engagements
from crm.models import Engagement, Lead
from parties.models import Party

from .helpers import CrmTestCase, organisation, profile

CRM = "/api/v1/crm/"
PARTIES = "/api/v1/parties/"


def team(company, key, name=None):
    return Team.objects.create(company=company, key=key, name=name or key.title())


def staff_api(company, username, *perms):
    user = make_staff_user(username, password="pw")
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label,
                                                         codename=codename))
    prof = UserProfile.objects.create(user=user, company=company, display_name=username)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, prof


class _Teams(CrmTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.west = team(cls.company, "west")
        cls.east = team(cls.company, "east")
        cls.rival_team = team(cls.other_company, "west")
        cls.rep.team = cls.west
        cls.rep.save()
        cls.east_rep = profile(cls.company, "east-rep")
        cls.east_rep.team = cls.east
        cls.east_rep.save()
        cls.no_team_rep = profile(cls.company, "loner")

    def fresh(self, obj):
        return type(obj)._base_manager.get(pk=obj.pk)


class ModelStampTests(_Teams):
    def test_create_stamps_from_the_owner(self):
        eng = self.engagement(owner=self.rep)
        self.assertEqual(self.fresh(eng).owner_team, self.west)
        party = organisation(self.company, "Stamped Co", owner=self.east_rep)
        self.assertEqual(self.fresh(party).owner_team, self.east)

    def test_no_owner_or_no_team_stamps_null(self):
        self.assertIsNone(self.fresh(self.engagement(owner=None)).owner_team)
        self.assertIsNone(self.fresh(self.engagement(owner=self.no_team_rep)).owner_team)

    def test_owner_change_restamps_and_clearing_clears(self):
        party = organisation(self.company, "Moving Co", owner=self.rep)
        party = self.fresh(party)
        party.owner = self.east_rep
        party.save()
        self.assertEqual(self.fresh(party).owner_team, self.east)
        party.owner = None
        party.save(update_fields=["owner"])
        self.assertIsNone(self.fresh(party).owner_team)

    def test_update_fields_save_writes_the_stamp(self):
        eng = self.fresh(self.engagement(owner=self.rep))
        eng.owner = self.east_rep
        eng.save(update_fields=["owner", "updated_at"])
        self.assertEqual(self.fresh(eng).owner_team, self.east)

    def test_an_unrelated_save_leaves_the_team_alone(self):
        eng = self.fresh(self.engagement(owner=self.rep))
        Engagement._base_manager.filter(pk=eng.pk).update(owner_team=self.east)
        eng = self.fresh(eng)
        eng.title = "Renamed"
        eng.save()
        self.assertEqual(self.fresh(eng).owner_team, self.east)

    def test_a_named_team_wins_over_the_stamp(self):
        eng = self.engagement(owner=self.rep, owner_team=self.east)
        self.assertEqual(self.fresh(eng).owner_team, self.east)
        party = self.fresh(organisation(self.company, "Named Co", owner=self.rep))
        party.owner, party.owner_team = self.east_rep, None  # named: no team
        party.save()
        self.assertIsNone(self.fresh(party).owner_team)
        # Naming the team it already has is not a choice: the stamp applies.
        party = self.fresh(party)
        party.owner, party.owner_team = self.rep, None
        party.save()
        self.assertEqual(self.fresh(party).owner_team, self.west)

    def test_a_team_move_leaves_old_records_unchanged(self):
        eng = self.engagement(owner=self.rep)
        party = organisation(self.company, "History Co", owner=self.rep)
        self.rep.team = self.east
        self.rep.save()
        self.assertEqual(self.fresh(eng).owner_team, self.west)
        self.assertEqual(self.fresh(party).owner_team, self.west)
        # A new record for the same owner gets the new team.
        self.assertEqual(self.fresh(self.engagement(owner=self.rep)).owner_team, self.east)
        self.rep.team = self.west
        self.rep.save()

    def test_an_owner_of_another_tenant_never_stamps_its_team(self):
        rival = profile(self.other_company, "rival-rep")
        rival.team = self.rival_team
        rival.save()
        party = organisation(self.company, "Odd Co")
        party.owner = rival
        party.save()  # around clean(): the stamp still refuses the foreign team
        self.assertIsNone(self.fresh(party).owner_team)

    def test_clean_refuses_another_tenants_team(self):
        eng = self.fresh(self.engagement(owner=self.rep))
        eng.owner_team = self.rival_team
        with self.assertRaises(ValidationError) as ctx:
            eng.full_clean(exclude=["number", "title"])
        self.assertIn("owner_team", ctx.exception.message_dict)
        with self.assertRaises(ValidationError):
            engagements.create(company=self.company, owner=self.rep, owner_team=self.rival_team)


class ServiceStampTests(_Teams):
    def test_reassign_stamps_and_can_name_a_team(self):
        eng = self.engagement(owner=self.rep)
        engagements.reassign(eng, self.east_rep)
        self.assertEqual(self.fresh(eng).owner_team, self.east)
        engagements.reassign(eng, self.east_rep, owner_team=self.west)
        self.assertEqual(self.fresh(eng).owner_team, self.west)
        engagements.reassign(eng, None)
        self.assertIsNone(self.fresh(eng).owner_team)

    def test_reassign_refuses_another_tenant(self):
        eng = self.engagement(owner=self.rep)
        with self.assertRaises(ValidationError):
            engagements.reassign(eng, self.rep, owner_team=self.rival_team)
        self.assertEqual(self.fresh(eng).owner_team, self.west)

    def test_lead_intake_stamps_the_assignee_team(self):
        lead = Lead.objects.create(company=self.company, email="ada@example.com", first_name="Ada",
                                   last_name="Lovelace", request_type=Lead.RequestType.QUOTE,
                                   assigned_to=self.east_rep)
        lead.refresh_from_db()
        self.assertEqual(lead.engagement.owner_team, self.east)

    def test_the_quote_engagement_stamps_the_sales_rep_team(self):
        from customers.models import Customer
        from orders.models import Quote

        customer = Customer.objects.create(first_name="Ada", last_name="L", email="ql@example.com",
                                           company=self.company)
        quote = Quote.objects.create(company=self.company, customer=customer, sales_rep=self.rep,
                                     grand_total=Decimal("10.00"))
        self.assertEqual(self.fresh(quote.engagement).owner_team, self.west)

    def test_the_bulk_create_backfill_stamps_its_rows(self):
        import itertools

        from crm.backfill import backfill_quote_engagements
        from customers.models import Customer
        from orders.models import Quote

        customer = Customer.objects.create(first_name="Bo", last_name="B", email="bf@example.com",
                                           company=self.company)
        numbers = itertools.count(1)
        quote = Quote(company=self.company, customer=customer, sales_rep=self.east_rep, status="sent",
                      quote_number=f"QT-T{next(numbers):05d}")
        Quote.objects.bulk_create([quote])  # no save(): no engagement yet
        backfill_quote_engagements(accept_unmapped=True)
        eng = Quote.objects.get(pk=quote.pk).engagement
        self.assertEqual(eng.owner_team, self.east)


class BackfillFunctionTests(_Teams):
    def test_stamps_only_unstamped_rows_and_is_idempotent(self):
        eng = self.engagement(owner=self.rep)
        explicit = self.engagement(owner=self.rep, owner_team=self.east)
        Engagement._base_manager.filter(pk=eng.pk).update(owner_team=None)
        self.assertEqual(backfill_owner_teams(Engagement, UserProfile), 1)
        self.assertEqual(self.fresh(eng).owner_team, self.west)
        self.assertEqual(self.fresh(explicit).owner_team, self.east)
        self.assertEqual(backfill_owner_teams(Engagement, UserProfile), 0)

    def test_a_cross_tenant_owner_is_never_stamped(self):
        rival = profile(self.other_company, "rival-rep2")
        rival.team = self.rival_team
        rival.save()
        party = organisation(self.company, "Odd Co 2")
        Party._base_manager.filter(pk=party.pk).update(owner=rival)
        self.assertEqual(backfill_owner_teams(Party, UserProfile), 0)
        self.assertIsNone(self.fresh(party).owner_team)


class ErpStampTests(_Teams):
    def setUp(self):
        self.user = make_staff_user("sam", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="reassign_engagement"))
        self.me = UserProfile.objects.create(user=self.user, company=self.company, display_name="Sam")
        self.client.force_login(self.user)

    def test_engagement_edit_form_stamps(self):
        eng = self.engagement(owner=None)
        response = self.client.post(reverse("erp-crm-engagement-edit", args=[eng.pk]),
                                    {"party": str(self.org.pk), "owner": self.east_rep.pk, "currency": "USD"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.fresh(eng).owner_team, self.east)

    def test_bulk_assign_stamps(self):
        a, b = self.engagement(owner=None), self.engagement(owner=self.rep)
        self.client.post(reverse("erp-crm-engagement-bulk"),
                         {"ids": [a.pk, b.pk], "action": "assign", "owner": self.east_rep.pk})
        self.assertEqual([self.fresh(a).owner_team, self.fresh(b).owner_team], [self.east, self.east])

    def test_party_forms_stamp(self):
        response = self.client.post(reverse("erp-party-create"), {
            "kind": "organisation", "display_name": "Form Co", "owner": self.rep.pk,
        })
        party = Party.objects.get(display_name="Form Co")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(party.owner_team, self.west)
        self.client.post(reverse("erp-party-edit", args=[party.pk]), {
            "kind": "organisation", "display_name": "Form Co", "owner": self.east_rep.pk,
        })
        self.assertEqual(self.fresh(party).owner_team, self.east)


class ApiStampTests(_Teams):
    def setUp(self):
        self.api, self.me = staff_api(self.company, "staffer")
        self.boss, _ = staff_api(self.company, "boss", "crm.reassign_engagement")
        self.rival_api, _ = staff_api(self.other_company, "rival-staffer")

    def test_engagement_create_and_patch_stamp(self):
        response = self.api.post(f"{CRM}engagements/", {"party": str(self.org.pk), "owner": str(self.me.pk)},
                                 format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.json()["owner_team"])  # the caller has no team
        self.me.team = self.east
        self.me.save()
        eng_id = response.json()["id"]
        response = self.api.patch(f"{CRM}engagements/{eng_id}/", {"owner": str(self.rep.pk)}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["owner_team"], str(self.west.pk))

    def test_naming_the_team_needs_reassign(self):
        eng = self.engagement(owner=self.me)
        url = f"{CRM}engagements/{eng.pk}/"
        self.assertEqual(self.api.patch(url, {"owner_team": str(self.east.pk)}, format="json").status_code, 403)
        response = self.api.post(f"{CRM}engagements/", {"owner_team": str(self.east.pk)}, format="json")
        self.assertEqual(response.status_code, 403)
        response = self.boss.patch(url, {"owner_team": str(self.east.pk)}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.fresh(eng).owner_team, self.east)
        # Re-sending the current value is not an edit.
        self.assertEqual(self.api.patch(url, {"owner_team": str(self.east.pk)}, format="json").status_code, 200)

    def test_another_tenants_team_is_a_400(self):
        eng = self.engagement(owner=self.me)
        response = self.boss.patch(f"{CRM}engagements/{eng.pk}/", {"owner_team": str(self.rival_team.pk)},
                                   format="json")
        self.assertEqual(response.status_code, 400)
        response = self.boss.patch(f"{PARTIES}parties/{self.org.pk}/", {"owner_team": str(self.rival_team.pk)},
                                   format="json")
        self.assertEqual(response.status_code, 400)

    def test_party_create_and_patch_stamp(self):
        response = self.api.post(f"{PARTIES}parties/", {"kind": "organisation", "display_name": "Api Co",
                                                       "owner": str(self.rep.pk)}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["owner_team"], str(self.west.pk))
        party_id = response.json()["id"]
        response = self.api.patch(f"{PARTIES}parties/{party_id}/", {"owner": str(self.east_rep.pk)}, format="json")
        self.assertEqual(response.json()["owner_team"], str(self.east.pk))
        response = self.api.patch(f"{PARTIES}parties/{party_id}/", {"owner_team": str(self.west.pk)}, format="json")
        self.assertEqual(response.status_code, 403)
        response = self.boss.patch(f"{PARTIES}parties/{party_id}/", {"owner_team": None}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["owner_team"])

    def test_owner_team_filters(self):
        stamped = self.engagement(owner=self.rep)
        unstamped = self.engagement(owner=None)
        ids = lambda r: {row["id"] for row in r.json()["results"]}  # noqa: E731
        self.assertEqual(ids(self.api.get(f"{CRM}engagements/", {"owner_team": str(self.west.pk)})),
                         {str(stamped.pk)})
        self.assertIn(str(unstamped.pk), ids(self.api.get(f"{CRM}engagements/", {"owner_team": "none"})))
        self.assertEqual(self.api.get(f"{CRM}engagements/", {"owner_team": "nope"}).json()["results"], [])
        self.assertEqual(self.rival_api.get(f"{CRM}engagements/", {"owner_team": str(self.west.pk)})
                         .json()["results"], [])
        party = organisation(self.company, "Filter Co", owner=self.east_rep)
        rows = ids(self.api.get(f"{PARTIES}parties/", {"owner_team": str(self.east.pk)}))
        self.assertEqual(rows, {str(party.pk)})


class OwnerArchivedTests(_Teams):
    """The "Owner archived" attention rule and the ``archived`` filters (2.5)."""

    def setUp(self):
        from core.staff import archive_profile

        self.leaver = profile(self.company, "leaver")
        self.leaver.team = self.west
        self.leaver.save()
        self.orphaned = self.engagement(owner=self.leaver)
        self.task = __import__("crm.tasks", fromlist=["create"]).create(
            engagement=self.orphaned, title="Call back", kind="follow_up", assignee=self.leaver)
        self.healthy = self.engagement(owner=self.rep)
        archive_profile(self.leaver)

    def test_attention_rule_fires_and_reassign_clears_it(self):
        import datetime

        from django.utils import timezone

        from crm.erp_views import attention_q, attention_rules

        now, grace = timezone.now(), datetime.timedelta(0)
        eng = Engagement.objects.select_related("owner__user").get(pk=self.orphaned.pk)
        self.assertIn("Owner archived", attention_rules(eng, now, grace))
        self.assertTrue(Engagement.objects.filter(attention_q(now, grace), pk=eng.pk).exists())
        self.assertEqual(self.fresh(eng).owner_team, self.west)  # kept: history
        engagements.reassign(eng, self.rep)
        eng = Engagement.objects.select_related("owner__user").get(pk=eng.pk)
        self.assertNotIn("Owner archived", attention_rules(eng, now, grace))

    def test_api_filters(self):
        api, _ = staff_api(self.company, "staffer")
        engs = api.get(f"{CRM}engagements/", {"owner": "archived"}).json()["results"]
        self.assertEqual([row["id"] for row in engs], [str(self.orphaned.pk)])
        tasks = api.get(f"{CRM}tasks/", {"assignee": "archived"}).json()["results"]
        self.assertEqual([row["id"] for row in tasks], [str(self.task.pk)])
        mine = organisation(self.company, "Archived Owner Co", owner=self.leaver)
        rows = api.get(f"{PARTIES}parties/", {"owner_archived": "true"}).json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(mine.pk)])
        rows = api.get(f"{PARTIES}parties/", {"owner_archived": "false"}).json()["results"]
        self.assertNotIn(str(mine.pk), [row["id"] for row in rows])

    def test_the_erp_list_shows_the_rule(self):
        user = make_staff_user("viewer", password="pw")
        UserProfile.objects.create(user=user, company=self.company, display_name="Viewer")
        self.client.force_login(user)
        response = self.client.get(reverse("erp-crm-engagement-list"), {"queue": "attention"})
        self.assertContains(response, "Owner archived")


class ArchivedTeamAndDeferredTests(_Teams):
    def test_an_archived_team_is_never_stamped_or_newly_named(self):
        old = team(self.company, "old")
        rep = profile(self.company, "old-rep")
        rep.team = old
        rep.save()
        Team.objects.filter(pk=old.pk).update(is_active=False)
        rep = UserProfile.objects.get(pk=rep.pk)
        self.assertIsNone(self.fresh(self.engagement(owner=rep)).owner_team)
        boss, _ = staff_api(self.company, "boss2", "crm.reassign_engagement")
        eng = self.engagement(owner=self.rep)
        url = f"{CRM}engagements/{eng.pk}/"
        self.assertEqual(boss.patch(url, {"owner_team": str(old.pk)}, format="json").status_code, 400)
        # A record that already has it keeps it, and may re-send it.
        Engagement._base_manager.filter(pk=eng.pk).update(owner_team=old)
        self.assertEqual(boss.patch(url, {"owner_team": str(old.pk), "title": "Kept"}, format="json").status_code,
                         200)

    def test_a_deferred_owner_team_assigned_with_the_owner_is_kept(self):
        eng = self.engagement(owner=self.rep)
        eng = Engagement.objects.only("id", "company", "owner").get(pk=eng.pk)
        eng.owner, eng.owner_team = self.east_rep, self.west
        eng.save(update_fields=["owner", "owner_team"])
        self.assertEqual(self.fresh(eng).owner_team, self.west)
