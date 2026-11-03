"""Users & roles from the back office (release harness R10.02, ``R10-no-role-ui``).

``/erp/users/<pk>/roles/`` grants and revokes the registry's human roles
through ``core.roles``; ``is_staff`` follows (docs/roles.md). The create
form takes an initial role. Gate: ``auth.change_user``.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse

from core import user_roles
from core.tests.helpers import make_staff_user

ROLES = [
    {"name": "sales", "label": "Sales", "staff": True},
    {"name": "vip", "label": "VIP customer", "portal": True},
    {"name": "bot", "label": "Bot", "kind": "service"},
]


@override_settings(GOLDEN_ROLES=ROLES)
class UserRolesScreenTests(TestCase):
    def setUp(self):
        self.admin = make_staff_user("roles-admin", password="pw")
        self.admin.user_permissions.add(Permission.objects.get(codename="change_user", content_type__app_label="auth"))
        self.admin = User.objects.get(pk=self.admin.pk)
        self.target = User.objects.create_user("rowan", email="rowan@example.com", password="pw")
        self.client.force_login(self.admin)
        self.url = reverse("erp-user-roles", args=[self.target.pk])

    def test_human_roles_only_are_offered(self):
        self.assertEqual(user_roles.grantable_names(), ["staff", "customer", "sales", "vip"])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="sales"')
        self.assertNotContains(response, 'value="bot"')

    def test_granting_a_staff_role_sets_is_staff_and_revoking_clears_it(self):
        self.assertFalse(self.target.is_staff)
        response = self.client.post(self.url, {"roles": ["sales"]})
        self.assertRedirects(response, reverse("erp-user-list"))
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_staff)
        self.assertEqual(sorted(self.target.groups.values_list("name", flat=True)), ["sales"])
        # Staff on top; then drop both: the last staff role gone turns the flag off.
        self.client.post(self.url, {"roles": ["sales", "staff"]})
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_staff)
        self.client.post(self.url, {"roles": ["vip"]})
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_staff)
        self.assertEqual(list(self.target.groups.values_list("name", flat=True)), ["vip"])

    def test_a_service_role_is_refused_before_any_write(self):
        response = self.client.post(self.url, {"roles": ["sales", "bot"]}, follow=True)
        self.assertContains(response, "Not a role a person can hold: bot.")
        self.target.refresh_from_db()
        self.assertEqual(self.target.groups.count(), 0)

    def test_the_users_list_shows_roles_and_links_the_screen(self):
        user_roles.set_roles(self.target, {"sales"})
        response = self.client.get(reverse("erp-user-list"))
        self.assertContains(response, self.url)
        self.assertContains(response, "Sales")
        self.assertContains(response, 'aria-label="Change roles for rowan"')

    def test_without_the_permission_the_link_is_hidden_and_the_screen_is_403(self):
        viewer = make_staff_user("roles-viewer", password="pw")
        self.client.force_login(viewer)
        response = self.client.get(reverse("erp-user-list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"roles": ["sales"]}).status_code, 403)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_staff)

    def test_own_roles_cannot_be_changed(self):
        response = self.client.post(reverse("erp-user-roles", args=[self.admin.pk]), {"roles": []}, follow=True)
        self.assertContains(response, "You cannot change your own roles.")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)

    def test_create_user_with_an_initial_role(self):
        response = self.client.post(reverse("erp-user-create"), {
            "username": "newbie", "email": "newbie@example.com", "first_name": "New", "last_name": "Bie",
            "password1": "Harness-pw-1!", "password2": "Harness-pw-1!", "role": "sales",
        })
        self.assertRedirects(response, reverse("erp-user-list"))
        user = User.objects.get(username="newbie")
        self.assertTrue(user.is_staff)
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["sales"])

    def test_create_user_defaults_to_staff(self):
        self.client.post(reverse("erp-user-create"), {
            "username": "plain", "email": "plain@example.com",
            "password1": "Harness-pw-1!", "password2": "Harness-pw-1!",
        })
        user = User.objects.get(username="plain")
        self.assertTrue(user.is_staff)
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["staff"])
