"""Custom actions in a service account's contract.

An ``@action`` is refused for every service account unless its viewset names
the model permission that describes it (``service_action_perms``,
docs/identity-unification.md 2.6 "Custom actions").
"""

from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core import access_matrix
from core.models import Company, UserProfile
from core.service_accounts import action_permissions, ensure_service_account, issue_api_key
from core.tests import access_matrix_expectations as exp
from customers.models import Customer
from orders.models import Order, OrderLineItem, Quote, SalesInvoice, SubOrder
from purchasing.models import PurchaseOrder, Supplier

ROLES = [
    # May do everything to an order that the listed actions cover.
    {"name": "order-desk", "label": "Order desk", "kind": "service",
     "permissions": ["orders.view_order", "orders.change_order", "orders.add_order",
                     "orders.add_salesinvoice", "orders.view_salesinvoice",
                     "payments.change_payment", "orders.view_quote",
                     "orders.change_suborder", "purchasing.view_purchaseorder",
                     "purchasing.change_purchaseorder"]},
    {"name": "order-reader", "label": "Order reader", "kind": "service",
     "permissions": ["orders.view_order", "orders.view_quote"]},
    # One permission short of each multi-permission action.
    {"name": "partial", "label": "Partial", "kind": "service",
     "permissions": ["orders.add_order", "orders.view_order", "orders.add_salesinvoice",
                     "orders.view_salesinvoice", "orders.change_quote", "orders.view_quote",
                     "orders.change_suborder"]},
]


@override_settings(API_KEY="", GOLDEN_ROLES=ROLES)
class ServiceActionContractTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada-contract@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company, status=Order.Status.PENDING,
        )
        OrderLineItem.objects.create(
            order=self.order, item_name="Widget", sku="W-1",
            quantity=Decimal("4"), unit_price_ex_tax=Decimal("25.00"),
        )
        self.url = f"/api/v1/orders/orders/{self.order.pk}/"

    def _api(self, name, role, *, pinned=True):
        svc = ensure_service_account(name, [role], company=self.company if pinned else None)
        _, raw = issue_api_key(svc, name=name)
        return APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")

    def test_declared_actions_follow_the_model_permission(self):
        api = self._api("desk", "order-desk")
        response = api.post(f"{self.url}recalculate/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["grand_total"], "100.00")
        response = api.post(f"{self.url}transition/", {"status": "awaiting_payment"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        response = api.post(f"{self.url}convert-to-invoice/", {}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(SalesInvoice.objects.get().order_id, self.order.pk)

    def test_the_permission_is_what_decides(self):
        api = self._api("reader", "order-reader")
        for action in ("recalculate", "transition", "convert-to-invoice", "duplicate"):
            with self.subTest(action=action):
                response = api.post(f"{self.url}{action}/", {"status": "awaiting_payment"}, format="json")
                self.assertEqual(response.status_code, 403, response.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_each_action_needs_every_permission_it_names(self):
        # "partial" holds add/view on orders and invoices and change on quotes
        # and sub-orders, but not change_order, change_payment or
        # change_engagement: one short of each action below.
        api = self._api("partial", "partial")
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        sub = SubOrder.objects.create(
            sub_order_number="SUB-CT-1", order=self.order, status=SubOrder.Status.PACKED,
        )
        refused = (
            (f"{self.url}convert-to-invoice/", {}),                       # change_order, change_payment
            (f"/api/v1/orders/quotes/{quote.pk}/convert-to-order/", {}),  # crm.change_engagement
            (f"/api/v1/orders/sub-orders/{sub.pk}/transition/", {"status": "shipped"}),  # change_order
            (f"{self.url}recalculate/", {}),                              # change_order
        )
        for url, body in refused:
            with self.subTest(url=url):
                self.assertEqual(api.post(url, body, format="json").status_code, 403)
        self.assertEqual(Order.objects.count(), 1)
        self.assertFalse(SalesInvoice.objects.exists())
        sub.refresh_from_db()
        self.assertEqual(sub.status, SubOrder.Status.PACKED)
        # It may duplicate an order: view and add are both held.
        response = api.post(f"{self.url}duplicate/", {}, format="json")
        self.assertEqual(response.status_code, 201, response.content)

    def test_duplicate_needs_view_as_well_as_add(self):
        with override_settings(GOLDEN_ROLES=ROLES + [
            {"name": "adder", "label": "Adder", "kind": "service", "permissions": ["orders.add_order"]},
        ]):
            api = self._api("adder", "adder")
            response = api.post(f"{self.url}duplicate/", {}, format="json")
            self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(Order.objects.count(), 1)

    def test_sub_order_and_purchase_order_transitions(self):
        api = self._api("desk", "order-desk")
        sub = SubOrder.objects.create(
            sub_order_number="SUB-CT-2", order=self.order, status=SubOrder.Status.PACKED,
        )
        response = api.post(
            f"/api/v1/orders/sub-orders/{sub.pk}/transition/", {"status": "shipped"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        po = PurchaseOrder.objects.create(
            company=self.company, status=PurchaseOrder.Status.DRAFT,
            supplier=Supplier.objects.create(
                company_name="Acme Corp", supplier_code="ACME-CT", company=self.company,
            ),
        )
        base = f"/api/v1/purchasing/purchase-orders/{po.pk}/"
        self.assertEqual(api.post(f"{base}transition/", {"status": "sent"}, format="json").status_code, 200)
        self.assertEqual(api.post(f"{base}recalculate/", {}, format="json").status_code, 200)
        # receive moves stock and posts: never in a contract.
        self.assertEqual(api.post(f"{base}receive/", {"lines": []}, format="json").status_code, 403)
        # duplicate needs add_purchaseorder, which the role lacks.
        self.assertEqual(api.post(f"{base}duplicate/", {}, format="json").status_code, 403)

    def test_the_storefront_identity_never_reaches_an_action(self):
        from core.storefront import ensure_storefront_identity

        user = ensure_storefront_identity()
        perms = Permission.objects.filter(codename__in=["view_order", "change_order"])
        user.groups.first().permissions.add(*perms)
        UserProfile.objects.update_or_create(user=user, defaults={"company": self.company})
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=user.pk))
        response = api.post(f"{self.url}recalculate/", {}, format="json")
        self.assertEqual(response.status_code, 403, response.content)

    def test_an_unlisted_action_is_refused_whatever_the_role_holds(self):
        api = self._api("desk", "order-desk")
        response = api.post(f"{self.url}send-confirmation/", {}, format="json")
        self.assertEqual(response.status_code, 403, response.content)

    def test_an_unpinned_account_is_refused(self):
        api = self._api("loose", "order-desk", pinned=False)
        response = api.post(f"{self.url}recalculate/", {}, format="json")
        self.assertEqual(response.status_code, 403, response.content)

    def test_another_companys_order_is_not_found(self):
        other = Company.objects.create(name="Other Co")
        theirs = Order.objects.create(
            customer=Customer.objects.create(
                first_name="B", last_name="C", email="other-contract@example.com", company=other,
            ),
            company=other, status=Order.Status.PENDING,
        )
        api = self._api("desk", "order-desk")
        response = api.post(f"/api/v1/orders/orders/{theirs.pk}/recalculate/", {}, format="json")
        self.assertEqual(response.status_code, 404, response.content)


LOOKUP_ROLES = ROLES + [
    {"name": "lookup", "label": "Lookup", "kind": "service",
     "permissions": ["parties.view_party", "customers.view_customer"]},
    {"name": "half-lookup", "label": "Half lookup", "kind": "service",
     "permissions": ["parties.view_party"]},
]


@override_settings(API_KEY="", GOLDEN_ROLES=LOOKUP_ROLES)
class ReadOnlyViewContractTests(TestCase):
    """``parties/match/`` is a read by POST: ``service_read_perms`` decides it."""

    URL = "/api/v1/parties/match/"

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Other Tenant LLC")
        self.mine = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada-match@example.com",
            company=self.company,
        )
        Customer.objects.create(
            first_name="Eve", last_name="Elsewhere", email="eve-match@example.com",
            company=self.other,
        )

    def _api(self, name, role, *, pinned=True):
        svc = ensure_service_account(name, [role], company=self.company if pinned else None)
        _, raw = issue_api_key(svc, name=name)
        return APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")

    def _match(self, api, email, **extra):
        return api.post(self.URL, {"target": "customer", "email": email, **extra}, format="json")

    def test_a_key_with_both_view_permissions_matches_in_its_own_tenant(self):
        api = self._api("lookup", "lookup")
        response = self._match(api, "ada-match@example.com")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["match_quality"], "exact")
        self.assertEqual(api.get(self.URL, {"target": "customer", "q": "Ada"}).status_code, 200)
        # Another tenant's customer is not there, and cannot be asked for.
        self.assertEqual(self._match(api, "eve-match@example.com").json()["match_quality"], "none")
        response = self._match(api, "eve-match@example.com", company=str(self.other.pk))
        self.assertEqual(response.status_code, 400, response.content)

    def test_every_named_permission_is_needed(self):
        for name, role in (("half", "half-lookup"), ("reader", "order-reader")):
            with self.subTest(role=role):
                response = self._match(self._api(name, role), "ada-match@example.com")
                self.assertEqual(response.status_code, 403, response.content)

    def test_an_unpinned_key_matches_nothing(self):
        response = self._match(self._api("loose", "lookup", pinned=False), "ada-match@example.com")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["match_quality"], "none")

    def test_a_flagged_service_account_is_still_refused(self):
        svc = ensure_service_account("flagged", ["lookup"], company=self.company)
        _, raw = issue_api_key(svc, name="flagged")
        User.objects.filter(pk=svc.pk).update(is_superuser=True)
        response = self._match(APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}"), "ada-match@example.com")
        self.assertIn(response.status_code, (401, 403), response.content)


class DeclarationTests(TestCase):
    """The viewsets' declarations and the matrix's own list say the same thing."""

    def _declaring_viewsets(self):
        seen = {}
        for endpoint in access_matrix.api_viewsets():
            if getattr(endpoint.cls, "service_action_perms", None):
                seen.setdefault(endpoint.cls, []).append(endpoint)
        return seen

    def test_viewset_declarations_match_the_matrix_expectations(self):
        declared = {}
        for cls, endpoints in self._declaring_viewsets().items():
            for action in cls.service_action_perms:
                key = (endpoints[0].model, action)
                self.assertNotIn(key, declared, "two viewsets declare the same model and action")
                declared[key] = action_permissions(cls(), action)
        self.assertEqual(declared, exp.CONTRACT_ACTIONS)

    def test_a_declared_action_is_routed_and_never_reachable_by_get(self):
        # Safe requests are decided by view_<model> before the declaration is
        # read, so a declared action that answered GET would ignore it.
        for cls, endpoints in self._declaring_viewsets().items():
            for action in cls.service_action_perms:
                methods = {e.method for e in endpoints if e.action == action}
                with self.subTest(viewset=cls.__name__, action=action):
                    self.assertTrue(methods, "declared action is not routed")
                    self.assertFalse(methods & {"get", "head", "options"}, methods)

    def test_a_malformed_declaration_grants_nothing(self):
        class View:
            queryset = Order.objects.all()
            service_action_perms = {"a": "nonsense", "b": ("change", "bad"), "c": ""}

        for action in ("a", "b", "c", "missing"):
            self.assertEqual(action_permissions(View(), action), ())
