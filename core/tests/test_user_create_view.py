"""MD.3 — tests for UserCreateView (/erp/users/create/).

Users created via the staff UI must come out with is_staff=True, or
StaffRequiredMixin (which gates every /erp/ view) rejects them at login.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import ERPUserSettings

PASSWORD = "unit-test-Str0ng-pass!"


class UserCreateViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staffer", password=PASSWORD, is_staff=True
        )
        self.client.force_login(self.staff)

    def _create(self, username="newhire"):
        return self.client.post(
            reverse("erp-user-create"),
            {
                "username": username,
                "email": f"{username}@example.com",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )

    def test_created_user_is_staff(self):
        """form_valid must set is_staff=True so the user passes
        StaffRequiredMixin.test_func (request.user.is_staff)."""
        response = self._create()
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="newhire")
        self.assertTrue(user.is_staff)
        # ...through the staff role, so derivation keeps it (identity step 2).
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["staff"])

    def test_created_user_gets_erp_settings_with_forced_password_change(self):
        self._create("newhire2")
        user = User.objects.get(username="newhire2")
        settings_obj = ERPUserSettings.objects.get(user=user)
        self.assertTrue(settings_obj.must_change_password)
        self.assertFalse(settings_obj.email_verified)

    def test_non_staff_cannot_access_create_view(self):
        pleb = User.objects.create_user(username="pleb", password=PASSWORD)
        self.client.force_login(pleb)
        response = self.client.get(reverse("erp-user-create"))
        self.assertNotEqual(response.status_code, 200)
