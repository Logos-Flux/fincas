"""Release hardening 6.5: personal data.

`parties.export_pii` gates every screen that shows a `pii` definition
value (`core.sidecar_forms.can_view_pii`). This module sweeps the surfaces
in one place for a staff user without the permission: the party, engagement
and task definition fieldsets on detail pages and forms, the list column
chooser, and the Other data panel (hidden with a count, never the value).

Cited, not repeated: the CSV export withholds PII columns on every path and
audits what it withheld (`core/tests/test_list_infra.py`
`test_pii_columns_need_export_pii_on_every_path`,
`test_contact_shaped_names_are_blanked_without_export_pii`); the party
detail, edit form, list column and merge-review picks
(`crm/tests/test_p4_screens.py` `test_pii_definition_needs_export_pii`,
`test_pii_field_is_not_offered_or_written_without_the_permission`,
`test_pii_definition_column_is_screen_gated`,
`test_crafted_pii_and_team_picks_are_ignored_without_the_permissions`); the
import queue masks source PII without `crm_import.view_import_pii`
(`crm_import/tests/test_review_views.py`).

Erasure reaching providers through the adapters, with a stubbed transport,
is `parties/tests/test_erasure_sweep_adapters.py`: one `<provider>.erase`
outbox row per adapter that wrote to its provider, the refs flagged, and
`test_draining_sends_exactly_each_adapters_erase_fixture` asserting the
exact provider requests through `core.integrations.transport.use_transport`.
`core/tests/test_integration_adapter.py` covers the adapter contract. No
generic fake-adapter test is added: the five real adapters already exercise
the shared `erase` consumer end to end.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse

from core import sidecar
from core.models import UserProfile
from core.sidecar_forms import can_view_pii
from core.tests.helpers import make_staff_user
from crm import engagements, tasks
from crm.tests.helpers import CrmTestCase

PII = "parties.export_pii"
SECRET_VALUES = {
    "party": ("custom.nickname", "Countess-PII"),
    "engagement": ("custom.home_notes", "Lives-at-PII"),
    "task": ("custom.home_phone", "555-0100-PII"),
}
DEFS = {
    "party": [{"key": "custom.nickname", "label": "Nickname", "type": "string", "group": "Personal",
               "pii": True, "list": True}],
    "engagement": [{"key": "custom.home_notes", "label": "Home notes", "type": "string",
                    "group": "Personal", "pii": True}],
    "task": [{"key": "custom.home_phone", "label": "Home phone", "type": "string",
              "group": "Personal", "pii": True}],
}


def revoke(user, codename="export_pii"):
    row = Permission.objects.get(content_type__app_label="parties", codename=codename)
    user.user_permissions.remove(row)
    for group in user.groups.all():
        group.permissions.remove(row)


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFS)
class PiiGateSweepTests(CrmTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = make_staff_user("no-pii", password="pw")
        cls.profile = UserProfile.objects.create(user=cls.user, company=cls.company, display_name="NP")
        cls.eng = engagements.create(company=cls.company, party=cls.ada, title="Deal")
        cls.task = tasks.create(company=cls.company, title="Call", engagement=cls.eng, assignee=cls.profile)

    def setUp(self):
        for obj, (key, value) in ((self.ada, SECRET_VALUES["party"]), (self.eng, SECRET_VALUES["engagement"]),
                                  (self.task, SECRET_VALUES["task"])):
            sidecar.set_fields(type(obj)._base_manager.get(pk=obj.pk), {key: value}, source="erp")
        revoke(self.user)
        self.user = type(self.user).objects.get(pk=self.user.pk)  # drop the permission cache
        self.assertFalse(can_view_pii(self.user))
        self.client.force_login(self.user)

    def assert_no_pii(self, url, *values):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        page = response.content.decode()
        for value in values:
            self.assertNotIn(value, page, url)
        return page

    def test_party_detail_form_and_list_hide_the_value(self):
        nick = SECRET_VALUES["party"][1]
        page = self.assert_no_pii(reverse("erp-party-detail", args=[self.ada.pk]), nick)
        self.assertIn("Hidden: personal data", page)
        page = self.assert_no_pii(reverse("erp-party-edit", args=[self.ada.pk]), nick)
        self.assertNotIn("sc__custom__nickname", page)
        self.client.post(reverse("erp-party-list"), {"list_action": "columns",
                                                     "columns": ["display_name", "custom.nickname"]})
        self.assert_no_pii(reverse("erp-party-list"), nick)

    def test_engagement_detail_and_form_hide_the_value(self):
        notes = SECRET_VALUES["engagement"][1]
        self.assert_no_pii(reverse("erp-crm-engagement-detail", args=[self.eng.pk]), notes)
        page = self.assert_no_pii(reverse("erp-crm-engagement-edit", args=[self.eng.pk]), notes)
        self.assertNotIn("sc__custom__home_notes", page)

    def test_task_surfaces_hide_the_value(self):
        phone = SECRET_VALUES["task"][1]
        self.assert_no_pii(reverse("erp-crm-task-list"), phone)
        self.assert_no_pii(reverse("erp-crm-my-day"), phone)
        self.assert_no_pii(reverse("erp-crm-engagement-detail", args=[self.eng.pk]), phone)
        page = self.assert_no_pii(reverse("erp-crm-task-create"), phone)
        self.assertNotIn("sc__custom__home_phone", page)

    def test_other_data_panel_is_hidden_without_the_permission(self):
        party = type(self.ada)._base_manager.get(pk=self.ada.pk)
        sidecar.set_fields(party, {"hubspot.note": "Prefers-evening-calls-PII"}, source="import:hubspot")
        page = self.assert_no_pii(reverse("erp-party-detail", args=[self.ada.pk]), "Prefers-evening-calls-PII")
        self.assertIn('id="other-data"', page)
        self.assertNotIn("hubspot.note", page)

    def test_a_crafted_post_cannot_write_the_pii_field(self):
        self.client.post(reverse("erp-party-edit", args=[self.ada.pk]), {
            "given_name": "Ada", "family_name": "Lovelace", "sc__custom__nickname": "overwritten",
        })
        party = type(self.ada)._base_manager.get(pk=self.ada.pk)
        self.assertEqual(party.metadata.get("custom.nickname"), SECRET_VALUES["party"][1])

    def test_granting_the_permission_reveals_every_surface(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="parties", codename="export_pii"))
        self.client.force_login(type(self.user).objects.get(pk=self.user.pk))
        for url, value in ((reverse("erp-party-detail", args=[self.ada.pk]), SECRET_VALUES["party"][1]),
                           (reverse("erp-crm-engagement-detail", args=[self.eng.pk]),
                            SECRET_VALUES["engagement"][1])):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), value)
