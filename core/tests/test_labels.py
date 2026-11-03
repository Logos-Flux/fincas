"""Label layer (CRM Phase 3 §3): registry, resolution order, tags, views."""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ImproperlyConfigured
from django.template import Context, Template, TemplateSyntaxError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core import label_registry, nav_registry
from core.context_processors import erp_nav
from core.context_processors import labels as labels_processor
from core.label_registry import GOLDEN_LABELS, Label
from core.labels import UnknownLabel, get_label, label
from core.models import Company, UserProfile

TENANT = "6f1c2c0e-8d0e-4f7b-9a43-3f8f1f0a0001"
IDEA = {"singular": "Idea", "plural": "Ideas", "article": "an"}


class RegistryTests(SimpleTestCase):
    def test_builtins_cover_the_spec_keys(self):
        for key in (
            "crm.engagement", "crm.pipeline", "crm.stage", "crm.task", "crm.lead",
            "crm.touchpoint", "communications.communication", "parties.party",
            "parties.organisation", "parties.person", "customers.customer",
            "orders.quote", "orders.order", "orders.sales_invoice",
        ):
            self.assertIn(key, label_registry.DEFAULTS, key)

    def test_builtins_are_immutable(self):
        with self.assertRaises(TypeError):
            GOLDEN_LABELS["crm.engagement"] = Label("X", "Xs")
        with self.assertRaises(AttributeError):
            GOLDEN_LABELS["crm.engagement"].singular = "X"

    def test_party_roles_contribute_keys(self):
        self.assertEqual(get_label("party_role.supplier").singular, "Supplier")
        with override_settings(GOLDEN_PARTY_ROLES=[{"name": "installer", "label": "Installer"}]):
            self.assertEqual(label("party_role.installer", article=True), "an Installer")
        self.assertNotIn("party_role.installer", label_registry.DEFAULTS)

    def _bad(self, overrides, setting="GOLDEN_LABEL_OVERRIDES"):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            with override_settings(**{setting: overrides}):
                pass
        return str(ctx.exception)

    def test_unknown_key_raises(self):
        self.assertIn("crm.engagment", self._bad({"crm.engagment": IDEA}))

    def test_missing_singular_raises(self):
        self.assertIn("singular", self._bad({"crm.engagement": {"plural": "Ideas"}}))

    def test_non_string_raises(self):
        self._bad({"crm.engagement": {"singular": 3}})
        self._bad({"crm.engagement": {"singular": "Idea", "plural": None}})

    def test_empty_singular_raises(self):
        self._bad({"crm.engagement": {"singular": "  "}})

    def test_unknown_field_raises(self):
        self.assertIn("plurals", self._bad({"crm.engagement": {"singular": "Idea", "plurals": "x"}}))

    def test_bad_article_raises(self):
        self._bad({"crm.engagement": {"singular": "Idea", "article": "the"}})

    def test_not_a_dict_raises(self):
        self._bad([("crm.engagement", IDEA)])
        self._bad({"crm.engagement": "Idea"})

    def test_tenant_key_must_be_a_uuid(self):
        self._bad({"acme": {"crm.engagement": IDEA}}, "GOLDEN_LABEL_TENANT_OVERRIDES")

    def test_tenant_unknown_label_key_raises(self):
        self._bad({TENANT: {"crm.nope": IDEA}}, "GOLDEN_LABEL_TENANT_OVERRIDES")

    def test_tenant_duplicate_company_raises(self):
        self._bad({TENANT: {}, TENANT.upper(): {}}, "GOLDEN_LABEL_TENANT_OVERRIDES")

    def test_plural_and_article_default(self):
        with override_settings(GOLDEN_LABEL_OVERRIDES={"orders.quote": {"singular": "Estimate"}}):
            resolved = get_label("orders.quote")
        self.assertEqual(resolved, Label("Estimate", "Estimates", "an"))


# ResolutionTests and NavTests are TestCases, not SimpleTestCases: the tenant
# layer asks ``label_registry.stored_any`` (cached), and a cache cleared by an
# earlier test (``cache.clear()`` below) sends it to the database. Found when
# the module ran after other suites (2026-10-18), not alone.
class ResolutionTests(TestCase):
    def test_golden_default(self):
        self.assertEqual(label("crm.engagement"), "Engagement")
        self.assertEqual(label("crm.engagement", True), "Engagements")
        self.assertEqual(label("crm.engagement", article=True), "an Engagement")

    def test_unknown_key(self):
        with self.assertRaises(UnknownLabel):
            label("crm.nope")

    @override_settings(
        GOLDEN_LABEL_OVERRIDES={"crm.engagement": IDEA, "orders.quote": {"singular": "Proposal"}},
        GOLDEN_LABEL_TENANT_OVERRIDES={TENANT: {"crm.engagement": {"singular": "Deal"}}},
    )
    def test_tenant_then_deployment_then_default(self):
        # tenant layer wins for its key
        self.assertEqual(label("crm.engagement", company=TENANT), "Deal")
        self.assertEqual(label("crm.engagement", company=uuid.UUID(TENANT), plural=True), "Deals")
        # tenant has no override for quote -> deployment
        self.assertEqual(label("orders.quote", company=TENANT), "Proposal")
        # neither layer -> golden default
        self.assertEqual(label("crm.stage", company=TENANT), "Stage")
        # no company / another company -> deployment
        self.assertEqual(label("crm.engagement"), "Idea")
        self.assertEqual(label("crm.engagement", company=str(uuid.uuid4())), "Idea")
        self.assertEqual(label("crm.engagement", article=True, case="lower"), "an idea")

    def test_case(self):
        self.assertEqual(label("orders.sales_invoice", plural=True, case="lower"), "invoices")
        self.assertEqual(label("parties.organisation", case="title"), "Organization")
        with self.assertRaises(ValueError):
            label("crm.stage", case="upper")

    @override_settings(GOLDEN_LABEL_OVERRIDES={"crm.engagement": IDEA})
    def test_setting_changed_rebuilds(self):
        self.assertEqual(label("crm.engagement"), "Idea")

    def test_override_removed_reverts(self):
        self.assertEqual(label("crm.engagement"), "Engagement")


def _render(source, context=None):
    return Template("{% load labels %}" + source).render(Context(context or {}))


class TemplateTests(TestCase):
    def test_tag_forms(self):
        self.assertEqual(_render('{% label "crm.engagement" %}'), "Engagement")
        self.assertEqual(_render('{% label "crm.engagement" plural=True %}'), "Engagements")
        self.assertEqual(_render('{% label "crm.engagement" case="lower" %}'), "engagement")
        self.assertEqual(_render('{% label "crm.engagement" article=True %}'), "an Engagement")
        self.assertEqual(_render('{% label "orders.quote" plural=True as q %}[{{ q }}]'), "[Quotes]")
        self.assertEqual(_render("{% label key %}", {"key": "crm.stage"}), "Stage")

    def test_filter(self):
        self.assertEqual(_render('{{ "crm.stage"|label }}'), "Stage")
        self.assertEqual(_render('{{ "crm.stage"|label:"plural" }}'), "Stages")

    @override_settings(GOLDEN_LABEL_OVERRIDES={"orders.quote": {"singular": "<b>Q</b>"}})
    def test_tag_output_is_escaped(self):
        self.assertEqual(_render('{% label "orders.quote" %}'), "&lt;b&gt;Q&lt;/b&gt;")
        self.assertEqual(_render('{% label "orders.quote" as q %}{{ q }}'), "&lt;b&gt;Q&lt;/b&gt;")
        self.assertEqual(_render('{{ "orders.quote"|label }}'), "&lt;b&gt;Q&lt;/b&gt;")
        self.assertEqual(
            _render('{% autoescape off %}{% label "orders.quote" %}{% endautoescape %}'), "<b>Q</b>"
        )

    def test_as_without_key_fails_at_compile(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{% load labels %}{% label as foo %}")

    def test_unknown_literal_key_fails_at_compile(self):
        with self.assertRaises(TemplateSyntaxError):
            Template('{% load labels %}{% label "crm.nope" %}')

    def test_unknown_option_fails_at_compile(self):
        with self.assertRaises(TemplateSyntaxError):
            Template('{% load labels %}{% label "crm.stage" plurl=True %}')
        with self.assertRaises(TemplateSyntaxError):
            Template('{% load labels %}{% label "crm.stage" True %}')

    @override_settings(GOLDEN_LABEL_OVERRIDES={"crm.engagement": IDEA})
    def test_tag_and_filter_follow_deployment(self):
        self.assertEqual(_render('{% label "crm.engagement" article=True %}'), "an Idea")
        self.assertEqual(_render('{{ "crm.engagement"|label:"plural" }}'), "Ideas")

    def test_tag_uses_request_company(self):
        company = Company.objects.create(name="Tenant Co")
        user = User.objects.create_user("tenant-user")
        UserProfile.objects.create(user=user, company=company)
        request = RequestFactory().get("/erp/")
        request.user = user
        overrides = {str(company.pk): {"crm.engagement": IDEA}}
        with override_settings(GOLDEN_LABEL_TENANT_OVERRIDES=overrides):
            self.assertEqual(
                _render('{% label "crm.engagement" plural=True %}', {"request": request}), "Ideas"
            )
            ctx = labels_processor(request)
            self.assertEqual(
                _render("{{ labels.crm.engagement }}/{{ labels.crm.engagement.plural }}", ctx),
                "Idea/Ideas",
            )
            self.assertEqual(_render("{{ labels.party_role.supplier }}", ctx), "Supplier")
            self.assertEqual(_render("[{{ labels.crm.nope }}][{{ labels.crm }}]", ctx), "[][]")

    def test_superuser_without_profile_sees_deployment_labels(self):
        admin = User.objects.create_superuser("root", "r@example.com", "pw")
        request = RequestFactory().get("/erp/")
        request.user = admin
        with override_settings(
            GOLDEN_LABEL_OVERRIDES={"crm.engagement": IDEA},
            GOLDEN_LABEL_TENANT_OVERRIDES={TENANT: {"crm.engagement": {"singular": "Deal"}}},
        ):
            self.assertEqual(_render('{% label "crm.engagement" %}', {"request": request}), "Idea")

    def test_no_tenant_layer_costs_no_query(self):
        user = User.objects.create_user("plain")
        request = RequestFactory().get("/erp/")
        request.user = User.objects.get(pk=user.pk)  # fresh instance, nothing cached
        # The stored tenant layer (core.LabelOverride) is one cached EXISTS
        # per process, never a query per request: warm it, then nothing.
        from django.core.cache import cache

        from core import label_registry

        cache.clear()
        with self.assertNumQueries(1):
            self.assertFalse(label_registry.stored_any())
        with self.assertNumQueries(0):
            self.assertFalse(label_registry.stored_any())
        with self.assertNumQueries(0):
            ctx = labels_processor(request)
            _render('{% label "crm.engagement" %}{{ labels.crm.stage }}', {**ctx, "request": request})
            erp_nav(request)

    def test_tenant_layer_costs_at_most_one_query(self):
        company = Company.objects.create(name="Q Co")
        user = User.objects.create_user("q-user")
        UserProfile.objects.create(user=user, company=company)
        request = RequestFactory().get("/erp/")
        request.user = User.objects.get(pk=user.pk)
        overrides = {str(company.pk): {"orders.quote": {"singular": "Estimate"}}}
        with override_settings(GOLDEN_LABEL_TENANT_OVERRIDES=overrides):
            with self.assertNumQueries(1):
                ctx = labels_processor(request)
                out = _render(
                    '{% label "orders.quote" %}{{ labels.orders.quote.plural }}{% label "crm.stage" %}',
                    {**ctx, "request": request},
                )
                nav = erp_nav(request)["erp_nav"]
        self.assertEqual(out, "EstimateEstimatesStage")
        self.assertIn("Estimates", [i["label"] for i in nav])

    def test_superuser_with_profile_sees_its_company(self):
        company = Company.objects.create(name="Root Co")
        admin = User.objects.create_superuser("root2", "r2@example.com", "pw")
        UserProfile.objects.create(user=admin, company=company)
        request = RequestFactory().get("/erp/")
        request.user = admin
        overrides = {str(company.pk): {"crm.engagement": IDEA}}
        with override_settings(GOLDEN_LABEL_TENANT_OVERRIDES=overrides):
            self.assertEqual(_render('{% label "crm.engagement" %}', {"request": request}), "Idea")

    def test_anonymous_request(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        with override_settings(GOLDEN_LABEL_TENANT_OVERRIDES={TENANT: {}}):
            self.assertEqual(_render('{% label "crm.stage" %}', {"request": request}), "Stage")


NAV = [
    {"type": "link", "section": "quotes", "url_name": "erp-quote-list",
     "icon": "ti ti-file-text", "label": "Quotes", "label_key": "orders.quote"},
]


class NavTests(TestCase):
    def test_unknown_label_key_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_NAV=[{**NAV[0], "label_key": "orders.quotes"}]):
                pass

    def test_sidebar_uses_resolved_plural(self):
        request = RequestFactory().get("/erp/")
        request.user = AnonymousUser()
        with override_settings(ERP_NAV=NAV):
            self.assertEqual(erp_nav(request)["erp_nav"][0]["label"], "Quotes")
            with override_settings(GOLDEN_LABEL_OVERRIDES={"orders.quote": {"singular": "Proposal"}}):
                request = RequestFactory().get("/erp/")
                request.user = AnonymousUser()
                self.assertEqual(erp_nav(request)["erp_nav"][0]["label"], "Proposals")
        self.assertTrue(nav_registry.NAV)


class ViewLabelTests(TestCase):
    """The representative /erp/ pages pick up the label layer."""

    def setUp(self):
        from customers.models import Customer
        from orders.models import Quote

        self.company = Company.objects.create(name="Label Co")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com", company=self.company
        )
        self.quote = Quote.objects.create(customer=self.customer, company=self.company)
        user = User.objects.create_user("label-staff", password="pw", is_staff=True)
        UserProfile.objects.create(user=user, company=self.company)
        self.client.force_login(user)

    def _get(self, name, *args):
        response = self.client.get(reverse(name, args=args))
        self.assertEqual(response.status_code, 200, name)
        return response

    def test_defaults_render_as_before(self):
        response = self._get("erp-quote-list")
        self.assertEqual(response.context["page_title"], "Quotes")
        self.assertContains(response, "New Quote")
        self.assertEqual(self._get("erp-customer-list").context["page_title"], "Customers")
        self.assertEqual(self._get("erp-quote-edit", self.quote.pk).context["page_title"], "Edit Quote")

    def test_overrides_reach_titles_nav_and_detail(self):
        overrides = {
            "orders.quote": {"singular": "Proposal"},
            "customers.customer": {"singular": "Client"},
        }
        tenant = {str(self.company.pk): {"orders.quote": {"singular": "Estimate"}}}
        with override_settings(GOLDEN_LABEL_OVERRIDES=overrides, GOLDEN_LABEL_TENANT_OVERRIDES=tenant):
            response = self._get("erp-quote-list")
            self.assertEqual(response.context["page_title"], "Estimates")
            self.assertContains(response, "New Estimate")
            self.assertContains(response, "Search estimates")
            nav = {i["url_name"]: i["label"] for i in response.context["erp_nav"]}
            self.assertEqual(nav["erp-quote-list"], "Estimates")
            self.assertEqual(nav["erp-customer-list"], "Clients")
            # entity_name stays canonical for machine-facing uses (chat context, CSV)
            self.assertEqual(response.context["entity_name"], "Quote")
            self.assertContains(response, '"entity": "Quote"')

            self.assertEqual(self._get("erp-customer-list").context["page_title"], "Clients")
            self.assertEqual(self._get("erp-customer-create").context["page_title"], "New Client")
            self.assertEqual(self._get("erp-quote-create").context["page_title"], "New Estimate")
            self.assertEqual(
                self._get("erp-quote-edit", self.quote.pk).context["page_title"], "Edit Estimate"
            )
            detail = self._get("erp-customer-detail", self.customer.pk)
            self.assertContains(detail, "— Client — Fincas</title>")
            self.assertContains(detail, "New Estimate")
            self.assertContains(detail, "Recent Estimates")
