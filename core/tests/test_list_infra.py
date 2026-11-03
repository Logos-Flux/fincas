"""List infrastructure (docs/crm/phase4-fields.md 5.1, 5.3-5.6; step 5).

The party list is the reference list: the column registry with definition
and plugin columns, server-side column preferences, saved filters, the
canonical CRM export and its ``parties.export_pii`` gate.
"""

from __future__ import annotations

import csv
import io
import re
import time
from unittest import mock

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core import list_columns
from core.list_columns import Column
from core.models import AuditLog, Company, ERPUserSettings, SavedFilter, UserProfile
from core.plugins.base import BasePlugin, ListColumn
from core.role_registry import ROLES, STAFF_ROLE
from core.tests.helpers import make_staff_user
from core.views.list_view import ERPListView
from parties.erp_views import PartyListView
from parties.models import Party

URL = reverse("erp-party-list")

DEFINITIONS = {
    "party": [
        {"key": "custom.tier", "label": "Tier", "type": "choice",
         "choices": [["gold", "Gold tier"], ["silver", "Silver tier"]],
         "list": True, "list_default": True},
        {"key": "custom.region", "label": "Region", "type": "string", "list": True, "list_default": True},
        {"key": "custom.vip", "label": "VIP", "type": "boolean", "list": True, "list_default": True},
        {"key": "custom.nickname", "label": "Nickname", "type": "string", "list": True, "pii": True},
        {"key": "custom.internal", "label": "Internal note", "type": "string", "export": False},
    ],
}


def party(company, name, **metadata):
    p = Party.objects.create(company=company, kind="organisation", display_name=name, origin="erp")
    if metadata:
        # Straight to the column: these tests are about reading, not the validator.
        Party.objects.filter(pk=p.pk).update(metadata=metadata)
    return p


def rows(response):
    body = b"".join(response.streaming_content).decode()
    return list(csv.reader(io.StringIO(body)))


class ListTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.staff = make_staff_user("list-a", password="pw")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"company": cls.company})
        cls.colleague = make_staff_user("list-a2", password="pw")
        UserProfile.objects.update_or_create(user=cls.colleague, defaults={"company": cls.company})
        cls.outsider = make_staff_user("list-b", password="pw")
        UserProfile.objects.update_or_create(user=cls.outsider, defaults={"company": cls.other_company})

    def setUp(self):
        self.client.force_login(self.staff)


# --- the registry -------------------------------------------------------------------


class RegistryTests(TestCase):
    def test_legacy_dicts_keep_their_field_as_id(self):
        cols = list_columns.from_legacy(
            [{"field": "email", "label": "Email"}, {"field": "customer__name", "label": "Customer", "priority": 3}],
            {"email": ("email",)},
        )
        self.assertEqual([c.id for c in cols], ["email", "customer__name"])
        self.assertTrue(cols[0].sortable)
        self.assertFalse(cols[1].sortable)
        self.assertEqual(cols[1].priority_class, "d-none d-lg-table-cell")
        self.assertTrue(all(c.default and c.source == "field" for c in cols))

    def test_ids_that_share_a_query_alias_are_refused(self):
        class Clash(ERPListView):
            model = Party
            list_columns = [Column("a.b_c", "One"), Column("a_b.c", "Two")]

        view = Clash()
        view.request = RequestFactory().get("/")
        with self.assertRaises(ImproperlyConfigured):
            view.get_columns()

    def test_bad_columns_are_refused(self):
        for kwargs in ({"id": "Bad Id"}, {"priority": 4}, {"source": "magic"}, {"format": "html"},
                       {"source": "definition", "sortable": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ImproperlyConfigured):
                list_columns.column(**{"id": "ok", "label": "Ok", **kwargs})

    def test_visible_ids_drop_unknown_and_cap(self):
        cols = [Column(f"c{i}", f"C{i}", default=i < 3) for i in range(15)]
        self.assertEqual(list_columns.visible_ids(cols, None), ["c0", "c1", "c2"])
        self.assertEqual(list_columns.visible_ids(cols, ["c5", "gone", "c1"]), ["c1", "c5"])
        self.assertEqual(list_columns.visible_ids(cols, ["gone"]), ["c0", "c1", "c2"])
        self.assertEqual(len(list_columns.visible_ids(cols, [c.id for c in cols])), list_columns.MAX_COLUMNS)

    def test_sidecar_values_are_canonical_in_csv_and_labelled_on_screen(self):
        col = Column("custom.tier", "Tier", source="definition", path="custom.tier",
                     choices=(("gold", "Gold tier"),))
        self.assertEqual(list_columns.display_value(col, "gold"), "Gold tier")
        self.assertEqual(list_columns.export_value(col, "gold"), "gold")
        multi = Column("custom.tags", "Tags", source="definition", path="custom.tags",
                       choices=(("a", "Alpha"), ("b", "Beta")))
        self.assertEqual(list_columns.display_value(multi, '["a", "b"]'), "Alpha, Beta")
        self.assertEqual(list_columns.export_value(multi, '["a", "b"]'), "a;b")
        flag = Column("custom.vip", "VIP", source="definition", path="custom.vip", format="boolean")
        self.assertEqual(list_columns.export_value(flag, "true"), "true")
        self.assertEqual(list_columns.display_value(flag, "false"), "No")
        self.assertEqual(list_columns.export_value(col, None), "")


# --- definition columns, query count, deferred metadata -------------------------------


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class DefinitionColumnTests(ListTestBase):
    def seed(self, n, start=0):
        for i in range(start, start + n):
            party(self.company, f"Org {i:03d}", **{"custom.tier": "gold", "custom.region": f"R{i}",
                                                   "custom.vip": True, "bulk": "x" * 2000})

    def test_three_definition_columns_render_by_default(self):
        self.seed(1)
        response = self.client.get(URL)
        visible = [c.id for c in response.context["visible_columns"]]
        for key in ("custom.tier", "custom.region", "custom.vip"):
            self.assertIn(key, visible)
        self.assertNotIn("custom.nickname", visible)  # list but not list_default
        offered = [i["column"].id for _g, items in response.context["chooser"]["groups"] for i in items]
        self.assertIn("custom.nickname", offered)
        self.assertNotIn("custom.internal", offered)  # export-only definition
        self.assertContains(response, "Gold tier")
        self.assertContains(response, "R0")
        self.assertContains(response, '<span title="Not sortable">Tier</span>', html=False)

    def test_query_count_is_constant_with_three_definition_columns(self):
        self.seed(3)
        self.client.get(URL)  # warm
        with CaptureQueriesContext(connection) as small:
            self.client.get(URL)
        self.seed(40, start=3)
        with CaptureQueriesContext(connection) as large:
            response = self.client.get(URL)
        self.assertEqual(len(response.context["object_list"]), 43)
        self.assertEqual(len(small), len(large))
        # The party list's budget without any definition (PartyListTests.
        # test_query_budget, 15): three definition columns add no query. The
        # pii definition (custom.nickname) adds the viewer's permission lookup
        # (user + group permissions, once per request: step 6's screen gate).
        with self.assertNumQueries(17):
            self.client.get(URL)

    def test_metadata_is_deferred_in_list_sql(self):
        self.seed(2)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(URL)
        for obj in response.context["object_list"]:
            self.assertIn("metadata", obj.get_deferred_fields())
        row_sql = [q["sql"] for q in ctx.captured_queries
                   if 'FROM "parties_party"' in q["sql"] and "COUNT(" not in q["sql"]]
        self.assertTrue(row_sql)
        for sql in row_sql:
            # The only reads of the column are JSON key extractions.
            bare = re.findall(r'"parties_party"\."metadata"(?!\s*(?:->|,\s*\'\$|\)))', sql)
            self.assertEqual(bare, [], sql)
            self.assertIn("custom.tier", sql)

    def test_definitions_scoped_to_another_tenant_are_not_columns(self):
        scoped = {"party": [{"key": "custom.secret", "label": "Secret", "type": "string", "list": True,
                             "list_default": True, "companies": [str(self.other_company.pk)]}]}
        with override_settings(GOLDEN_FIELD_DEFINITIONS=scoped):
            response = self.client.get(URL)
            self.assertNotIn("custom.secret", [c.id for c in response.context["visible_columns"]])
            self.client.force_login(self.outsider)
            response = self.client.get(URL)
            self.assertIn("custom.secret", [c.id for c in response.context["visible_columns"]])


# --- plugin columns -----------------------------------------------------------------


class OwnerNamePlugin(BasePlugin):
    name = "listext"

    def get_list_columns(self, list_id):
        if list_id != "parties.party":
            return []
        return [ListColumn(id="listext.owner_name", label="Owner name", path="owner__display_name",
                           sortable=True, default=True, priority=2)]


def bad_plugin(**column):
    class Bad(BasePlugin):
        name = "badext"

        def get_list_columns(self, list_id):
            return [ListColumn(**{"id": "badext.x", "label": "X", "path": "display_name", **column})]

    return Bad


class PluginColumnTests(ListTestBase):
    @override_settings(GOLDEN_PLUGINS=[OwnerNamePlugin])
    def test_plugin_column_renders_sorts_and_exports_without_queries(self):
        owner_profile = UserProfile.objects.get(user=self.staff)
        owner_profile.display_name = "Zed Owner"
        owner_profile.save()
        other_profile = UserProfile.objects.get(user=self.colleague)
        other_profile.display_name = "Amy Owner"
        other_profile.save()
        a = party(self.company, "Alpha")
        b = party(self.company, "Beta")
        Party.objects.filter(pk=a.pk).update(owner=other_profile)
        Party.objects.filter(pk=b.pk).update(owner=owner_profile)
        response = self.client.get(URL, {"sort": "listext.owner_name", "dir": "desc"})
        self.assertIn("listext.owner_name", [c.id for c in response.context["visible_columns"]])
        self.assertEqual([p.pk for p in response.context["object_list"]][:2], [b.pk, a.pk])
        self.assertContains(response, "Zed Owner")
        table = rows(self.client.get(URL, {"export": "csv"}))
        self.assertIn("listext.owner_name", table[0])
        self.assertIn("Zed Owner", [r[table[0].index("listext.owner_name")] for r in table[1:]])
        self.client.get(URL)
        with CaptureQueriesContext(connection) as with_plugin:
            self.client.get(URL)
        with override_settings(GOLDEN_PLUGINS=[]):
            self.client.get(URL)
            with CaptureQueriesContext(connection) as without:
                self.client.get(URL)
        self.assertEqual(len(with_plugin), len(without))

    def test_plugin_columns_cannot_reach_arbitrary_orm(self):
        cases = {
            "id not namespaced": {"id": "x"},
            "another plugin's namespace": {"id": "listext.x"},
            "reverse foreign key (multiplies rows)": {"path": "roles__role"},
            "credentials via auth": {"path": "owner__user__password"},
            "auth model": {"path": "user__username"},
            "a relation, not a value": {"path": "owner"},
            "unknown field": {"path": "nope"},
            "lookup smuggled in": {"path": "display_name__icontains"},
            "sidecar is not a plugin path": {"path": "metadata__custom"},
        }
        for why, column in cases.items():
            with self.subTest(why), override_settings(GOLDEN_PLUGINS=[bad_plugin(**column)]):
                with self.assertRaises(ImproperlyConfigured):
                    list_columns.plugin_columns("parties.party", Party)

    def test_credential_models_are_never_reachable(self):
        from core.identity.models import UserIdentity
        from core.models import ApiKey

        self.assertTrue(list_columns._denied(ApiKey))
        self.assertTrue(list_columns._denied(UserIdentity))
        self.assertFalse(list_columns._plugin_path_ok(UserProfile, "user__userprofile__display_name"))

    def test_reverse_one_to_one_is_allowed(self):
        self.assertTrue(list_columns._plugin_path_ok(Company, "document_sequence__next_number"))
        self.assertFalse(list_columns._plugin_path_ok(Company, "saved_filters__name"))


# --- column preferences (5.3) ----------------------------------------------------------


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class ColumnPreferenceTests(ListTestBase):
    def post(self, **data):
        return self.client.post(URL, {"list_action": "columns", "query": "q=Org&page=3&export=csv", **data})

    def prefs(self):
        row = ERPUserSettings.objects.filter(user=self.staff).first()
        return row.list_preferences if row else {}

    def test_save_is_server_side_and_redirects_to_the_same_query(self):
        response = self.post(columns=["display_name", "custom.nickname", "not-a-column"], page_size="100")
        self.assertRedirects(response, URL + "?q=Org", fetch_redirect_response=False)
        prefs = ERPUserSettings.objects.get(user=self.staff).list_preferences
        self.assertEqual(prefs["parties.party"], {"columns": ["display_name", "custom.nickname"],
                                                  "page_size": 100})
        response = self.client.get(URL)
        self.assertEqual([c.id for c in response.context["visible_columns"]],
                         ["display_name", "custom.nickname"])
        self.assertEqual(response.context["paginator"].per_page, 100)
        # Another user is untouched.
        self.client.force_login(self.colleague)
        self.assertIn("roles", [c.id for c in self.client.get(URL).context["visible_columns"]])

    def test_too_many_or_none_is_refused_and_reset_restores_defaults(self):
        offered = [c.id for c in PartyListView.list_columns if not c.export_only]
        offered += ["custom.tier", "custom.region", "custom.vip", "custom.nickname"]
        self.post(columns=offered)
        self.assertEqual(self.prefs(), {})
        self.post(columns=[])
        self.assertEqual(self.prefs(), {})
        self.post(columns=["display_name"])
        self.assertEqual(self.prefs(), {"parties.party": {"columns": ["display_name"]}})
        self.post(reset="1")
        self.assertEqual(self.prefs(), {})

    def test_a_removed_definition_drops_out_of_the_preference(self):
        self.post(columns=["display_name", "custom.region"])
        with override_settings(GOLDEN_FIELD_DEFINITIONS={}):
            response = self.client.get(URL)
        self.assertEqual([c.id for c in response.context["visible_columns"]], ["display_name"])

    def test_page_keeps_query_budget_with_a_preference(self):
        party(self.company, "Org")
        self.client.get(URL)
        with CaptureQueriesContext(connection) as before:
            self.client.get(URL)
        self.post(columns=["display_name", "custom.region"])
        self.client.get(URL)
        with CaptureQueriesContext(connection) as after:
            self.client.get(URL)
        self.assertEqual(len(before), len(after))

    def test_responsive_priority_classes(self):
        party(self.company, "Org")
        page = self.client.get(URL).content.decode()
        self.assertRegex(page, r'<th scope="col" class="[^"]*d-none d-md-table-cell[^"]*"')  # roles, owner
        self.assertRegex(page, r'<th scope="col" class="[^"]*d-none d-lg-table-cell[^"]*"')  # phone, updated
        self.assertIn("(shown on wider screens)", page)
        name_th = re.search(r'<th scope="col" class="([^"]*)"[^>]*>\s*(?:<[^>]+>)?\s*<a [^>]*sort=display_name', page)
        self.assertNotIn("d-none", name_th.group(1))

    def test_unknown_post_action_is_refused(self):
        self.assertEqual(self.client.post(URL, {"list_action": "drop_table"}).status_code, 400)


# --- saved filters (5.4) ------------------------------------------------------------------


class SavedFilterTests(ListTestBase):
    def save(self, name="Orgs", query="kind=organisation&q=Org&page=2&export=csv&evil=1", visibility="private"):
        return self.client.post(URL, {"list_action": "save_filter", "name": name, "query": query,
                                      "visibility": visibility})

    def test_params_are_validated_on_save(self):
        self.save()
        saved = SavedFilter.objects.get(owner=self.staff)
        self.assertEqual(saved.params, {"kind": "organisation", "q": "Org"})
        self.assertEqual((saved.company, saved.list_id), (self.company, "parties.party"))

    def test_params_are_revalidated_on_load(self):
        saved = SavedFilter.objects.create(
            company=self.company, owner=self.staff, list_id="parties.party", name="Old",
            params={"kind": "organisation", "custom.removed": "x", "sort": "metadata", "dir": "desc",
                    "role": ["not", "a", "string"], "show": "everything"},
        )
        response = self.client.get(URL, {"saved": str(saved.pk)})
        self.assertRedirects(response, URL + "?kind=organisation", fetch_redirect_response=False)
        page = self.client.get(response["Location"])
        self.assertContains(page, "no longer apply")
        chip = next(c for c in page.context["saved_filters"]["items"] if c["id"] == saved.pk)
        self.assertTrue(chip["stale"])
        self.assertTrue(chip["active"])

    def test_tenant_isolation(self):
        mine_private = SavedFilter.objects.create(company=self.company, owner=self.colleague,
                                                  list_id="parties.party", name="Theirs, private",
                                                  params={"kind": "person"})
        shared = SavedFilter.objects.create(company=self.company, owner=self.colleague, list_id="parties.party",
                                            name="Shared", params={"kind": "person"}, visibility="tenant")
        foreign = SavedFilter.objects.create(company=self.other_company, owner=self.outsider,
                                             list_id="parties.party", name="Foreign",
                                             params={"kind": "person"}, visibility="tenant")
        names = [c["name"] for c in self.client.get(URL).context["saved_filters"]["items"]]
        self.assertEqual(names, ["Shared"])
        for hidden in (mine_private, foreign):
            response = self.client.get(URL, {"saved": str(hidden.pk)})
            self.assertRedirects(response, URL, fetch_redirect_response=False)
            self.client.post(URL, {"list_action": "delete_filter", "filter": str(hidden.pk)})
            self.assertTrue(SavedFilter.objects.filter(pk=hidden.pk).exists())
        # A shared filter is not mine to delete without core.change_savedfilter.
        self.client.post(URL, {"list_action": "delete_filter", "filter": str(shared.pk)})
        self.assertTrue(SavedFilter.objects.filter(pk=shared.pk).exists())
        self.staff.user_permissions.add(Permission.objects.get(codename="change_savedfilter"))
        self.client.post(URL, {"list_action": "delete_filter", "filter": str(shared.pk)})
        self.assertFalse(SavedFilter.objects.filter(pk=shared.pk).exists())
        # A superuser is no exception: still only their own tenant.
        self.staff.is_superuser = True
        self.staff.save()
        names = [c["name"] for c in self.client.get(URL).context["saved_filters"]["items"]]
        self.assertNotIn("Foreign", names)

    def test_owner_deletes_and_name_overwrites(self):
        self.save()
        self.save(query="kind=person")
        saved = SavedFilter.objects.get(owner=self.staff)
        self.assertEqual(saved.params, {"kind": "person"})
        self.client.post(URL, {"list_action": "delete_filter", "filter": str(saved.pk), "query": "kind=person"})
        self.assertFalse(SavedFilter.objects.exists())

    def test_cap_per_owner_per_list(self):
        SavedFilter.objects.bulk_create([
            SavedFilter(company=self.company, owner=self.staff, list_id="parties.party", name=f"F{i}",
                        params={"q": str(i)}) for i in range(50)
        ])
        self.save(name="One more")
        self.assertFalse(SavedFilter.objects.filter(name="One more").exists())

    def test_nothing_to_save_and_no_company(self):
        self.save(query="page=2")
        self.assertFalse(SavedFilter.objects.exists())
        nobody = make_staff_user("list-nocompany", password="pw")
        self.client.force_login(nobody)
        self.save()
        self.assertFalse(SavedFilter.objects.exists())

    def test_crm_lists_validate_their_own_params(self):
        url = reverse("erp-crm-engagement-list")
        self.client.post(url, {"list_action": "save_filter", "name": "Open overdue",
                               "query": "chip=overdue&status=open&stage=x&kind=person&sort=value&dir=desc"})
        saved = SavedFilter.objects.get(list_id="crm.engagement")
        self.assertEqual(saved.params, {"chip": "overdue", "status": "open", "stage": "x", "sort": "expected_value",
                                        "dir": "desc"})
        self.assertContains(self.client.get(url), "Open overdue")


# --- CSV export (5.5) -------------------------------------------------------------------------


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class ExportTests(ListTestBase):
    def setUp(self):
        super().setUp()
        self.gold = party(self.company, "Gold Org", **{"custom.tier": "gold", "custom.nickname": "Goldie",
                                                       "custom.internal": "hidden", "hubspot.raw": "r"})
        Party.objects.filter(pk=self.gold.pk).update(primary_email_normalized="ops@gold.example")
        party(self.company, "Silver Org", **{"custom.tier": "silver"})
        party(self.other_company, "Gold Elsewhere", **{"custom.tier": "gold"})

    def test_headers_are_ids_and_rows_follow_the_search(self):
        table = rows(self.client.get(URL, {"export": "csv", "q": "Gold"}))
        self.assertEqual(table[0][:2], ["display_name", "roles"])
        self.assertIn("custom.tier", table[0])
        self.assertEqual([r[0] for r in table[1:]], ["Gold Org"])
        self.assertEqual(table[1][table[0].index("custom.tier")], "gold")  # value, not label

    def test_visible_columns_or_all_fields(self):
        self.client.post(URL, {"list_action": "columns", "columns": ["display_name", "custom.tier"]})
        visible = rows(self.client.get(URL, {"export": "csv"}))
        self.assertEqual(visible[0], ["display_name", "custom.tier"])
        everything = rows(self.client.get(URL, {"export": "csv", "columns": "all"}))[0]
        for col in ("id", "kind", "email", "created_at", "custom.nickname", "custom.vip"):
            self.assertIn(col, everything)
        self.assertNotIn("custom.internal", everything)  # export=False
        self.assertNotIn("hubspot.raw", everything)  # undefined keys never leave as columns

    def test_pii_columns_need_export_pii_on_every_path(self):
        staff_group = Group.objects.get(name=STAFF_ROLE)
        staff_group.permissions.remove(Permission.objects.get(codename="export_pii"))
        self.client.post(URL, {"list_action": "columns",
                               "columns": ["display_name", "email", "phone", "custom.nickname"]})
        for params in ({"export": "csv"}, {"export": "csv", "columns": "all"}):
            with self.subTest(params=params):
                table = rows(self.client.get(URL, params))
                for pii in ("email", "phone", "custom.nickname"):
                    self.assertNotIn(pii, table[0])
                body = "\n".join(",".join(r) for r in table)
                self.assertNotIn("Goldie", body)
                self.assertNotIn("ops@gold.example", body)
        audit = AuditLog.objects.filter(action="export").latest("created_at")
        self.assertEqual(set(audit.changes["withheld"]), {"email", "phone", "custom.nickname"})
        # Granted (the staff default): they come back.
        staff_group.permissions.add(Permission.objects.get(codename="export_pii"))
        table = rows(self.client.get(URL, {"export": "csv", "columns": "all"}))
        self.assertIn("custom.nickname", table[0])
        self.assertIn("Goldie", [r[table[0].index("custom.nickname")] for r in table[1:]])

    def test_every_export_is_audited(self):
        self.client.get(URL, {"export": "csv", "q": "Gold"})
        audit = AuditLog.objects.get(action="export")
        self.assertEqual(audit.model_name, "parties.Party")
        self.assertEqual(audit.changes["list_id"], "parties.party")
        self.assertEqual(audit.changes["rows"], 1)
        self.assertEqual(audit.changes["params"], {"q": "<4 characters>"})  # never the search text
        self.assertEqual(audit.user, UserProfile.objects.get(user=self.staff))

    def test_contact_shaped_names_are_blanked_without_export_pii(self):
        # A person with no name gets their email as display name (parties.mirror).
        nameless = party(self.company, "nobody@example.com")
        phoneish = party(self.company, "+1 (217) 555-0100")
        Group.objects.get(name=STAFF_ROLE).permissions.remove(Permission.objects.get(codename="export_pii"))
        table = rows(self.client.get(URL, {"export": "csv", "columns": "all"}))
        ids = table[0].index("id")
        names = {r[ids]: r[0] for r in table[1:]}
        self.assertEqual(names[str(nameless.pk)], "")
        self.assertEqual(names[str(phoneish.pk)], "")
        self.assertEqual(names[str(self.gold.pk)], "Gold Org")
        audit = AuditLog.objects.filter(action="export").latest("created_at")
        self.assertTrue(audit.changes["contact_values_redacted"])
        Group.objects.get(name=STAFF_ROLE).permissions.add(Permission.objects.get(codename="export_pii"))
        table = rows(self.client.get(URL, {"export": "csv", "columns": "all"}))
        self.assertIn("nobody@example.com", [r[0] for r in table[1:]])

    def test_numbers_keep_their_sign_and_text_is_neutralised(self):
        defs = {"party": [*DEFINITIONS["party"],
                          {"key": "custom.score", "label": "Score", "type": "integer", "list": True}]}
        Party.objects.filter(pk=self.gold.pk).update(
            display_name="-cmd", metadata={"custom.score": -5, "custom.tier": "gold"})
        with override_settings(GOLDEN_FIELD_DEFINITIONS=defs):
            table = rows(self.client.get(URL, {"export": "csv", "columns": "all"}))
        gold = next(r for r in table[1:] if r[table[0].index("id")] == str(self.gold.pk))
        self.assertEqual(gold[table[0].index("custom.score")], "-5")
        self.assertEqual(gold[0], "'-cmd")

    def test_values_are_canonical(self):
        table = rows(self.client.get(URL, {"export": "csv", "columns": "all"}))
        header, gold = table[0], next(r for r in table[1:] if r[0] == "Gold Org")
        self.assertRegex(gold[header.index("created_at")], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
        self.assertEqual(gold[header.index("kind")], "organisation")
        self.assertEqual(gold[header.index("marketable")], "false")
        self.assertEqual(gold[header.index("custom.vip")], "")  # unknown, not "false"

    def test_over_the_cap_is_refused_with_narrow_the_filter(self):
        with mock.patch.object(PartyListView, "export_row_cap", 1):
            response = self.client.get(URL, {"export": "csv", "kind": "organisation"})
        self.assertRedirects(response, URL + "?kind=organisation", fetch_redirect_response=False)
        self.assertContains(self.client.get(response["Location"]), "Narrow the filter")
        self.assertFalse(AuditLog.objects.filter(action="export").exists())

    def test_commerce_lists_keep_label_headers(self):
        response = self.client.get(reverse("erp-customer-list"), {"export": "csv"})
        self.assertFalse(response.streaming)
        self.assertEqual(response.content.decode().split("\r\n")[0].split(",")[0], "Name")


class ExportSizeTests(ListTestBase):
    """The done-when: a 60k-row export is refused and a 40k-row one streams."""

    def bulk(self, n, prefix):
        Party.objects.bulk_create([
            Party(company=self.company, kind="organisation", display_name=f"{prefix} {i:05d}", origin="erp")
            for i in range(n)
        ], batch_size=5000)

    @tag("slow")
    def test_60k_refused_40k_streams(self):
        self.bulk(40_000, "Keep")
        self.bulk(20_000, "Drop")
        refused = self.client.get(URL, {"export": "csv"})
        self.assertEqual(refused.status_code, 302)
        started = time.monotonic()
        response = self.client.get(URL, {"export": "csv", "q": "Keep"})
        self.assertTrue(response.streaming)
        lines = 0
        for chunk in response.streaming_content:
            lines += chunk.count(b"\n")
        elapsed = time.monotonic() - started
        self.assertEqual(lines, 40_001)
        print(f"\nEXPORT_TIMING vendor={connection.vendor} rows=40000 seconds={elapsed:.2f}")  # noqa: T201


# --- the staff default for export_pii ---------------------------------------------------------


class ExportPiiGrantTests(TestCase):
    def test_staff_role_declares_export_pii_unless_revoked(self):
        self.assertIn("parties.export_pii", ROLES[STAFF_ROLE].permissions)
        with override_settings(GOLDEN_STAFF_EXPORT_PII=False):
            self.assertNotIn("parties.export_pii", ROLES[STAFF_ROLE].permissions)
        self.assertIn("parties.export_pii", ROLES[STAFF_ROLE].permissions)

    def test_staff_user_holds_it(self):
        user = make_staff_user("pii-staff")
        self.assertTrue(user.has_perm("parties.export_pii"))


class RequestFactoryTests(TestCase):
    def test_columns_without_a_user(self):
        view = ERPListView()
        view.model = Party
        view.request = RequestFactory().get("/")
        view.list_columns = [{"field": "display_name", "label": "Name"}]
        self.assertEqual([c.id for c in view.get_visible_columns()], ["display_name"])


class CrmExportTests(ListTestBase):
    def test_engagement_export_all_fields_and_defers_related_metadata(self):
        from crm import engagements
        from crm.models import Pipeline

        org = party(self.company, "Deal Org", **{"bulk": "x" * 1000})
        pipeline = Pipeline.objects.get(company=self.company, key="sales")
        engagements.create(company=self.company, party=org, stage=pipeline.stages.get(key="new"))
        url = reverse("erp-crm-engagement-list")
        # Step 6 renders the table from the registry, so the default export is
        # the 7 visible columns; "All fields" carries every exportable one.
        table = rows(self.client.get(url, {"export": "csv", "chip": "open", "columns": "all"}))
        self.assertGreater(len(table[0]), list_columns.MAX_COLUMNS)
        for col in ("source", "last_activity_at", "closed_at", "created_at"):
            self.assertIn(col, table[0])
        response = self.client.get(url, {"chip": "open"})
        eng = response.context["object_list"][0]
        self.assertIn("metadata", eng.get_deferred_fields())
        self.assertIn("metadata", eng.party.get_deferred_fields())
