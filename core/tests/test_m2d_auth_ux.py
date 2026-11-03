"""Milestone 2 Track D — auth UX: login/logout, password change, email
verify, password reset.

Covers testing-plan §5 "Auth UX": expired links, reused tokens, and
mismatched users all fail with a human message, never a 500.
SSO handoff itself is covered by core/tests/test_sso_views.py.
"""

from __future__ import annotations

import time
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.test import TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from core.models import ERPUserSettings

PASSWORD = "AuthUx-Str0ng-pass!"


class AuthUxMixin:
    def setUp(self):
        self.user = User.objects.create_user(
            username="authux",
            password=PASSWORD,
            email="authux@example.com",
            is_staff=True,
        )
        ERPUserSettings.objects.create(user=self.user)


class LoginLogoutTests(AuthUxMixin, TestCase):
    def test_login_page_renders(self):
        response = self.client.get(reverse("erp-login"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrfmiddlewaretoken", response.content.decode())

    def test_demo_login_hint_hidden_by_default(self):
        response = self.client.get(reverse("erp-login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("login-hint", response.content.decode())

    def test_demo_login_hint_shows_demo_credentials(self):
        from django.test.utils import override_settings

        with override_settings(
            SHOW_DEMO_LOGIN_HINT=True,
            DEMO_LOGIN_USERNAME="demo",
            DEMO_LOGIN_PASSWORD="try-it",
            DEMO_USERNAMES=["demo"],
        ):
            response = self.client.get(reverse("erp-login"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("login-hint", html)
        self.assertIn("<code>demo</code>", html)
        self.assertIn("<code>try-it</code>", html)
        self.assertNotIn("<code>admin</code>", html)

    def test_demo_login_hint_hidden_without_demo_credentials(self):
        from django.test.utils import override_settings

        with override_settings(SHOW_DEMO_LOGIN_HINT=True, DEMO_LOGIN_USERNAME="", DEMO_LOGIN_PASSWORD=""):
            response = self.client.get(reverse("erp-login"))
        self.assertNotIn("login-hint", response.content.decode())

    def test_valid_login_reaches_erp(self):
        response = self.client.post(
            reverse("erp-login"),
            {"username": "authux", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 302)
        home = self.client.get(reverse("erp-dashboard"))
        self.assertEqual(home.status_code, 200)

    def test_invalid_login_rerenders_with_error_never_500(self):
        response = self.client.post(
            reverse("erp-login"),
            {"username": "authux", "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode().lower()
        self.assertTrue(
            "correct" in html or "invalid" in html or "error" in html,
            "expected a human login-error message",
        )

    def test_logout_redirects_and_session_ends(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("erp-logout"))
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/erp/login/", response["Location"])
        dashboard = self.client.get(reverse("erp-dashboard"))
        self.assertIn(dashboard.status_code, (301, 302))


class PasswordChangeTests(AuthUxMixin, TestCase):
    url = staticmethod(lambda: reverse("erp-account-change-password"))

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_valid_change_redirects_and_new_password_works(self):
        response = self.client.post(self.url(), {
            "old_password": PASSWORD,
            "new_password1": "AuthUx-Brand-New-pass!",
            "new_password2": "AuthUx-Brand-New-pass!",
        })
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("AuthUx-Brand-New-pass!"))

    def test_wrong_old_password_rerenders_with_field_errors(self):
        response = self.client.post(self.url(), {
            "old_password": "not-the-password",
            "new_password1": "AuthUx-Brand-New-pass!",
            "new_password2": "AuthUx-Brand-New-pass!",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("old_password", response.context["form"].errors)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))


class EmailVerifyTests(AuthUxMixin, TestCase):
    def _url(self, token):
        return reverse("erp-verify-email", kwargs={"token": token})

    def test_valid_token_verifies(self):
        token = signing.dumps(self.user.pk, salt="email-verify")
        response = self.client.get(self._url(token))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["success"])
        settings_obj = ERPUserSettings.objects.get(user=self.user)
        self.assertTrue(settings_obj.email_verified)

    def test_reused_token_stays_successful_never_500(self):
        """Re-clicking a verification link is idempotent, not an error."""
        token = signing.dumps(self.user.pk, salt="email-verify")
        self.client.get(self._url(token))
        again = self.client.get(self._url(token))
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.context["success"])

    def test_tampered_token_fails_with_human_message_never_500(self):
        token = signing.dumps(self.user.pk, salt="email-verify") + "tampered"
        response = self.client.get(self._url(token))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["success"])
        html = response.content.decode().lower()
        self.assertTrue(
            "invalid" in html or "expired" in html or "verif" in html,
            "expected a human invalid/expired-link message",
        )

    def test_expired_token_fails_cleanly_never_500(self):
        eight_days_ago = time.time() - 8 * 86400
        with mock.patch("django.core.signing.time.time",
                        return_value=eight_days_ago):
            token = signing.dumps(self.user.pk, salt="email-verify")
        response = self.client.get(self._url(token))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["success"])

    def test_token_for_deleted_user_fails_cleanly_never_500(self):
        token = signing.dumps(self.user.pk, salt="email-verify")
        self.user.delete()
        response = self.client.get(self._url(token))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["success"])


class PasswordResetTests(AuthUxMixin, TestCase):
    def _reset_url(self, user=None):
        user = user or self.user
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return reverse(
            "erp-password-reset-confirm", kwargs={"uidb64": uid, "token": token}
        ), token

    def test_reset_request_page_renders(self):
        response = self.client.get(reverse("erp-password-reset"))
        self.assertEqual(response.status_code, 200)

    def test_valid_link_renders_set_password_form(self):
        url, _ = self._reset_url()
        response = self.client.get(url)
        # Django follows the link internally to a session-keyed URL.
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(response["Location"])
        self.assertEqual(follow.status_code, 200)

    def test_full_reset_changes_password(self):
        url, _ = self._reset_url()
        self.client.get(url)  # establishes the one-time session token
        response = self.client.post(
            reverse("erp-password-reset-confirm",
                    kwargs={"uidb64": urlsafe_base64_encode(force_bytes(self.user.pk)),
                            "token": "set-password"}),
            {"new_password1": "AuthUx-Reset-pass!",
             "new_password2": "AuthUx-Reset-pass!"},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("AuthUx-Reset-pass!"))

    def test_reused_link_fails_with_human_message_never_500(self):
        """After a successful reset the same link must not 500 — Django
        renders its 'invalid link' message instead."""
        url, _ = self._reset_url()
        self.client.get(url)
        self.client.post(
            reverse("erp-password-reset-confirm",
                    kwargs={"uidb64": urlsafe_base64_encode(force_bytes(self.user.pk)),
                            "token": "set-password"}),
            {"new_password1": "AuthUx-Reset-pass!",
             "new_password2": "AuthUx-Reset-pass!"},
        )
        reused = self.client.get(url, follow=True)
        self.assertEqual(reused.status_code, 200)
        html = reused.content.decode().lower()
        self.assertTrue(
            "invalid" in html or "expired" in html or "reset" in html,
            "expected a human invalid/expired-link message",
        )

    def test_garbage_link_never_500(self):
        response = self.client.get(
            reverse("erp-password-reset-confirm",
                    kwargs={"uidb64": "AAAA", "token": "bad-token"}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
