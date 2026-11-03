"""CRM Phase 4 step 4: ``core.Team`` and archived owners
(docs/crm/phase4-fields.md 2.1, 2.5).

Teams are tenant data: another tenant's team is invisible and never
linkable, ``key`` is immutable, and editing needs ``core.manage_teams``.
``core.staff.archive_profile`` deactivates a person and never deletes
anything; open engagements they own surface under "Owner archived".
"""

from __future__ import annotations

from io import StringIO

from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import AuditLog, Company, Team, UserProfile
from core.roles import grant_role
from core.staff import archive_profile, archived_q, is_archived
from core.tests.helpers import make_staff_user

TEAMS = "/api/v1/core/teams/"


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


class TeamModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.west = Team.objects.create(company=cls.company, key="west", name="West")

    def test_key_is_unique_per_company_only(self):
        Team.objects.create(company=self.other_company, key="west", name="West")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Team.objects.create(company=self.company, key="west", name="West again")

    def test_key_and_company_are_immutable(self):
        team = Team.objects.get(pk=self.west.pk)
        team.key = "wild-west"
        with self.assertRaises(ValidationError):
            team.save()
        team = Team.objects.get(pk=self.west.pk)
        team.company = self.other_company
        with self.assertRaises(ValidationError):
            team.save()
        team = Team.objects.get(pk=self.west.pk)
        team.name, team.is_active = "West (old)", False
        team.save()
        self.assertFalse(Team.objects.get(pk=team.pk).is_active)

    def test_a_profile_team_must_be_its_company(self):
        rival = Team.objects.create(company=self.other_company, key="rival", name="Rival")
        prof = UserProfile.objects.create(company=self.company, display_name="P")
        prof.team = rival
        with self.assertRaises(ValidationError):
            prof.full_clean()
        with self.assertRaises(ValidationError):
            prof.save()
        prof.team = self.west
        prof.save()

    def test_check_tenancy_reports_a_team_written_around_save(self):
        from crm.models import Engagement, Pipeline  # noqa: F401  (seeded pipelines)
        from parties.models import Party

        rival = Team.objects.create(company=self.other_company, key="rival", name="Rival")
        party = Party.objects.create(company=self.company, kind="organisation", display_name="Acme",
                                     origin="erp")
        Party._base_manager.filter(pk=party.pk).update(owner_team=rival)
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("check_tenancy", "--app", "parties", stdout=out)
        self.assertIn("owner_team.company", out.getvalue())

    def test_a_team_in_use_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        from parties.models import Party

        Party.objects.create(company=self.company, kind="organisation", display_name="Acme",
                             origin="erp", owner_team=self.west)
        with self.assertRaises(ProtectedError):
            self.west.delete()


class TeamApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.west = Team.objects.create(company=cls.company, key="west", name="West")
        cls.rival = Team.objects.create(company=cls.other_company, key="west", name="Rival West")

    def setUp(self):
        self.api, _ = staff_api(self.company, "staffer")
        self.admin, _ = staff_api(self.company, "admin", "core.manage_teams")

    def test_staff_read_their_tenants_teams_only(self):
        rows = self.api.get(TEAMS).json()["results"]
        self.assertEqual([r["id"] for r in rows], [str(self.west.pk)])
        self.assertEqual(self.api.get(f"{TEAMS}{self.rival.pk}/").status_code, 404)

    def test_writes_need_manage_teams(self):
        self.assertEqual(self.api.post(TEAMS, {"key": "east", "name": "East"}, format="json").status_code, 403)
        self.assertEqual(self.api.patch(f"{TEAMS}{self.west.pk}/", {"name": "W"}, format="json").status_code, 403)
        response = self.admin.post(TEAMS, {"key": "east", "name": "East"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["company"], str(self.company.pk))

    def test_key_rules(self):
        response = self.admin.post(TEAMS, {"key": "west", "name": "Dupe"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("key", response.json())
        response = self.admin.patch(f"{TEAMS}{self.west.pk}/", {"key": "renamed"}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.admin.patch(f"{TEAMS}{self.west.pk}/", {"name": "West Coast", "is_active": False},
                                    format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Team.objects.get(pk=self.west.pk).key, "west")

    def test_another_tenant_cannot_be_named(self):
        response = self.admin.post(TEAMS, {"key": "x", "name": "X", "company": str(self.other_company.pk)},
                                   format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Team.objects.filter(key="x").exists())

    def test_no_delete(self):
        self.assertEqual(self.admin.delete(f"{TEAMS}{self.west.pk}/").status_code, 405)


class ArchiveProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.team = Team.objects.create(company=cls.company, key="west", name="West")
        cls.boss_user = make_staff_user("boss", password="pw")
        cls.boss = UserProfile.objects.create(user=cls.boss_user, company=cls.company, display_name="Boss")

    def leaver(self, **profile_fields):
        user = make_staff_user("leaver", "leaver@example.com", "pw", first_name="Lee", last_name="Ver")
        grant_role(user, "customer")
        group = Group.objects.create(name="not-a-role")
        user.groups.add(group)
        profile_fields.setdefault("display_name", "")
        return UserProfile.objects.create(user=user, company=self.company, team=self.team, **profile_fields)

    def test_archive_deactivates_freezes_and_audits_but_never_deletes(self):
        from crm import engagements
        from crm.models import Engagement, Pipeline

        prof = self.leaver()
        Pipeline.objects.get(company=self.company, key="sales")
        eng = engagements.create(company=self.company, owner=prof)
        before = {m: m._base_manager.count() for m in (User, UserProfile, Engagement, Team)}
        archive_profile(prof, by=self.boss)
        after = {m: m._base_manager.count() for m in (User, UserProfile, Engagement, Team)}
        self.assertEqual(before, after)
        prof = UserProfile.objects.select_related("user").get(pk=prof.pk)
        self.assertEqual(prof.display_name, "Lee Ver")
        self.assertEqual(prof.team, self.team)
        self.assertFalse(prof.user.is_active)
        self.assertFalse(prof.user.has_usable_password())
        self.assertFalse(prof.user.is_staff)
        self.assertEqual(sorted(prof.user.groups.values_list("name", flat=True)), ["not-a-role"])
        self.assertEqual(prof.user.email, "leaver@example.com")  # still resolvable by email
        self.assertTrue(is_archived(prof))
        self.assertEqual(Engagement.objects.get(pk=eng.pk).owner, prof)
        log = AuditLog.objects.get(action="archive", object_id=prof.pk)
        self.assertEqual(log.user, self.boss)
        self.assertEqual(log.changes["roles_removed"], ["customer", "staff"])
        # Idempotent: no second audit row.
        archive_profile(prof, by=self.boss)
        self.assertEqual(AuditLog.objects.filter(action="archive", object_id=prof.pk).count(), 1)

    def test_a_set_display_name_is_kept(self):
        prof = self.leaver(display_name="Lee (Sales)")
        archive_profile(prof)
        self.assertEqual(UserProfile.objects.get(pk=prof.pk).display_name, "Lee (Sales)")
        self.assertIsNone(AuditLog.objects.get(action="archive", object_id=prof.pk).user)

    def test_username_when_no_full_name(self):
        user = make_staff_user("solo", password="pw")
        prof = UserProfile.objects.create(user=user, company=self.company)
        archive_profile(prof)
        self.assertEqual(UserProfile.objects.get(pk=prof.pk).display_name, "solo")

    def test_refusals(self):
        with self.assertRaises(ValidationError):
            archive_profile(self.boss, by=self.boss)
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        with self.assertRaises(ValidationError):
            archive_profile(UserProfile.objects.create(user=root, company=self.company))
        svc = User.objects.create_user("svc-sync")
        with self.assertRaises(ValidationError):
            archive_profile(UserProfile.objects.create(user=svc, company=self.company))
        self.assertTrue(User.objects.get(pk=svc.pk).is_active)

    def test_archived_q(self):
        prof = self.leaver()
        self.assertFalse(UserProfile.objects.filter(archived_q(), pk=prof.pk).exists())
        archive_profile(prof)
        self.assertTrue(UserProfile.objects.filter(archived_q(), pk=prof.pk).exists())
        self.assertFalse(UserProfile.objects.filter(archived_q(), pk=self.boss.pk).exists())


class ReviewFollowUpTests(TestCase):
    """Edges found in review: a deactivated-but-not-archived user, an actor of
    another tenant, who may set a profile's team, archived teams."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.west = Team.objects.create(company=cls.company, key="west", name="West")
        cls.old = Team.objects.create(company=cls.company, key="old", name="Old", is_active=False)
        cls.rival = Team.objects.create(company=cls.other_company, key="rival", name="Rival")

    def test_a_merely_deactivated_user_is_archived_properly(self):
        user = make_staff_user("quiet", password="pw")
        prof = UserProfile.objects.create(user=user, company=self.company, display_name="Quiet")
        User.objects.filter(pk=user.pk).update(is_active=False)  # switched off in the admin
        archive_profile(prof)
        user.refresh_from_db()
        self.assertFalse(user.groups.filter(name="staff").exists())
        self.assertFalse(user.has_usable_password())
        self.assertTrue(AuditLog.objects.filter(action="archive", object_id=prof.pk).exists())

    def test_an_actor_of_another_tenant_is_refused(self):
        user = make_staff_user("target", password="pw")
        prof = UserProfile.objects.create(user=user, company=self.company, display_name="T")
        rival = UserProfile.objects.create(company=self.other_company, display_name="R")
        with self.assertRaises(ValidationError):
            archive_profile(prof, by=rival)
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

    def test_setting_a_profile_team_through_users_needs_manage_teams(self):
        target = UserProfile.objects.create(company=self.company, display_name="Target")
        url = f"/api/v1/core/users/{target.pk}/"
        plain, _ = staff_api(self.company, "plain")
        self.assertEqual(plain.patch(url, {"team": str(self.west.pk)}, format="json").status_code, 400)
        boss, _ = staff_api(self.company, "boss", "core.manage_teams")
        self.assertEqual(boss.patch(url, {"team": str(self.old.pk)}, format="json").status_code, 400)
        self.assertEqual(boss.patch(url, {"team": str(self.rival.pk)}, format="json").status_code, 400)
        response = boss.patch(url, {"team": str(self.west.pk)}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(UserProfile.objects.get(pk=target.pk).team, self.west)
        rival_boss, _ = staff_api(self.other_company, "rival-boss", "core.manage_teams")
        east = Team.objects.create(company=self.company, key="east", name="East")
        self.assertEqual(rival_boss.patch(url, {"team": str(east.pk)}, format="json").status_code, 400)

    def test_team_writes_are_refused_before_validation(self):
        plain, _ = staff_api(self.company, "plain2")
        self.assertEqual(plain.post(TEAMS, {"key": "west", "name": "Dupe"}, format="json").status_code, 403)

    def test_a_superuser_names_the_company_and_duplicates_are_a_400(self):
        root = User.objects.create_superuser("root2", "r@example.com", "pw")
        api = APIClient()
        api.force_authenticate(user=root)
        response = api.post(TEAMS, {"key": "west", "name": "Dupe", "company": str(self.company.pk)}, format="json")
        self.assertEqual(response.status_code, 400)
        response = api.post(TEAMS, {"key": "north", "name": "North"}, format="json")
        self.assertEqual(response.status_code, 400)  # two tenants: name one
        response = api.post(TEAMS, {"key": "north", "name": "North", "company": str(self.company.pk)},
                            format="json")
        self.assertEqual(response.status_code, 201, response.content)
