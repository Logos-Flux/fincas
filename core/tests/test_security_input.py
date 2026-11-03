"""Release hardening 6.3: input handling across the back office.

Re-runs and extends the September S4/S5/S9 set (`docs/testing-plan.md` 8.2).
One minimal regression payload per class, never more than proves escaping:

- stored XSS through every free-text field that renders back to staff
  (task title, engagement title, communication subject and notes,
  relationship title, party and customer names, address lines, saved
  filter names, stage names, label overrides, import cells);
- reflected XSS and ORM injection through every search box and list
  parameter (`?q=`, `?sort=`, `?dir=`, `?page=`, `?saved=`, filters);
- template injection renders literally;
- the JSON search endpoints that feed `innerHTML` widgets, and the template
  that consumes them;
- `?next=` handling (open redirect, header CRLF), upload caps.

Already covered elsewhere and not repeated: product title / description /
reflected search (`products/test_m3_xss.py`), CMS article title and the
staff-only raw content surface (`content/test_m3_xss.py`), address display
HTML and the `<script>` breakout in the document form (`orders/test_m3_xss.py`,
F-043), customer AJAX details (`customers/test_m3_xss.py`), API filter and
ordering SQLi (`core/tests/test_m3_sqli.py`), sort allow-lists
(`core/tests/test_list_sorting.py`), document upload caps
(`documents/tests/test_upload_caps.py`), accounting upload extension gate
and escaped report cells (`accounting/test_m3_upload.py`).
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from unittest import expectedFailure

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import get_resolver, reverse
from django.urls.resolvers import RoutePattern, URLPattern, URLResolver
from django.utils import timezone

from communications.ingest import record as record_communication
from core.http import safe_next
from core.middleware import StaffOnlyERPMiddleware
from core.models import SavedFilter, UserProfile
from core.tests.helpers import make_staff_user
from core.views.list_view import ERPListView
from crm import engagements, tasks
from crm.models import Engagement, Stage
from crm.tests.helpers import CrmTestCase, organisation, person
from customers.models import Customer, CustomerAddress
from orders.models import Quote
from parties.models import PartyRelationship
from unify.erp_views import UPLOAD_LIMIT_BYTES

SCRIPT = "<script>alert(1)</script>"
SCRIPT_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
IMG = '"><img src=x onerror=alert(1)>'
IMG_RAW = "<img src=x onerror=alert(1)>"
TEMPLATE = "{{7*7}}"
SQLI = "' OR 1=1 --"
CRLF = "/erp/\r\nSet-Cookie: pwned=1"


def _argless_erp_routes():
    """``[(path, view class or None)]`` for every `/erp/` route with no URL
    argument, walking the resolver so plugin and app includes are covered."""
    found = []

    def walk(patterns, prefix):
        for entry in patterns:
            if not isinstance(entry.pattern, RoutePattern):
                continue
            route = prefix + str(entry.pattern)
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, route)
            elif isinstance(entry, URLPattern) and "<" not in route:
                found.append(("/" + route, getattr(entry.callback, "view_class", None)))

    walk(get_resolver().url_patterns, "")
    return [(p, v) for p, v in found if p.startswith("/erp/")
            and not p.startswith(StaffOnlyERPMiddleware.EXEMPT_PREFIXES)]


class _Staff(CrmTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = make_staff_user("sec-input", password="pw")
        cls.profile = UserProfile.objects.create(user=cls.user, company=cls.company,
                                                 display_name="Sec")

    def setUp(self):
        self.client.force_login(self.user)

    def assert_escaped(self, response, *, raw=SCRIPT, escaped=SCRIPT_ESCAPED, expect_present=True):
        self.assertEqual(response.status_code, 200, response.get("Location", ""))
        content = response.content.decode()
        self.assertNotIn(raw, content)
        if expect_present:
            self.assertIn(escaped, content)
        return content


class StoredXssCrmTests(_Staff):
    """Payloads stored through the CRM services come back escaped on every
    staff screen that shows them."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eng = engagements.create(company=cls.company, party=cls.ada, title=SCRIPT)
        cls.task = tasks.create(company=cls.company, title=SCRIPT, engagement=cls.eng,
                                assignee=cls.profile, due=timezone.localdate())
        cls.tpl_task = tasks.create(company=cls.company, title=TEMPLATE, engagement=cls.eng)
        cls.comm = record_communication(
            cls.company, "note", direction="internal", occurred_at=timezone.now(),
            subject=IMG, body_text=SCRIPT, engagement=cls.eng, party=cls.ada,
        ).communication

    def test_task_title_on_task_list_and_my_day(self):
        for name in ("erp-crm-task-list", "erp-crm-my-day"):
            with self.subTest(route=name):
                self.assert_escaped(self.client.get(reverse(name)))

    def test_engagement_title_on_list_detail_and_timeline(self):
        self.assert_escaped(self.client.get(reverse("erp-crm-engagement-list")))
        self.assert_escaped(self.client.get(reverse("erp-crm-engagement-detail", args=[self.eng.pk])))
        self.assert_escaped(self.client.get(reverse("erp-crm-engagement-timeline", args=[self.eng.pk])))

    def test_communication_subject_and_notes_on_detail_and_party(self):
        for url in (reverse("erp-crm-engagement-detail", args=[self.eng.pk]),
                    reverse("erp-party-detail", args=[self.ada.pk])):
            with self.subTest(url=url):
                content = self.assert_escaped(self.client.get(url))
                self.assertNotIn(IMG_RAW, content)
                self.assertIn("&lt;img src=x onerror=alert(1)&gt;", content)

    def test_template_syntax_renders_literally(self):
        content = self.client.get(reverse("erp-crm-task-list")).content.decode()
        self.assertIn(TEMPLATE, content)
        self.assertNotIn(">49<", content)

    def test_log_form_echoes_the_engagement_title_escaped(self):
        self.assert_escaped(self.client.get(reverse("erp-crm-engagement-log", args=[self.eng.pk])))


class StoredXssPartyTests(_Staff):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.villain = person(cls.company, given=SCRIPT, family="Smith")
        cls.org = organisation(cls.company, name=IMG)
        cls.rel = PartyRelationship.objects.create(
            company=cls.company, from_party=cls.villain, to_party=cls.org,
            kind="employment", is_symmetric=False, title=SCRIPT,
        )

    def test_party_names_on_list_and_detail(self):
        content = self.assert_escaped(self.client.get(reverse("erp-party-list")))
        self.assertNotIn(IMG_RAW, content)
        self.assert_escaped(self.client.get(reverse("erp-party-detail", args=[self.villain.pk])))

    def test_relationship_title_on_both_party_pages(self):
        for party in (self.villain, self.org):
            with self.subTest(party=party.kind):
                content = self.assert_escaped(
                    self.client.get(reverse("erp-party-detail", args=[party.pk])))
                self.assertNotIn(IMG_RAW, content)

    def test_party_search_is_json_with_the_name_as_data(self):
        response = self.client.get(reverse("erp-party-search"), {"q": "alert"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        names = [r["name"] for r in response.json()["results"]]
        self.assertIn(SCRIPT + " Smith", names)  # data, not markup: the widget escapes it

    def test_relationship_form_echoes_names_escaped(self):
        url = reverse("erp-party-relationship-create", args=[self.villain.pk])
        self.assert_escaped(self.client.get(url))
        edit = reverse("erp-party-relationship-edit", args=[self.rel.pk])
        self.assert_escaped(self.client.get(edit))


class StoredXssCustomerTests(_Staff):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.customer = Customer.objects.create(
            first_name=SCRIPT, last_name="Doe", email="xss-customer@example.com",
            company=cls.company, company_name=IMG,
        )
        cls.address = CustomerAddress.objects.create(
            customer=cls.customer, company=cls.company, address_line_1=SCRIPT,
            city=IMG, state_province="NY", postal_code="10001", country="US",
        )

    def test_customer_fields_on_list_and_detail(self):
        content = self.assert_escaped(self.client.get(reverse("erp-customer-list")))
        self.assertNotIn(IMG_RAW, content)
        content = self.assert_escaped(
            self.client.get(reverse("erp-customer-detail", args=[self.customer.pk])))
        self.assertNotIn(IMG_RAW, content)

    def test_address_ajax_detail_is_json_data_not_markup(self):
        # The editor fills form inputs from this (``.value``), and the display
        # fragment is built server-side by ``core.html_safety.display_html``
        # (F-043, `orders/test_m3_xss.py`); the JSON itself carries data.
        response = self.client.get(reverse("erp-address-detail-ajax", args=[self.address.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["address_line_1"], SCRIPT)


class StoredXssListInfraTests(_Staff):
    def test_saved_filter_name_is_escaped_in_the_chip(self):
        url = reverse("erp-party-list")
        response = self.client.post(url, {"list_action": "save_filter", "name": SCRIPT, "query": "q=ada"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SavedFilter.objects.filter(owner=self.user, name=SCRIPT).exists())
        self.assert_escaped(self.client.get(url))

    @override_settings(GOLDEN_LABEL_OVERRIDES={"crm.engagement": {"singular": SCRIPT, "plural": SCRIPT}})
    def test_label_override_is_escaped_on_a_real_page(self):
        self.assert_escaped(self.client.get(reverse("erp-crm-engagement-list")))


class StoredXssStageReportTests(_Staff):
    """A stage name reaches the report page inside a ``<script>`` block
    (`erp/report.html` embeds ``chart_data`` with ``|safe``), so it must be
    serialised script-safe (`core.html_safety.dumps_script_safe`), never with
    plain ``json.dumps``."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.stage = Stage.objects.create(pipeline=cls.pipeline, company=cls.company, key="xss",
                                         name="</script><script>alert(1)</script>", sort_order=42)
        customer = Customer.objects.create(first_name="Q", last_name="R", email="q@example.com",
                                           company=cls.company)
        quote = Quote.objects.create(customer=customer, company=cls.company, status=Quote.Status.SENT,
                                     shipping_cost_ex_tax=Decimal("0"))
        Engagement.objects.filter(pk=quote.engagement_id).update(stage=cls.stage)

    def test_quote_conversion_report_has_no_script_breakout(self):
        response = self.client.get(reverse("erp-report-list"), {"report": "quote_conversion"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("</script><script>alert(1)</script>", content)
        self.assertIn("\\u003c/script\\u003e", content)

    def test_crm_stage_report_has_no_script_breakout(self):
        response = self.client.get("/erp/reports/crm/stages/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("</script><script>alert(1)</script>", response.content.decode())


class StoredXssImportTests(_Staff):
    def test_unify_cell_values_render_escaped_on_the_batch_page(self):
        csv = f'sku,title,retail_price\nQ-1,"{SCRIPT}",9.99\n'.encode()
        response = self.client.post(reverse("erp-unify-batch-upload"), {
            "source_name": IMG,
            "csv_file": SimpleUploadedFile("q.csv", csv, content_type="text/csv"),
        })
        self.assertEqual(response.status_code, 302)
        content = self.assert_escaped(self.client.get(reverse("erp-unify-batch-list")), raw=IMG_RAW,
                                      escaped="&lt;img src=x onerror=alert(1)&gt;")
        self.assertNotIn(SCRIPT, content)
        self.assert_escaped(self.client.get(response["Location"]))

    def test_crm_import_queue_renders_source_cells_escaped(self):
        from django.contrib.auth.models import Permission

        from crm_import import stage as staging
        from crm_import.tests.staging_helpers import ExportDir, contact, make_person
        from unify.models import StagedRow

        for code in ("apply_import", "view_import_pii"):
            self.user.user_permissions.add(
                Permission.objects.get(content_type__app_label="crm_import", codename=code))
        make_person(self.company, given="Pat", family="Example", emails=["pat@example.com"])
        export = ExportDir(self, {"contact": [
            contact("c0", given=SCRIPT, family=IMG, emails=["pat@example.com"], job_title=TEMPLATE),
        ]})
        job = staging.stage(self.company, export.path)
        from crm_import.models import ImportJobBatch

        batch = ImportJobBatch._base_manager.get(job=job, object_type="contact").batch
        StagedRow.objects.filter(batch=batch).update(
            mapping_state=StagedRow.MappingState.PROPOSED, mapped_values={})
        response = self.client.get(reverse("erp-crm-import-queue", args=[job.pk, "contact"]))
        content = self.assert_escaped(response)
        self.assertNotIn(IMG_RAW, content)
        self.assertIn(TEMPLATE, content)


class ReflectedParamsSweepTests(_Staff):
    """Every argument-free `/erp/` route takes a hostile query string without
    a 500 and without echoing the payload raw; every `ERPListView` also takes
    hostile sort, direction, page, saved-filter and filter values."""

    HOSTILE = {"q": SCRIPT, "search": IMG, "sort": SQLI, "dir": IMG, "page": SCRIPT,
               "columns": SCRIPT, "saved": SQLI, "status": SQLI, "next": "https://evil.example/",
               "report": SCRIPT, "period": SQLI, "date_from": SCRIPT, "export": SCRIPT}

    def test_every_argless_route_survives_hostile_params(self):
        routes = _argless_erp_routes()
        self.assertGreater(len(routes), 40, routes)
        failures = []
        for path, view_class in routes:
            response = self.client.get(path, self.HOSTILE)
            if response.status_code >= 500:
                failures.append((path, response.status_code))
                continue
            if response.status_code != 200:
                continue
            body = response.content.decode(errors="replace")
            if SCRIPT in body or IMG_RAW in body:
                failures.append((path, "raw payload echoed"))
        self.assertEqual(failures, [])

    def test_every_list_view_ignores_hostile_sort_and_filters(self):
        lists = [(p, v) for p, v in _argless_erp_routes() if v and issubclass(v, ERPListView)]
        self.assertGreater(len(lists), 10, lists)
        failures = []
        for path, view_class in lists:
            filters = {f["field"]: SQLI for f in getattr(view_class, "list_filters", [])
                       if isinstance(f, dict) and "field" in f}
            params = {**self.HOSTILE, **filters, "sort": "id); DROP TABLE x; --", "dir": "desc"}
            response = self.client.get(path, params)
            if response.status_code >= 500:
                failures.append((path, response.status_code))
            elif response.status_code == 200 and "<script>" in response.content.decode(errors="replace"):
                failures.append((path, "raw <script> echoed"))
        self.assertEqual(failures, [])


class SafeNextTests(SimpleTestCase):
    """`core.http.safe_next`, the one check behind every back-office ``?next=``:
    off-site, protocol-relative, backslash and header-injecting values fall
    back to the default (`parties/tests/test_relationship_picker.py` and
    `core/tests/test_oidc.py` cover the two views with their own defaults)."""

    def _next(self, value, *, secure=False):
        request = RequestFactory().get("/erp/", {"next": value}, secure=secure,
                                       HTTP_HOST="testserver")
        return safe_next(request, "/erp/default/")

    def test_same_host_paths_are_kept(self):
        self.assertEqual(self._next("/erp/orders/?page=2"), "/erp/orders/?page=2")

    def test_offsite_and_malformed_targets_fall_back(self):
        for hostile in ("https://evil.example/", "//evil.example/", "/\\evil.example",
                        "http://testserver@evil.example/", "javascript:alert(1)",
                        "\\\\evil.example"):
            with self.subTest(next=hostile):
                self.assertEqual(self._next(hostile), "/erp/default/")

    def test_crlf_in_next_never_reaches_a_header(self):
        # Same-host, so it is kept; the redirect encodes the control
        # characters (``%0D%0A``) instead of ending the Location header.
        from django.http import HttpResponseRedirect

        location = HttpResponseRedirect(self._next(CRLF))["Location"]
        self.assertNotIn("\r", location)
        self.assertNotIn("\n", location)
        self.assertTrue(location.startswith("/erp/"), location)

    def test_https_request_refuses_a_downgrade(self):
        self.assertEqual(self._next("http://testserver/erp/", secure=True), "/erp/default/")

    def test_post_wins_over_query(self):
        request = RequestFactory().post("/erp/?next=/erp/from-query/", {"next": "/erp/from-post/"},
                                        HTTP_HOST="testserver")
        self.assertEqual(safe_next(request, "/x/"), "/erp/from-post/")


class LoginNextTests(TestCase):
    def test_login_never_redirects_off_site_or_injects_headers(self):
        make_staff_user("next-user", password="pw-next-1")
        for hostile in ("https://evil.example/", "//evil.example/", CRLF):
            with self.subTest(next=hostile):
                response = self.client.post(reverse("erp-login") + "?next=" + hostile,
                                            {"username": "next-user", "password": "pw-next-1"})
                self.assertEqual(response.status_code, 302)
                location = response["Location"]
                self.assertFalse(location.startswith(("http", "//")), location)
                # Django percent-encodes CR/LF in Location, so a crafted
                # ``next`` can never start a second header.
                self.assertNotIn("\r", location)
                self.assertNotIn("\n", location)
                self.client.logout()


class UploadCapTests(TestCase):
    def setUp(self):
        from core.models import Company

        self.user = make_staff_user("upl", password="pw")
        self.company = Company.objects.create(name="Upload Co")
        UserProfile.objects.create(user=self.user, company=self.company)
        self.client.force_login(self.user)

    def test_unify_upload_over_the_cap_is_refused_with_a_field_error(self):
        big = b"sku,title\n" + b"X,y\n" * (UPLOAD_LIMIT_BYTES // 4 + 1)
        self.assertGreater(len(big), UPLOAD_LIMIT_BYTES)
        response = self.client.post(reverse("erp-unify-batch-upload"), {
            "source_name": "big", "csv_file": SimpleUploadedFile("big.csv", big, content_type="text/csv"),
        }, follow=True)
        self.assertContains(response, "slice cap")
        from unify.models import ImportBatch

        self.assertFalse(ImportBatch.objects.exists())

    def test_unify_upload_extension_allow_list(self):
        response = self.client.post(reverse("erp-unify-batch-upload"), {
            "source_name": "x", "csv_file": SimpleUploadedFile("x.exe", b"MZ", content_type="text/csv"),
        }, follow=True)
        self.assertContains(response, "Only .csv")

    @expectedFailure
    def test_accounting_import_refuses_an_oversized_file(self):
        """F-048 (open): `AccountingImportForm` has no upload size cap. The
        characterisation in `accounting/test_m3_upload.py` pins today's
        behaviour; this is the expectation once the cap lands (documents use
        `DOCUMENT_MAX_UPLOAD_BYTES`, 25 MB)."""
        from accounting.forms import AccountingImportForm

        cap = getattr(settings, "DOCUMENT_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
        big = SimpleUploadedFile("big.csv", b"x" * (cap + 1), content_type="text/csv")
        form = AccountingImportForm(data={"company": str(self.company.pk)}, files={"chart_of_accounts": big})
        self.assertFalse(form.is_valid())
        self.assertIn("chart_of_accounts", form.errors)

    @expectedFailure
    def test_accounting_import_refuses_macro_capable_workbooks(self):
        """F-048 (open): `.xlsm` passes the extension gate. The reader never
        runs macros, but the allow-list should not advertise a macro format."""
        from accounting.forms import AccountingImportForm

        self.assertNotIn(".xlsm", AccountingImportForm.ALLOWED_EXTENSIONS)


class DocumentFormInnerHtmlTests(SimpleTestCase):
    """`erp/document_form.html` builds search dropdowns with ``innerHTML``
    from JSON endpoints whose values are user-controlled (variant SKU and
    name, storefront product title, supplier code and name). Every such
    value must go through the template's ``escHtml()``; F-043 fixed the
    address and label widgets the same way."""

    TEMPLATE = Path(settings.BASE_DIR) / "golden_erp" / "templates" / "erp" / "document_form.html"

    def test_search_result_fields_are_escaped_before_innerhtml(self):
        source = self.TEMPLATE.read_text()
        self.assertIn("function escHtml(", source)
        unescaped = []
        for statement in re.finditer(r"innerHTML\s*=([^;]*);", source):
            unescaped += re.findall(
                r"(?<!escHtml\()\b((?:item|s)\.(?:sku|name|title|label|code|company|retail_price))\b",
                statement.group(1))
        self.assertEqual(unescaped, [], "unescaped JSON fields reach innerHTML")

    def test_no_view_serves_a_request_named_file(self):
        """Path traversal: no back-office view opens a filesystem path taken
        from the request (PDFs are rendered, CSVs streamed, uploads parsed in
        memory). Keep it that way: `FileResponse`/`open(` on request data
        needs its own allow-list test."""
        offenders = []
        for app in ("core", "orders", "payments", "documents", "reporting", "accounting",
                    "crm", "crm_import", "unify", "parties", "products", "content"):
            for path in (Path(settings.BASE_DIR) / app).rglob("*view*.py"):
                if "tests" in path.parts:
                    continue
                text = path.read_text()
                if "FileResponse(" in text or re.search(r"\bopen\(\s*(request|self\.request)", text):
                    offenders.append(str(path.relative_to(settings.BASE_DIR)))
        self.assertEqual(offenders, [])
