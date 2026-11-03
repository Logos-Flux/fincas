"""Roles registry + derived is_staff (identity-unification step 2, D3)."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth.models import Group, User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core import role_registry
from core.role_registry import (
    CUSTOMER_ROLE,
    ROLES,
    STAFF_ROLE,
    STOREFRONT_ROLE,
    build_roles,
    staff_role_names,
)
from core.roles import (
    adopt_existing_staff,
    ensure_roles,
    grant_role,
    has_staff_role,
    sync_user_flags,
)
from core.tests.helpers import make_staff_user


def _legacy_staff(username):
    """A pre-roles staff account: is_staff in the DB, no role, as left by an
    install that predates GOLDEN_ROLES. Written around the ORM's save so the
    adopt-on-create receiver does not turn it into a whole staff account."""
    user = User.objects.create_user(username, password="pw")
    User.objects.filter(pk=user.pk).update(is_staff=True)
    return User.objects.get(pk=user.pk)


MANAGER = {"name": "manager", "label": "Manager", "staff": True,
           "permissions": ["orders.change_quote"]}
VIP = {"name": "vip-customer", "label": "VIP customer", "portal": True}


class RegistryConfigTests(TestCase):
    def _build(self, roles):
        with self.settings(GOLDEN_ROLES=roles):
            return build_roles()

    def test_builtins_always_present(self):
        roles = self._build([])
        self.assertEqual(list(roles)[:3], [STAFF_ROLE, CUSTOMER_ROLE, STOREFRONT_ROLE])
        self.assertTrue(roles[STAFF_ROLE].staff)
        self.assertTrue(roles[CUSTOMER_ROLE].portal)
        self.assertFalse(roles[CUSTOMER_ROLE].staff)
        self.assertEqual(roles[STOREFRONT_ROLE].kind, "service")

    def test_storefront_role_matches_storefront_identity(self):
        from core.storefront import STOREFRONT_GROUP, STOREFRONT_PERMISSIONS

        self.assertEqual(STOREFRONT_ROLE, STOREFRONT_GROUP)
        self.assertEqual(
            ROLES[STOREFRONT_ROLE].permissions,
            tuple(f"{a}.{c}" for a, c in STOREFRONT_PERMISSIONS),
        )

    def test_overlay_appends(self):
        roles = self._build([MANAGER, VIP])
        self.assertEqual(list(roles)[3:], ["manager", "vip-customer"])
        self.assertEqual(roles["manager"].permissions, ("orders.change_quote",))

    def test_builtins_are_immutable(self):
        for name in (STAFF_ROLE, CUSTOMER_ROLE, STOREFRONT_ROLE):
            with self.subTest(name=name), self.assertRaisesRegex(
                ImproperlyConfigured, "built-in"
            ):
                self._build([{"name": name, "label": "Hijack", "staff": False}])

    def test_bad_entries_raise(self):
        cases = {
            "not a list": "staff",
            "not a dict": ["manager"],
            "unknown key": [{"name": "m", "label": "M", "colour": "red"}],
            "bad slug": [{"name": "Manager!", "label": "M"}],
            "empty label": [{"name": "m", "label": " "}],
            "bad kind": [{"name": "m", "label": "M", "kind": "robot"}],
            "service staff": [{"name": "m", "label": "M", "kind": "service", "staff": True}],
            "service portal": [{"name": "m", "label": "M", "kind": "service", "portal": True}],
            "flag not bool": [{"name": "m", "label": "M", "staff": "yes"}],
            "perms a string": [{"name": "m", "label": "M", "permissions": "orders.view_order"}],
            "perm malformed": [{"name": "m", "label": "M", "permissions": ["view_order"]}],
            "perm duplicated": [{"name": "m", "label": "M",
                                 "permissions": ["orders.view_order", "orders.view_order"]}],
            "duplicate name": [MANAGER, MANAGER],
            "name with trailing newline": [{"name": "manager\n", "label": "M"}],
        }
        for label, config in cases.items():
            with self.subTest(case=label), self.assertRaises(ImproperlyConfigured):
                self._build(config)

    def test_registry_rebuilds_on_setting_change(self):
        with override_settings(GOLDEN_ROLES=[MANAGER]):
            self.assertIn("manager", role_registry.ROLES)
            self.assertEqual(set(staff_role_names()), {STAFF_ROLE, "manager"})
        self.assertNotIn("manager", role_registry.ROLES)


class EnsureRolesTests(TestCase):
    def test_post_migrate_created_the_builtin_groups(self):
        for name in (STAFF_ROLE, CUSTOMER_ROLE, STOREFRONT_ROLE):
            self.assertTrue(Group.objects.filter(name=name).exists(), name)

    @override_settings(GOLDEN_ROLES=[MANAGER])
    def test_creates_groups_and_attaches_permissions_idempotently(self):
        report = ensure_roles()
        self.assertEqual(report["created"], ["manager"])
        group = Group.objects.get(name="manager")
        self.assertEqual(
            list(group.permissions.values_list("codename", flat=True)), ["change_quote"]
        )
        self.assertEqual(ensure_roles()["created"], [])
        self.assertEqual(group.permissions.count(), 1)

    def test_hand_added_permissions_survive(self):
        group = Group.objects.get(name=STAFF_ROLE)
        from django.contrib.auth.models import Permission

        extra = Permission.objects.get(codename="view_order")
        group.permissions.add(extra)
        ensure_roles()
        self.assertIn(extra, group.permissions.all())

    @override_settings(GOLDEN_ROLES=[{"name": "ghost", "label": "G",
                                      "permissions": ["orders.fly_order"]}])
    def test_missing_permission_is_reported_not_fatal(self):
        report = ensure_roles()
        self.assertEqual(report["missing_permissions"], {"ghost": ["orders.fly_order"]})

    def test_first_creation_of_staff_group_adopts_existing_staff(self):
        Group.objects.filter(name=STAFF_ROLE).delete()
        legacy = _legacy_staff("legacy")
        boss = User.objects.create_superuser("boss", "b@x.com", "pw")
        shopper = User.objects.create_user("shopper", password="pw")
        report = ensure_roles()
        self.assertEqual(report["adopted"], ["legacy"])
        legacy.refresh_from_db()
        self.assertTrue(legacy.is_staff)
        self.assertTrue(has_staff_role(legacy))
        self.assertFalse(has_staff_role(boss))  # superusers are not adopted
        self.assertFalse(has_staff_role(shopper))

    def test_existing_staff_group_does_not_auto_adopt(self):
        _legacy_staff("legacy2")
        self.assertEqual(ensure_roles()["adopted"], [])


class DerivationTests(TestCase):
    def setUp(self):
        self.staff_group = Group.objects.get(name=STAFF_ROLE)
        self.other = Group.objects.create(name="warehouse")

    def _fresh(self, user):
        return User.objects.get(pk=user.pk)

    def test_add_and_remove_via_user_side(self):
        user = User.objects.create_user("u1", password="pw")
        user.groups.add(self.staff_group)
        self.assertTrue(user.is_staff)  # in-memory object kept truthful
        self.assertTrue(self._fresh(user).is_staff)
        user.groups.remove(self.staff_group)
        self.assertFalse(user.is_staff)
        self.assertFalse(self._fresh(user).is_staff)

    def test_add_remove_clear_via_group_side(self):
        a = User.objects.create_user("a", password="pw")
        b = User.objects.create_user("b", password="pw")
        self.staff_group.user_set.add(a, b)
        self.assertTrue(self._fresh(a).is_staff and self._fresh(b).is_staff)
        self.staff_group.user_set.remove(a)
        self.assertFalse(self._fresh(a).is_staff)
        self.assertTrue(self._fresh(b).is_staff)
        self.staff_group.user_set.clear()
        self.assertFalse(self._fresh(b).is_staff)

    def test_set_and_clear_via_user_side(self):
        user = User.objects.create_user("u2", password="pw")
        user.groups.set([self.other, self.staff_group])
        self.assertTrue(self._fresh(user).is_staff)
        user.groups.set([self.other])
        self.assertFalse(self._fresh(user).is_staff)
        user.groups.add(self.staff_group)
        user.groups.clear()
        self.assertFalse(self._fresh(user).is_staff)

    def test_non_role_group_does_not_grant_staff(self):
        user = User.objects.create_user("u3", password="pw")
        user.groups.add(self.other)
        self.assertFalse(self._fresh(user).is_staff)

    @override_settings(GOLDEN_ROLES=[MANAGER, VIP])
    def test_deployment_staff_role_grants_and_portal_role_does_not(self):
        ensure_roles()
        manager = User.objects.create_user("m", password="pw")
        grant_role(manager, "manager")
        vip = User.objects.create_user("v", password="pw")
        grant_role(vip, "vip-customer")
        self.assertTrue(self._fresh(manager).is_staff)
        self.assertFalse(self._fresh(vip).is_staff)

    def test_customer_role_is_not_staff(self):
        user = User.objects.create_user("c", password="pw")
        grant_role(user, CUSTOMER_ROLE)
        self.assertFalse(self._fresh(user).is_staff)

    def test_hand_set_staff_flag_is_demoted_on_next_group_change(self):
        user = _legacy_staff("legacy")
        user.groups.add(self.other)
        self.assertFalse(self._fresh(user).is_staff)

    def test_superuser_is_unaffected(self):
        boss = User.objects.create_superuser("boss", "b@x.com", "pw")
        boss.groups.add(self.other)
        boss.groups.clear()
        self.assertTrue(self._fresh(boss).is_staff)
        report = sync_user_flags()
        self.assertNotIn("boss", report["demoted"])
        self.assertTrue(self._fresh(boss).is_staff)

    def test_grant_role_refuses_unknown(self):
        user = User.objects.create_user("u4", password="pw")
        with self.assertRaises(ValueError):
            grant_role(user, "warehouse")

    def test_adopt_existing_staff(self):
        legacy = _legacy_staff("legacy3")
        already = make_staff_user("already", password="pw")
        self.assertEqual(adopt_existing_staff(), ["legacy3"])
        self.assertTrue(has_staff_role(legacy))
        self.assertTrue(self._fresh(already).is_staff)


class SyncRolesCommandTests(TestCase):
    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command("sync_roles", *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def test_adopt_existing_then_derivation_keeps_staff(self):
        legacy = _legacy_staff("legacy")
        output = self._run("--adopt-existing")
        self.assertIn("legacy", output)
        legacy.refresh_from_db()
        self.assertTrue(legacy.is_staff)
        self.assertTrue(has_staff_role(legacy))

    def test_without_adopt_demotes_unroled_staff(self):
        legacy = _legacy_staff("legacy")
        User.objects.create_superuser("boss", "b@x.com", "pw")
        output = self._run()
        self.assertIn("is_staff cleared: ['legacy']", output)
        legacy.refresh_from_db()
        self.assertFalse(legacy.is_staff)
        self.assertTrue(User.objects.get(username="boss").is_staff)

    def test_unflagged_role_member_refuses_then_accept_promotes(self):
        user = make_staff_user("drifted", password="pw")
        User.objects.filter(pk=user.pk).update(is_staff=False)  # out-of-band edit
        from django.core.management.base import CommandError

        with self.assertRaisesRegex(CommandError, "GOLDEN_ROLES_ADOPT_PREEXISTING"):
            self._run()
        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertIn("would refuse", self._run("--dry-run"))
        self._run("--accept-preexisting")
        user.refresh_from_db()
        self.assertTrue(user.is_staff)

    def test_dry_run_writes_nothing(self):
        legacy = _legacy_staff("legacy")
        output = self._run("--dry-run")
        self.assertIn("would demote", output)
        legacy.refresh_from_db()
        self.assertTrue(legacy.is_staff)
        self.assertFalse(has_staff_role(legacy))


class SystemCheckTests(TestCase):
    def _ids(self):
        from core.checks import roles_check

        return [w.id for w in roles_check(None, databases=["default"])]

    def test_clean_database_has_no_warnings(self):
        make_staff_user("ok", password="pw")
        User.objects.create_superuser("boss", "b@x.com", "pw")
        self.assertEqual(self._ids(), [])

    def test_warns_on_staff_flag_without_role(self):
        _legacy_staff("legacy")
        self.assertIn("core.W101", self._ids())

    def test_no_w101_before_the_staff_group_exists(self):
        Group.objects.filter(name=STAFF_ROLE).delete()
        _legacy_staff("legacy")
        ids = self._ids()
        self.assertNotIn("core.W101", ids)
        self.assertIn("core.W103", ids)

    def test_warns_on_role_without_flag(self):
        user = make_staff_user("drifted", password="pw")
        User.objects.filter(pk=user.pk).update(is_staff=False)
        self.assertIn("core.W102", self._ids())

    @override_settings(GOLDEN_ROLES=[MANAGER])
    def test_warns_on_missing_group(self):
        self.assertIn("core.W103", self._ids())

    @override_settings(GOLDEN_ROLES=[{"name": "ghost", "label": "G",
                                      "permissions": ["orders.fly_order"]}])
    def test_warns_on_unknown_permission(self):
        ensure_roles()
        self.assertIn("core.W104", self._ids())

    def test_skipped_without_database(self):
        from core.checks import roles_check

        _legacy_staff("legacy")
        self.assertEqual(roles_check(None), [])


class PrincipalPermissionRoleTests(TestCase):
    """Step 2: the staff API gate is role membership, not the raw flag."""

    URL = "/api/v1/purchasing/suppliers/"

    def _get(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(self.URL).status_code

    def test_staff_role_passes(self):
        self.assertNotEqual(self._get(make_staff_user("clerk", password="pw")), 403)

    @override_settings(GOLDEN_ROLES=[MANAGER])
    def test_deployment_staff_role_passes(self):
        ensure_roles()
        self.assertNotEqual(self._get(make_staff_user("mgr", password="pw", role="manager")), 403)

    def test_hand_set_flag_without_role_is_403(self):
        legacy = _legacy_staff("legacy")
        self.assertEqual(self._get(legacy), 403)

    def test_customer_role_is_403(self):
        user = User.objects.create_user("buyer", password="pw")
        grant_role(user, CUSTOMER_ROLE)
        self.assertEqual(self._get(user), 403)

    def test_superuser_without_role_passes(self):
        boss = User.objects.create_superuser("boss", "b@x.com", "pw")
        self.assertNotEqual(self._get(boss), 403)


class AdminUserFormTests(TestCase):
    def test_is_staff_is_read_only_in_admin(self):
        from django.contrib import admin

        model_admin = admin.site._registry[User]
        self.assertIn("is_staff", model_admin.get_readonly_fields(None))
        self.assertNotIn("is_superuser", model_admin.get_readonly_fields(None))

    def test_admin_group_edit_derives_is_staff(self):
        boss = User.objects.create_superuser("boss", "b@x.com", "pw")
        target = User.objects.create_user("target", password="pw")
        self.client.force_login(boss)
        staff_group = Group.objects.get(name=STAFF_ROLE)
        response = self.client.post(f"/admin/auth/user/{target.pk}/change/", {
            "username": "target", "password": target.password,
            "is_active": "on", "groups": [staff_group.pk],
            "last_login_0": "", "last_login_1": "",
            "date_joined_0": "2026-01-01", "date_joined_1": "00:00:00",
        })
        self.assertIn(response.status_code, (200, 302))
        target.refresh_from_db()
        self.assertTrue(target.is_staff, response.content[:500] if response.status_code == 200 else "")


class DerivationEdgeTests(TestCase):
    """Paths m2m_changed cannot see (code review of identity step 2)."""

    def setUp(self):
        self.staff_group = Group.objects.get(name=STAFF_ROLE)

    def _fresh(self, user):
        return User.objects.get(pk=user.pk)

    def test_deleting_a_role_group_demotes_and_migrate_does_not_readopt(self):
        member = make_staff_user("member", password="pw")
        Group.objects.filter(name=STAFF_ROLE).delete()  # queryset path
        self.assertFalse(self._fresh(member).is_staff)
        report = ensure_roles()  # what the next migrate does
        self.assertEqual(report["created"], [STAFF_ROLE])
        self.assertEqual(report["adopted"], [])
        self.assertFalse(has_staff_role(member))

    def test_instance_delete_also_demotes(self):
        member = make_staff_user("member2", password="pw")
        self.staff_group.delete()
        self.assertFalse(self._fresh(member).is_staff)

    def test_removing_superuser_rederives_is_staff(self):
        boss = User.objects.create_superuser("boss", "b@x.com", "pw")
        boss.is_superuser = False
        boss.save()
        self.assertFalse(boss.is_staff)
        self.assertFalse(self._fresh(boss).is_staff)

    def test_stale_copy_save_cannot_restore_staff(self):
        user = make_staff_user("stale", password="pw")
        stale = User.objects.get(pk=user.pk)  # loaded while still staff
        user.groups.remove(self.staff_group)
        stale.first_name = "Edited"
        stale.save()
        self.assertFalse(self._fresh(user).is_staff)

    def test_group_side_add_then_stale_save_keeps_staff(self):
        user = User.objects.create_user("gs", password="pw")
        self.staff_group.user_set.add(user)  # `user` in memory still says False
        user.first_name = "Edited"
        user.save()
        self.assertTrue(self._fresh(user).is_staff)

    def test_turning_the_flag_on_adopts_into_staff(self):
        user = User.objects.create_user("promoted", password="pw")
        user.is_staff = True
        user.save()
        self.assertTrue(self._fresh(user).is_staff)
        self.assertTrue(has_staff_role(user))

    def test_turning_the_flag_off_revokes_staff_roles(self):
        user = make_staff_user("leaver", password="pw")
        user.is_staff = False
        user.save()
        self.assertFalse(self._fresh(user).is_staff)
        self.assertFalse(has_staff_role(user))

    def test_created_with_flag_is_whole_staff(self):
        """create_user(is_staff=True) (17 golden files, downstream commands)."""
        user = User.objects.create_user("hire", password="pw", is_staff=True)
        self.assertTrue(has_staff_role(user))
        user.set_password("new-pw")
        user.save()  # a later full save must not demote
        self.assertTrue(self._fresh(user).is_staff)
        client = APIClient()
        client.force_authenticate(user=self._fresh(user))
        self.assertEqual(client.get("/api/v1/purchasing/suppliers/").status_code, 200)
        self.client.force_login(user)
        self.assertNotEqual(self.client.get("/erp/").status_code, 403)

    def test_revert_on_save_logs_a_warning(self):
        user = make_staff_user("stale2", password="pw")
        stale = User.objects.get(pk=user.pk)
        user.groups.clear()
        with self.assertLogs("core.roles", level="WARNING") as logs:
            stale.save()
        self.assertIn("reverted", "\n".join(logs.output))
        self.assertFalse(self._fresh(user).is_staff)

    def test_receiver_runs_for_any_app_emission(self):
        """A model-less plugin app appended last must not silence role setup."""
        from django.apps import apps

        from core.roles import ensure_roles_after_migrate

        Group.objects.filter(name=CUSTOMER_ROLE).delete()
        ensure_roles_after_migrate(sender=None, app_config=apps.get_app_config("auth"),
                                   verbosity=0)
        self.assertTrue(Group.objects.filter(name=CUSTOMER_ROLE).exists())

    def test_first_sync_roles_adopts_instead_of_demoting(self):
        Group.objects.filter(name=STAFF_ROLE).delete()
        legacy = _legacy_staff("legacy")
        out = StringIO()
        call_command("sync_roles", stdout=out)
        self.assertIn("adopted into 'staff': ['legacy']", out.getvalue())
        self.assertTrue(self._fresh(legacy).is_staff)
        self.assertTrue(has_staff_role(legacy))

    def test_preexisting_group_member_without_flag_gets_no_api(self):
        """A group that merely shares the staff role's name does not open the API."""
        member = User.objects.create_user("lookalike", password="pw")
        through = User.groups.through
        through.objects.create(user_id=member.pk, group_id=self.staff_group.pk)  # no signal
        self.assertFalse(self._fresh(member).is_staff)
        client = APIClient()
        client.force_authenticate(user=self._fresh(member))
        self.assertEqual(client.get("/api/v1/purchasing/suppliers/").status_code, 403)

    def test_builtin_groups_cannot_be_deleted_or_renamed_in_admin(self):
        from django.contrib import admin
        from django.test import RequestFactory

        boss = User.objects.create_superuser("boss", "b@x.com", "pw")
        request = RequestFactory().get("/")
        request.user = boss
        group_admin = admin.site._registry[Group]
        self.assertFalse(group_admin.has_delete_permission(request, self.staff_group))
        self.assertIn("name", group_admin.get_readonly_fields(request, self.staff_group))
        other = Group.objects.create(name="ops")
        self.assertTrue(group_admin.has_delete_permission(request, other))

    def test_login_does_not_demote_an_unadopted_account(self):
        # update_last_login saves with update_fields=["last_login"].
        legacy = _legacy_staff("legacy-login")
        self.client.force_login(legacy)
        self.assertTrue(self._fresh(legacy).is_staff)


class AdoptionPathTests(TestCase):
    """Blocker 2: whichever code path creates the staff group adopts staff."""

    def setUp(self):
        Group.objects.filter(name=STAFF_ROLE).delete()
        self.legacy = _legacy_staff("legacy")

    def _adopted(self):
        return has_staff_role(User.objects.get(pk=self.legacy.pk))

    def test_grant_role_creating_the_group_adopts(self):
        grant_role(User.objects.create_user("new", password="pw"), STAFF_ROLE)
        self.assertTrue(self._adopted())

    def test_make_staff_user_creating_the_group_adopts(self):
        make_staff_user("helper", password="pw")
        self.assertTrue(self._adopted())

    @override_settings(DEMO_USERNAMES=["demo"], DEMO_LOGIN_USERNAME="demo",
                       DEMO_LOGIN_PASSWORD="try-it")
    def test_setup_demo_user_creating_the_group_adopts(self):
        call_command("setup_demo_user", stdout=StringIO())
        self.assertTrue(self._adopted())

    def test_adopt_existing_staff_creating_the_group_adopts_once(self):
        self.assertEqual(adopt_existing_staff(), ["legacy"])
        self.assertTrue(self._adopted())

    def test_dry_run_matches_the_real_run_when_group_is_missing(self):
        out = StringIO()
        call_command("sync_roles", "--dry-run", stdout=out)
        self.assertIn("would adopt into 'staff': ['legacy']", out.getvalue())
        self.assertIn("would demote (no staff role): nobody", out.getvalue())
        call_command("sync_roles", stdout=StringIO())
        self.assertTrue(self._adopted())
        self.assertTrue(User.objects.get(pk=self.legacy.pk).is_staff)


class PreexistingGroupTests(TestCase):
    """Item 6: a pre-existing look-alike group must not silently promote."""

    def setUp(self):
        Group.objects.filter(name=STAFF_ROLE).delete()
        self.outsider = User.objects.create_user("outsider", password="pw")
        # Simulate a group created before golden, written around the receivers.
        group = Group.objects.create(name=STAFF_ROLE)
        User.groups.through.objects.create(user_id=self.outsider.pk, group_id=group.pk)

    def test_ensure_roles_refuses(self):
        with self.assertRaisesRegex(ImproperlyConfigured, "outsider"):
            ensure_roles()
        self.assertFalse(User.objects.get(pk=self.outsider.pk).is_staff)

    def test_post_migrate_refuses(self):
        from django.apps import apps

        from core.roles import ensure_roles_after_migrate

        with self.assertRaises(ImproperlyConfigured):
            ensure_roles_after_migrate(sender=None, app_config=apps.get_app_config("core"),
                                       verbosity=0)

    @override_settings(GOLDEN_ROLES_ADOPT_PREEXISTING=True)
    def test_opt_in_takes_the_group_over(self):
        legacy = _legacy_staff("legacy")
        report = ensure_roles()
        self.assertEqual(report["promoted"], ["outsider"])
        self.assertIn("legacy", report["adopted"])
        self.assertTrue(User.objects.get(pk=self.outsider.pk).is_staff)
        self.assertTrue(has_staff_role(legacy))

    def test_no_access_while_unresolved(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get("/erp/").status_code, 403)


class SecondReviewTests(TestCase):
    def test_refresh_then_revoke_then_save_does_not_regrant(self):
        user = User.objects.create_user("flip", password="pw")
        copy_a = User.objects.get(pk=user.pk)  # loaded is_staff=False
        grant_role(user, STAFF_ROLE)
        copy_a.refresh_from_db()  # now True in memory
        user.groups.clear()  # revoked elsewhere
        copy_a.first_name = "Edited"
        copy_a.save()
        fresh = User.objects.get(pk=user.pk)
        self.assertFalse(fresh.is_staff)
        self.assertFalse(has_staff_role(fresh))

    @override_settings(GOLDEN_ROLES_ADOPT_PREEXISTING=True)
    def test_dry_run_honours_the_setting(self):
        user = make_staff_user("drifted", password="pw")
        User.objects.filter(pk=user.pk).update(is_staff=False)
        out = StringIO()
        call_command("sync_roles", "--dry-run", stdout=out)
        self.assertNotIn("would refuse", out.getvalue())
        self.assertIn("would promote (staff role, no flag): ['drifted']", out.getvalue())

    def test_stranded_staff_are_printed_after_migrate(self):
        from contextlib import redirect_stdout

        from django.apps import apps

        from core.roles import ensure_roles_after_migrate

        _legacy_staff("stranded")  # staff group already exists: no adoption
        out = StringIO()
        with redirect_stdout(out):
            ensure_roles_after_migrate(sender=None, app_config=apps.get_app_config("core"),
                                       verbosity=1)
        self.assertIn("stranded", out.getvalue())

    def test_plugin_gate_and_catalog_staff_checks_use_the_principal(self):
        from django.test import RequestFactory

        from core.plugins.url_auth import _is_staff
        from products.serializers import is_staff_reader

        legacy = _legacy_staff("halfstaff")
        whole = make_staff_user("whole", password="pw")
        self.assertFalse(_is_staff(legacy))
        self.assertTrue(_is_staff(whole))
        request = RequestFactory().get("/")
        request.user = legacy
        self.assertFalse(is_staff_reader(request))
        request.user = whole
        self.assertTrue(is_staff_reader(request))
