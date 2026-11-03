"""ERPListView sorting is allow-listed (CRM Phase 4 step 0).

``?sort=`` used to go straight to ``order_by``, so any field path, including
relations the list never shows, could be sorted on: ordering by
``customer__user__password`` leaks hash order and joins tables the view
never meant to touch. Now only the view's sortable ids apply; anything else
is ignored and the default ordering stands (no error, so old bookmarks load).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import Company, UserProfile
from core.views.list_view import ERPListView, _is_orderable_path
from customers.erp_views import CustomerListView
from customers.models import Customer
from orders.erp_views import SalesOrderListView
from orders.models import Order


class SortAllowListUnitTests(TestCase):
    def test_default_allow_list_is_the_orderable_columns(self):
        fields = SalesOrderListView().get_sortable_fields()
        self.assertEqual(
            set(fields),
            {c["field"] for c in SalesOrderListView.list_columns},
        )
        self.assertNotIn("customer__user__password", fields)

    def test_properties_reverse_and_unknown_paths_are_not_orderable(self):
        self.assertFalse(_is_orderable_path(Customer, "full_name"))  # property
        self.assertFalse(_is_orderable_path(Customer, "orders"))  # reverse FK
        self.assertFalse(_is_orderable_path(Customer, "nope"))
        self.assertFalse(_is_orderable_path(Customer, "email__x"))  # not a relation
        self.assertTrue(_is_orderable_path(Order, "customer__last_name"))

    def test_iterable_and_dict_declarations(self):
        class V(ERPListView):
            model = Customer
            sortable_fields = ["email"]

        self.assertEqual(V().get_sortable_fields(), {"email": ("email",)})
        self.assertEqual(
            CustomerListView().get_sortable_fields()["full_name"],
            ("last_name", "first_name"),
        )


class SortAllowListViewTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme Sorting")
        self.user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        UserProfile.objects.create(user=self.user, company=self.company)
        for last, email in (("Babbage", "c@example.com"), ("Lovelace", "a@example.com"),
                            ("Hopper", "b@example.com")):
            Customer.objects.create(first_name="X", last_name=last, email=email,
                                    company=self.company)
        self.client.force_login(self.user)

    def _names(self, url, params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        return response, [c.last_name for c in response.context["object_list"]]

    def test_sensitive_related_path_is_refused_and_default_order_applies(self):
        Order.objects.create(customer=Customer.objects.first(), company=self.company)
        url = reverse("erp-order-list")
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, {"sort": "customer__user__password", "dir": "desc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "")
        list_sql = [q["sql"] for q in queries if "orders_order" in q["sql"]]
        self.assertTrue(list_sql)
        for sql in list_sql:
            self.assertNotIn("password", sql)

    def test_unlisted_sort_on_customers_keeps_default_order(self):
        url = reverse("erp-customer-list")
        _, default = self._names(url, {})
        response, refused = self._names(url, {"sort": "company__name", "dir": "desc"})
        self.assertEqual(refused, default)
        self.assertEqual(response.context["current_sort"], "")

    def test_listed_sort_applies(self):
        url = reverse("erp-customer-list")
        response, names = self._names(url, {"sort": "email", "dir": "desc"})
        self.assertEqual(names, ["Babbage", "Hopper", "Lovelace"])
        self.assertEqual(response.context["current_sort"], "email")
        self.assertEqual(response.context["current_sort_dir"], "desc")

    def test_property_column_sorts_by_its_declared_paths(self):
        _, names = self._names(reverse("erp-customer-list"), {"sort": "full_name"})
        self.assertEqual(names, ["Babbage", "Hopper", "Lovelace"])

    def test_party_list_ignores_unlisted_sort(self):
        response = self.client.get(
            reverse("erp-party-list"), {"sort": "owner__user__password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "")
