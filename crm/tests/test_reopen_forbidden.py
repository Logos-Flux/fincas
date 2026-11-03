"""A direct reopen without ``crm.reopen_engagement`` is the styled 403 (R2.04).

Found by the release harness on 2026-10-17 (docs/release-testing.md): the
Reopen action is hidden from a role without the permission (R2.03), but a
direct ``POST .../reopen/`` was caught by ``_EngagementAction.post``,
flashed and redirected (302). Decision 10 of the hardening plan: denied
actions are hidden; reaching one directly gives the styled 403 page.

Authorization is the 403; a business rule ("no staff profile") stays a
message, like every other ``ValidationError`` the actions raise.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission
from django.urls import reverse

from core.tests.test_error_pages import assert_styled
from crm import engagements as engagement_service

from .test_erp_views import CrmViewTestBase


class ReopenForbiddenTests(CrmViewTestBase):
    def post(self, name, eng, data=None):
        return self.client.post(reverse(name, args=[eng.pk]), data or {})

    def lost(self):
        eng = self.eng()
        engagement_service.lose(eng, "price", by=self.me, source="erp")
        eng.refresh_from_db()
        self.assertEqual(eng.status, "lost")
        return eng

    def test_without_the_permission_the_direct_post_is_the_styled_403_and_changes_nothing(self):
        eng = self.lost()
        self.assertFalse(self.user.has_perm("crm.reopen_engagement"))
        response = self.post("erp-crm-engagement-reopen", eng, {"stage": ""})
        assert_styled(self, response, 403, "403.html")
        self.assertContains(response, "crm.reopen_engagement", status_code=403)
        eng.refresh_from_db()
        self.assertEqual((eng.status, eng.loss_reason), ("lost", "price"))
        self.assertEqual(eng.stage_changes.filter(from_status="lost").count(), 0)
        # No flash rides along: the 403 is the answer, not a redirect.
        self.assertEqual([str(m) for m in response.wsgi_request._messages], [])

    def test_with_the_permission_the_post_reopens_and_redirects(self):
        eng = self.lost()
        self.user.user_permissions.add(Permission.objects.get(codename="reopen_engagement"))
        response = self.post("erp-crm-engagement-reopen", eng, {"stage": ""})
        self.assertRedirects(response, reverse("erp-crm-engagement-detail", args=[eng.pk]),
                             fetch_redirect_response=False)
        eng.refresh_from_db()
        self.assertEqual((eng.status, eng.loss_reason), ("open", ""))
        self.assertEqual(eng.stage_changes.filter(from_status="lost", to_status="open").count(), 1)

    def test_a_missing_staff_profile_is_a_message_not_a_403(self):
        eng = self.lost()
        # A superuser acting in another tenant: scoped in, but no "by" there.
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.me.company = self.other_company
        self.me.save(update_fields=["company"])
        response = self.post("erp-crm-engagement-reopen", eng, {"stage": ""})
        self.assertEqual(response.status_code, 302)
        shown = " ".join(str(m) for m in response.wsgi_request._messages)
        self.assertIn("staff profile", shown)
        eng.refresh_from_db()
        self.assertEqual(eng.status, "lost")
