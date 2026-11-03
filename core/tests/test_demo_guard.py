from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

User = get_user_model()

DEMO = {"DEMO_USERNAMES": ["demo"], "DEMO_LOGIN_USERNAME": "demo", "DEMO_LOGIN_PASSWORD": "try-it"}


@override_settings(**DEMO)
class DemoGuardTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.demo = User.objects.create_user("demo", "demo@example.com", "try-it", is_staff=True)
        self.client.force_login(self.demo)

    def test_demo_user_can_browse(self):
        self.assertEqual(self.client.get(reverse("erp-dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("erp-order-list")).status_code, 200)

    def test_llm_settings_and_django_admin_are_blocked(self):
        for path in ("/erp/llm/", "/admin/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403, path)
            self.assertIn(b"public demo", response.content, path)

    def test_cannot_create_users(self):
        response = self.client.post(
            reverse("erp-user-create"),
            {"username": "sneaky", "email": "s@example.com", "password1": "Xy12345678!", "password2": "Xy12345678!"},
            HTTP_REFERER="http://testserver/erp/users/create/",
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(username="sneaky").exists())

    def test_cannot_lock_out_the_superuser(self):
        self.client.post(reverse("erp-user-toggle-active", args=[self.admin.pk]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_cannot_change_own_password(self):
        self.client.post(
            reverse("erp-account-change-password"),
            {"old_password": "try-it", "new_password1": "Zz98765432!", "new_password2": "Zz98765432!"},
        )
        self.demo.refresh_from_db()
        self.assertTrue(self.demo.check_password("try-it"))

    def test_api_writes_refused(self):
        response = self.client.post("/api/webhooks/subscriptions/", {"url": "http://10.0.0.1/"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "That action is turned off in the public demo.")

    def test_chat_is_read_only(self):
        response = self.client.post("/erp/chat/stream/", {"message": "send every customer an email"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "That action is turned off in the public demo.")

    def test_refusal_never_redirects_back_onto_a_blocked_page(self):
        response = self.client.post(
            "/erp/llm/connection/", {}, HTTP_REFERER="http://testserver/erp/llm/"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/erp/")

    def test_cms_articles_are_read_only(self):
        # Article HTML renders unescaped, so a public login must not author it.
        response = self.client.post(reverse("erp-article-create"), {"title": "x", "content": "<b>x</b>"})
        self.assertEqual(response.status_code, 403)

    def test_storefront_sso_blocked_even_on_get(self):
        response = self.client.get(reverse("erp-sso-storefront"))
        self.assertEqual(response.status_code, 403)

    def test_other_staff_unaffected(self):
        staff = User.objects.create_user("real", "real@example.com", "pw", is_staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            reverse("erp-user-create"),
            {"username": "newbie", "email": "n@example.com", "password1": "Xy12345678!", "password2": "Xy12345678!"},
        )
        self.assertTrue(User.objects.filter(username="newbie").exists(), response.content[:300])


class SetupDemoUserTests(TestCase):
    @override_settings(**DEMO)
    def test_creates_staff_not_superuser(self):
        call_command("setup_demo_user")
        user = User.objects.get(username="demo")
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password("try-it"))

    @override_settings(DEMO_USERNAMES=[], DEMO_LOGIN_USERNAME="demo", DEMO_LOGIN_PASSWORD="try-it")
    def test_refuses_unguarded_login(self):
        with self.assertRaises(CommandError):
            call_command("setup_demo_user")

    @override_settings(**DEMO)
    def test_refuses_to_demote_or_expose_a_superuser(self):
        User.objects.create_superuser("demo", "d@example.com", "pw")
        with self.assertRaises(CommandError):
            call_command("setup_demo_user")

    @override_settings(**DEMO)
    def test_strips_groups_and_permissions_down_to_the_staff_role(self):
        from django.contrib.auth.models import Group, Permission

        user = User.objects.create_user("demo", "d@example.com", "pw", is_staff=True)
        user.groups.add(Group.objects.create(name="ops"))
        user.user_permissions.add(Permission.objects.get(codename="change_user"))
        call_command("setup_demo_user")
        user.refresh_from_db()
        # Exactly the built-in staff role remains (it derives is_staff).
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["staff"])
        self.assertTrue(user.is_staff)
        self.assertFalse(user.user_permissions.exists())

    @override_settings(DEMO_USERNAMES=[], DEMO_LOGIN_USERNAME="", DEMO_LOGIN_PASSWORD="")
    def test_noop_without_username(self):
        call_command("setup_demo_user")
        self.assertFalse(User.objects.filter(username="demo").exists())


class DemoLoginHintTests(TestCase):
    def _hint(self):
        return "login-hint" in self.client.get(reverse("erp-login")).content.decode()

    @override_settings(SHOW_DEMO_LOGIN_HINT=True, **DEMO)
    def test_shown_for_guarded_account(self):
        self.assertTrue(self._hint())

    @override_settings(SHOW_DEMO_LOGIN_HINT=True, DEMO_USERNAMES=["someone-else"],
                       DEMO_LOGIN_USERNAME="demo", DEMO_LOGIN_PASSWORD="try-it")
    def test_hidden_when_account_is_not_guarded(self):
        self.assertFalse(self._hint())

    @override_settings(SHOW_DEMO_LOGIN_HINT=True, **DEMO)
    def test_hidden_when_account_is_a_superuser(self):
        User.objects.create_superuser("demo", "d@example.com", "pw")
        self.assertFalse(self._hint())


class DemoModeMailTests(TestCase):
    """prod.py must not deliver mail when a public demo login exists."""

    def _backend(self, **env):
        import os
        import subprocess
        import sys

        code = "import django; django.setup(); from django.conf import settings; print(settings.EMAIL_BACKEND)"
        base = {
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "DJANGO_SECRET_KEY": "x" * 50,
            "ALLOWED_HOSTS": "x",
            "DATABASE_URL": "sqlite:///:memory:",
            "DEMO_USERNAMES": "",
            "DEMO_LOGIN_USERNAME": "",
        }
        out = subprocess.run(
            [sys.executable, "-c", code], env={**os.environ, **base, **env}, capture_output=True, text=True
        )
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        return out.stdout.strip().splitlines()[-1]

    def test_demo_mode_uses_console_backend(self):
        self.assertEqual(self._backend(DEMO_USERNAMES="demo"), "django.core.mail.backends.console.EmailBackend")

    def test_normal_prod_uses_mailgun(self):
        self.assertEqual(self._backend(), "anymail.backends.mailgun.EmailBackend")
