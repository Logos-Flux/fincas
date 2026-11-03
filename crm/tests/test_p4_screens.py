"""CRM Phase 4 step 6: the CRM screens (docs/crm/phase4-fields.md 5.1, 5.2, 2.5, 2.7).

Detail section order and the Profile section, definition fieldsets in the
party / engagement / task forms, the Other data panel, owner pickers limited
to active people with "(archived)" on archived names, the owner team on the
ERP forms, the ``/erp/settings/teams/`` page, merge-review rows and sidecar
field picks, and the Apply button on the last auto-submitting filter forms.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse

from core import sidecar
from core.models import AuditLog, Team, UserProfile
from core.staff import archive_profile
from core.tests.helpers import make_staff_user
from crm.models import Engagement, Task
from parties.merge import SIDECAR_CONFLICTS, merge, unmerge_group
from parties.models import Party, PartyMergeLog

from .helpers import CrmTestCase, organisation, person, profile

DEFS = {
    "party": [
        {"key": "custom.segment", "label": "Segment", "type": "choice",
         "choices": [["smb", "Small business"], ["ent", "Enterprise"]], "group": "Qualification",
         "list": True, "filterable": True},
        {"key": "custom.nickname", "label": "Nickname", "type": "string", "group": "Personal",
         "pii": True, "help": "What they like to be called."},
        {"key": "custom.since", "label": "Customer since", "type": "date", "group": "Qualification"},
    ],
    "engagement": [
        {"key": "custom.package", "label": "Package", "type": "choice",
         "choices": [["accelerator", "Accelerator"], ["other", "Other"]], "group": "Qualification",
         "required": True},
        {"key": "custom.budget", "label": "Budget", "type": "decimal", "group": "Qualification"},
    ],
    "task": [
        {"key": "custom.channel", "label": "Channel", "type": "choice",
         "choices": [["phone", "Phone"], ["visit", "Visit"]], "group": "Follow-up"},
    ],
}
PII = "parties.export_pii"


def grant(user, *perms):
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))


def revoke(user, *perms):
    """Take ``perms`` away from ``user``: off the user and off every group it
    is in (the staff role grants ``parties.export_pii`` by default)."""
    for perm in perms:
        app_label, codename = perm.split(".")
        row = Permission.objects.get(content_type__app_label=app_label, codename=codename)
        user.user_permissions.remove(row)
        for group in user.groups.all():
            group.permissions.remove(row)


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFS)
class _Screens(CrmTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = make_staff_user("screens", password="pw")
        cls.me = UserProfile.objects.create(user=cls.user, company=cls.company, display_name="Sam")
        cls.west = Team.objects.create(company=cls.company, key="west", name="West")
        cls.gone = profile(cls.company, "gone")
        cls.gone.display_name = "Gone Person"
        cls.gone.save()

    def setUp(self):
        self.client.force_login(self.user)

    def fresh(self, obj):
        return type(obj)._base_manager.get(pk=obj.pk)


class PartyDetailTests(_Screens):
    def test_sections_follow_5_2_order(self):
        page = self.client.get(reverse("erp-party-detail", args=[self.ada.pk])).content.decode()
        ids = ["contact-points", "next-actions", "engagements", "profile", "relationships", "timeline",
               "consent"]
        positions = [page.index(f'id="{i}"') for i in ids]
        self.assertEqual(positions, sorted(positions))
        side = ["roles", "tags"]
        self.assertLess(page.index('id="roles"'), page.index('id="tags"'))
        self.assertTrue(all(f'id="{i}"' in page for i in side))

    def test_profile_lists_lifecycle_contact_type_and_every_definition(self):
        Party.objects.filter(pk=self.ada.pk).update(
            lifecycle_stage="lead", lifecycle_stage_changed_at=datetime.datetime(2026, 3, 2, 15, tzinfo=datetime.UTC))
        sidecar.set_fields(self.fresh(self.ada), {"custom.segment": "ent"}, source="erp")
        response = self.client.get(reverse("erp-party-detail", args=[self.ada.pk]))
        self.assertContains(response, "Lifecycle: Lead")
        self.assertContains(response, "since Mar 2, 2026")
        self.assertContains(response, "Enterprise")
        self.assertContains(response, "Customer since")  # empty, listed, not hidden
        self.assertContains(response, '<span class="visually-hidden">unknown</span>', html=True)
        self.assertContains(response, "Qualification")

    def test_pii_definition_needs_export_pii(self):
        sidecar.set_fields(self.fresh(self.ada), {"custom.nickname": "Countess"}, source="erp")
        grant(self.user, PII)
        self.assertContains(self.client.get(reverse("erp-party-detail", args=[self.ada.pk])), "Countess")
        revoke(self.user, PII)
        response = self.client.get(reverse("erp-party-detail", args=[self.ada.pk]))
        self.assertNotContains(response, "Countess")
        self.assertContains(response, "Hidden: personal data")

    def test_other_data_shows_provider_keys_escaped_and_truncated(self):
        sidecar.set_fields(self.fresh(self.ada), {
            "hubspot.note": "<b>bold</b>" + "x" * 300, "custom.segment": "smb",
        }, source="import:hubspot")
        grant(self.user, PII)
        response = self.client.get(reverse("erp-party-detail", args=[self.ada.pk]))
        page = response.content.decode()
        self.assertIn('id="other-data"', page)
        self.assertIn("hubspot.note", page)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", page)
        self.assertNotIn("x" * 250, page)
        # A defined key renders in Profile, not in Other data.
        other = page[page.index('id="other-data"'):]
        self.assertNotIn("custom.segment", other)
        revoke(self.user, PII)
        page = self.client.get(reverse("erp-party-detail", args=[self.ada.pk])).content.decode()
        self.assertNotIn("hubspot.note", page)
        self.assertIn("1 imported value", page)

    def test_next_actions_and_engagements(self):
        eng = self.engagement(party=self.ada, title="Ada deal", owner=self.rep)
        from crm import tasks

        tasks.create(engagement=eng, title="Call Ada back", due=datetime.date(2030, 1, 1))
        response = self.client.get(reverse("erp-party-detail", args=[self.ada.pk]))
        self.assertContains(response, "Call Ada back")
        self.assertContains(response, eng.number)

    def test_owner_renders_with_team_and_archived_suffix(self):
        self.gone.team = self.west
        self.gone.save()
        party = organisation(self.company, "Owned Co", owner=self.gone)
        archive_profile(self.gone)
        response = self.client.get(reverse("erp-party-detail", args=[party.pk]))
        self.assertContains(response, "Gone Person (archived) · West")

    def test_another_tenants_party_is_404(self):
        other = organisation(self.other_company, "Elsewhere")
        self.assertEqual(self.client.get(reverse("erp-party-detail", args=[other.pk])).status_code, 404)


class PartyFormTests(_Screens):
    def test_form_has_classification_and_definition_fieldsets(self):
        grant(self.user, PII)
        page = self.client.get(reverse("erp-party-edit", args=[self.ada.pk])).content.decode()
        for name in ("lifecycle_stage", "sc__custom__segment", "sc__custom__nickname", "sc__custom__since"):
            self.assertIn(f'name="{name}"', page)
        self.assertIn("<legend", page)
        self.assertIn('aria-describedby="id_sc__custom__nickname_help"', page)
        self.assertIn('id="id_sc__custom__nickname_help"', page)

    def test_save_writes_columns_and_sidecar_through_the_validator(self):
        grant(self.user, PII)
        response = self.client.post(reverse("erp-party-edit", args=[self.ada.pk]), {
            "given_name": "Ada", "family_name": "Lovelace", "display_name": "Ada Lovelace",
            "lifecycle_stage": "customer", "sc__custom__segment": "smb", "sc__custom__nickname": "Countess",
            "sc__custom__since": "2026-01-05",
        })
        self.assertEqual(response.status_code, 302)
        ada = self.fresh(self.ada)
        self.assertEqual(ada.lifecycle_stage, "customer")
        self.assertEqual(ada.metadata["custom.segment"], "smb")
        self.assertEqual(ada.metadata["custom.since"], "2026-01-05")
        audit = AuditLog.objects.filter(action="update_fields", object_id=ada.pk).latest("created_at")
        self.assertEqual(audit.changes["set_pii"], ["custom.nickname"])
        self.assertNotIn("Countess", str(audit.changes))

    def test_an_invalid_choice_is_a_form_error_and_writes_nothing(self):
        response = self.client.post(reverse("erp-party-edit", args=[self.ada.pk]), {
            "given_name": "Ada", "family_name": "Lovelace", "sc__custom__segment": "nope",
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("custom.segment", self.fresh(self.ada).metadata)

    def test_pii_field_is_not_offered_or_written_without_the_permission(self):
        sidecar.set_fields(self.fresh(self.ada), {"custom.nickname": "Countess"}, source="erp")
        revoke(self.user, PII)
        page = self.client.get(reverse("erp-party-edit", args=[self.ada.pk])).content.decode()
        self.assertNotIn("sc__custom__nickname", page)
        self.assertNotIn("Countess", page)
        self.client.post(reverse("erp-party-edit", args=[self.ada.pk]), {
            "given_name": "Ada", "family_name": "Lovelace", "sc__custom__nickname": "",
        })
        self.assertEqual(self.fresh(self.ada).metadata["custom.nickname"], "Countess")

    def test_create_with_definition_values(self):
        response = self.client.post(reverse("erp-party-create"), {
            "kind": "organisation", "display_name": "Defined Co", "sc__custom__segment": "ent",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Party.objects.get(display_name="Defined Co").metadata["custom.segment"], "ent")

    def test_owner_picker_offers_active_people_and_keeps_the_current_owner(self):
        party = organisation(self.company, "Kept Co", owner=self.gone)
        archive_profile(self.gone)
        page = self.client.get(reverse("erp-party-edit", args=[party.pk])).content.decode()
        self.assertIn("Gone Person (archived)", page)  # the current value stays selectable
        page = self.client.get(reverse("erp-party-edit", args=[self.org.pk])).content.decode()
        self.assertNotIn("Gone Person", page)
        response = self.client.post(reverse("erp-party-edit", args=[self.org.pk]), {
            "display_name": self.org.display_name, "owner": self.gone.pk,
        })
        self.assertEqual(response.status_code, 200)  # not a valid choice
        self.assertIsNone(self.fresh(self.org).owner_id)

    def test_owner_team_needs_reassign_permission(self):
        page = self.client.get(reverse("erp-party-edit", args=[self.org.pk])).content.decode()
        self.assertNotIn('name="owner_team"', page)
        grant(self.user, "crm.reassign_engagement")
        page = self.client.get(reverse("erp-party-edit", args=[self.org.pk])).content.decode()
        self.assertIn('name="owner_team"', page)
        self.client.post(reverse("erp-party-edit", args=[self.org.pk]), {
            "display_name": self.org.display_name, "owner_team": self.west.pk,
        })
        self.assertEqual(self.fresh(self.org).owner_team, self.west)


class EngagementScreenTests(_Screens):
    def test_definitions_render_inside_deal_facts_and_other_data_in_the_side(self):
        eng = self.engagement(owner=self.rep)
        sidecar.set_fields(eng, {"custom.package": "accelerator", "hubspot.deal_type": "new"},
                           source="import:hubspot")
        grant(self.user, PII)
        page = self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk])).content.decode()
        facts, timeline = page.index('id="deal-facts"'), page.index('id="timeline"')
        self.assertTrue(facts < page.index("Accelerator") < timeline)
        self.assertGreater(page.index('id="other-data"'), timeline)
        self.assertIn("hubspot.deal_type", page)

    def test_edit_form_writes_definitions_and_honours_required(self):
        eng = self.engagement(owner=self.me)
        url = reverse("erp-crm-engagement-edit", args=[eng.pk])
        base = {"party": str(self.org.pk), "owner": self.me.pk, "currency": "USD"}
        self.assertEqual(self.client.post(url, base).status_code, 400)  # Package is required
        response = self.client.post(url, {**base, "sc__custom__package": "other",
                                          "sc__custom__budget": "1250.5"})
        self.assertEqual(response.status_code, 302)
        meta = self.fresh(eng).metadata
        self.assertEqual((meta["custom.package"], meta["custom.budget"]), ("other", "1250.5"))

    def test_owner_team_direct_edit_needs_the_permission(self):
        eng = self.engagement(owner=self.me)
        url = reverse("erp-crm-engagement-edit", args=[eng.pk])
        data = {"party": str(self.org.pk), "owner": self.me.pk, "currency": "USD",
                "sc__custom__package": "other", "owner_team": self.west.pk}
        self.client.post(url, data)
        self.assertIsNone(self.fresh(eng).owner_team)  # field not offered: ignored
        grant(self.user, "crm.reassign_engagement")
        self.client.post(url, data)
        self.assertEqual(self.fresh(eng).owner_team, self.west)

    def test_task_form_writes_task_definitions(self):
        eng = self.engagement(owner=self.me)
        self.assertContains(self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk])),
                            'name="sc__custom__channel"')
        self.client.post(reverse("erp-crm-engagement-task-create", args=[eng.pk]), {
            "title": "Visit them", "kind": "follow_up", "priority": "normal", "sc__custom__channel": "visit",
        })
        task = Task.objects.get(title="Visit them")
        self.assertEqual(task.metadata["custom.channel"], "visit")

    def test_owner_picker_excludes_archived_people(self):
        archive_profile(self.gone)
        page = self.client.get(reverse("erp-crm-engagement-create")).content.decode()
        self.assertNotIn("Gone Person", page)
        eng = self.engagement(owner=self.rep)
        Engagement.objects.filter(pk=eng.pk).update(owner=self.gone)
        page = self.client.get(reverse("erp-crm-engagement-edit", args=[eng.pk])).content.decode()
        self.assertIn("Gone Person (archived)", page)


class TeamsPageTests(_Screens):
    def test_every_staff_user_reads_the_page_without_edit_controls(self):
        response = self.client.get(reverse("erp-settings-teams"))
        self.assertContains(response, "West")
        self.assertNotContains(response, reverse("erp-settings-team-create"))
        self.assertEqual(self.client.get(reverse("erp-settings-team-create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("erp-settings-team-edit", args=[self.west.pk]),
                                          {"name": "X"}).status_code, 403)

    def test_manage_teams_creates_edits_and_assigns_members(self):
        grant(self.user, "core.manage_teams")
        response = self.client.post(reverse("erp-settings-team-create"), {
            "name": "East", "key": "east", "sort_order": 1, "is_active": "on", "members": [self.rep.pk],
        })
        self.assertEqual(response.status_code, 302)
        east = Team.objects.get(company=self.company, key="east")
        self.assertEqual(self.fresh(self.rep).team, east)
        self.client.post(reverse("erp-settings-team-edit", args=[east.pk]), {
            "name": "East coast", "key": "ignored", "sort_order": 1, "is_active": "on", "members": [],
        })
        east.refresh_from_db()
        self.assertEqual((east.name, east.key), ("East coast", "east"))  # key is immutable
        self.assertIsNone(self.fresh(self.rep).team)

    def test_duplicate_key_and_archived_people_are_refused(self):
        grant(self.user, "core.manage_teams")
        archive_profile(self.gone)
        response = self.client.post(reverse("erp-settings-team-create"), {
            "name": "Again", "key": "west", "sort_order": 0, "is_active": "on", "members": [self.gone.pk],
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Team.objects.filter(name="Again").exists())

    def test_another_tenants_team_is_404(self):
        grant(self.user, "core.manage_teams")
        theirs = Team.objects.create(company=self.other_company, key="west", name="Their west")
        self.assertEqual(self.client.get(reverse("erp-settings-team-edit", args=[theirs.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("erp-settings-teams")), "Their west")


class MergeSidecarTests(_Screens):
    def test_survivor_keeps_loser_only_keys_move_picks_win_and_unmerge_restores(self):
        a = person(self.company, "Ann", "Twin", lifecycle_stage="lead")
        b = person(self.company, "Ann", "Twin")
        sidecar.set_fields(a, {"custom.segment": "smb"}, source="erp")
        sidecar.set_fields(b, {"custom.segment": "ent", "custom.since": "2025-01-01", "hubspot.x": "y"},
                           source="import:hubspot")
        Party.objects.filter(pk=b.pk).update(lifecycle_stage="customer", contact_type="")
        logs = merge(a, [b], actor="tester")
        survivor = self.fresh(a)
        self.assertEqual(survivor.metadata["custom.segment"], "smb")      # survivor keeps
        self.assertEqual(survivor.metadata["custom.since"], "2025-01-01")  # loser-only moves
        self.assertEqual(survivor.metadata["hubspot.x"], "y")
        self.assertNotIn("custom.since", self.fresh(b).metadata)  # moved, not copied
        self.assertEqual(survivor.lifecycle_stage, "lead")
        self.assertEqual(logs[0].field_picks[SIDECAR_CONFLICTS], ["custom.segment"])
        unmerge_group(logs[0].merge_id, actor="tester")
        self.assertEqual(self.fresh(a).metadata, {"custom.segment": "smb"})
        self.assertEqual(self.fresh(b).metadata["custom.segment"], "ent")

    def test_picks_take_the_losers_value(self):
        a = person(self.company, "Bo", "Twin")
        b = person(self.company, "Bo", "Twin", lifecycle_stage="customer")
        Party.objects.filter(pk=a.pk).update(lifecycle_stage="lead")
        sidecar.set_fields(a, {"custom.segment": "smb"}, source="erp")
        sidecar.set_fields(b, {"custom.segment": "ent"}, source="erp")
        merge(self.fresh(a), [b], actor="tester",
              field_picks={"custom.segment": str(b.pk), "lifecycle_stage": str(b.pk)})
        survivor = self.fresh(a)
        self.assertEqual((survivor.metadata["custom.segment"], survivor.lifecycle_stage), ("ent", "customer"))
        self.assertEqual(PartyMergeLog.objects.get(survivor=a).field_picks[SIDECAR_CONFLICTS], ["custom.segment"])


class MergeReviewTests(_Screens):
    def test_review_rows_and_picks_for_phase_4_fields(self):
        from parties.merge_queue import scan
        from parties.models import PartyEmail

        grant(self.user, "parties.merge_party", PII)
        a = person(self.company, "Cy", "Twin", lifecycle_stage="lead")
        b = person(self.company, "Cy", "Twin", lifecycle_stage="customer")
        for p in (a, b):
            PartyEmail.objects.create(party=p, email="cy@example.com", purpose="work", is_primary=True)
        sidecar.set_fields(a, {"custom.segment": "smb"}, source="erp")
        sidecar.set_fields(b, {"custom.segment": "ent"}, source="erp")
        scan(self.company, actor="t")
        page = self.client.get(reverse("erp-party-merge-review")).content.decode()
        for text in ("Lifecycle stage", "Contact type", "Owner team", "Segment", "Enterprise"):
            self.assertIn(text, page)
        self.assertIn('name="pick_lifecycle_stage"', page)
        self.assertIn('name="pick_sc__custom__segment"', page)


class ApplyButtonTests(_Screens):
    """The last five filter forms that submitted on change (phase4-fields.md 5.7)."""

    def test_no_auto_submit_on_change_and_an_apply_button(self):
        grant(self.user, "parties.merge_party")
        self.user.is_superuser = True
        self.user.save()
        from parties.merge_queue import scan

        scan(self.company, actor="t")  # an ImportBatch for the batch-detail page
        from unify.models import ImportBatch

        batch = ImportBatch.objects.filter(company=self.company).first()
        urls = [reverse("erp-party-merge-review"), reverse("erp-report-list"),
                reverse("erp-accounting-trial-balance"), reverse("erp-accounting-reconciliation")]
        if batch is not None:
            urls.append(reverse("erp-unify-batch-detail", args=[batch.pk]))
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                page = response.content.decode()
                self.assertNotIn('onchange="this.form.submit()"', page)
                self.assertIn(">Apply<", page)
                self.assertIn("select[data-auto-submit]", page)


class ListColumnTests(_Screens):
    """5.1 defaults, sorting, filters and the screen gate for pii columns."""

    def test_party_defaults_and_lifecycle_sorts_in_funnel_order(self):
        response = self.client.get(reverse("erp-party-list"))
        visible = [c.id for c in response.context["visible_columns"]]
        self.assertEqual(visible, ["display_name", "roles", "lifecycle_stage", "email", "phone", "owner",
                                   "updated_at"])
        a = organisation(self.company, "Zed Customer", lifecycle_stage="customer")
        b = organisation(self.company, "Ace Lead", lifecycle_stage="lead")
        response = self.client.get(reverse("erp-party-list"), {"sort": "lifecycle_stage"})
        names = [p.pk for p in response.context["object_list"] if p.pk in (a.pk, b.pk)]
        self.assertEqual(names, [b.pk, a.pk])  # lead before customer, not alphabetical
        response = self.client.get(reverse("erp-party-list"), {"lifecycle_stage": "customer"})
        self.assertEqual([p.pk for p in response.context["object_list"]], [a.pk])

    def test_owner_filter_groups_archived_people(self):
        archive_profile(self.gone)
        page = self.client.get(reverse("erp-party-list")).content.decode()
        self.assertIn('<optgroup label="Archived">', page)
        self.assertIn("Gone Person (archived)", page[page.index('<optgroup label="Archived">'):])
        party = organisation(self.company, "Archived Owner Co", owner=self.gone)
        self.assertContains(self.client.get(reverse("erp-party-list")), "Gone Person (archived)")
        self.assertTrue(party)

    def test_crm_lists_render_from_the_registry_with_the_chooser(self):
        for name, expected in (
            ("erp-crm-engagement-list", ["number", "party", "stage", "next_action", "expected_value", "owner",
                                         "last_activity_at"]),
            ("erp-crm-task-list", ["title", "related", "due_at", "assignee", "priority", "status"]),
            ("erp-crm-lead-list", ["created_at", "name", "email", "source", "request_type", "engagement",
                                   "status"]),
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual([c.id for c in response.context["visible_columns"]], expected)
                self.assertIn("chooser", response.context)
                self.assertContains(response, 'id="column-chooser"')

    def test_phase_3_sort_ids_still_sort(self):
        response = self.client.get(reverse("erp-crm-engagement-list"), {"chip": "open", "sort": "value"})
        self.assertEqual(response.context["current_sort"], "expected_value")
        self.assertContains(response, 'aria-sort="ascending"')

    def test_attention_queue_keeps_its_own_columns(self):
        url = reverse("erp-crm-engagement-list")
        self.client.post(url + "?queue=attention", {"list_action": "columns", "columns": ["number", "owner"],
                                                    "query": "queue=attention"})
        attention = self.client.get(url, {"queue": "attention"})
        self.assertEqual([c.id for c in attention.context["visible_columns"]], ["number", "owner"])
        plain = self.client.get(url, {"chip": "open"})
        self.assertEqual(len(plain.context["visible_columns"]), 7)

    @override_settings(GOLDEN_FIELD_DEFINITIONS={
        "party": [{"key": "custom.nickname", "label": "Nickname", "type": "string", "list": True, "pii": True}],
    })
    def test_pii_definition_column_is_screen_gated(self):
        self.client.post(reverse("erp-party-list"), {"list_action": "columns",
                                                     "columns": ["display_name", "custom.nickname"]})
        response = self.client.get(reverse("erp-party-list"))
        self.assertIn("custom.nickname", [c.id for c in response.context["visible_columns"]])
        revoke(self.user, PII)
        response = self.client.get(reverse("erp-party-list"))
        self.assertNotIn("custom.nickname", [c.id for c in response.context["visible_columns"]])
        offered = [i["column"].id for _g, items in response.context["chooser"]["groups"] for i in items]
        self.assertNotIn("custom.nickname", offered)


class ReviewFixTests(_Screens):
    def _cluster(self):
        from parties.merge_queue import scan
        from parties.models import PartyEmail

        a = person(self.company, "Di", "Twin")
        b = person(self.company, "Di", "Twin")
        for p in (a, b):
            PartyEmail.objects.create(party=p, email="di@example.com", purpose="work", is_primary=True)
        scan(self.company, actor="t")
        from unify.models import StagedRow

        return a, b, StagedRow.objects.get(batch__company=self.company)

    def test_crafted_pii_and_team_picks_are_ignored_without_the_permissions(self):
        grant(self.user, "parties.merge_party")
        a, b, row = self._cluster()
        revoke(self.user, PII)
        self.client.post(reverse("erp-party-merge-review"), {
            "action": "approve", "row": row.pk, "survivor": a.pk,
            "pick_sc__custom__nickname": b.pk, "pick_owner_team": b.pk, "pick_lifecycle_stage": b.pk,
        })
        row.refresh_from_db()
        picks = row.mapped_values["field_picks"]
        self.assertNotIn("custom.nickname", picks)
        self.assertNotIn("owner_team", picks)
        self.assertEqual(picks["lifecycle_stage"], str(b.pk))

    def test_owner_team_is_never_blank_filled_by_a_merge(self):
        team = Team.objects.create(company=self.company, key="east", name="East")
        a = person(self.company, "Ed", "Twin")
        b = person(self.company, "Ed", "Twin")
        Party.objects.filter(pk=b.pk).update(owner_team=team)
        merge(a, [b], actor="t")
        self.assertIsNone(self.fresh(a).owner_team_id)

    def test_from_the_owner_restamps_the_team(self):
        grant(self.user, "crm.reassign_engagement")
        self.me.team = self.west
        self.me.save()
        east = Team.objects.create(company=self.company, key="east", name="East")
        eng = self.engagement(owner=self.me)
        Engagement.objects.filter(pk=eng.pk).update(owner_team=east)
        self.client.post(reverse("erp-crm-engagement-edit", args=[eng.pk]), {
            "party": str(self.org.pk), "owner": self.me.pk, "currency": "USD",
            "sc__custom__package": "other", "owner_team": "",
        })
        self.assertEqual(self.fresh(eng).owner_team, self.west)
        party = organisation(self.company, "Team Co", owner=self.me)
        Party.objects.filter(pk=party.pk).update(owner_team=east)
        self.client.post(reverse("erp-party-edit", args=[party.pk]), {
            "display_name": "Team Co", "owner": self.me.pk, "owner_team": "",
        })
        self.assertEqual(self.fresh(party).owner_team, self.west)

    def test_reassign_keeps_an_archived_assignee_valid(self):
        from crm import tasks

        eng = self.engagement(owner=self.me)
        task = tasks.create(engagement=eng, title="Old", assignee=self.gone)
        archive_profile(self.gone)
        response = self.client.post(reverse("erp-crm-task-reassign", args=[task.pk]), {"assignee": self.gone.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.fresh(task).assignee, self.gone)
