"""Which company a new order or quote form preselects (GOLDEN_DEFAULT_COMPANY_NAME)."""

from django.test import TestCase, override_settings

from core.models import Company
from orders.forms import OrderForm, QuoteForm

FORMS = (OrderForm, QuoteForm)


class DefaultCompanyTests(TestCase):
    def initial_company(self, form_class):
        return form_class().initial.get("company")

    def test_one_company_is_preselected_without_the_setting(self):
        only = Company.objects.create(name="Only Co")
        with override_settings(GOLDEN_DEFAULT_COMPANY_NAME=None):
            for form_class in FORMS:
                with self.subTest(form=form_class.__name__):
                    self.assertEqual(self.initial_company(form_class), only.pk)

    def test_several_companies_and_no_setting_preselect_nothing(self):
        Company.objects.create(name="First Co")
        Company.objects.create(name="Second Co")
        with override_settings(GOLDEN_DEFAULT_COMPANY_NAME=None):
            for form_class in FORMS:
                with self.subTest(form=form_class.__name__):
                    self.assertIsNone(self.initial_company(form_class))

    def test_the_setting_picks_its_company_case_insensitively(self):
        Company.objects.create(name="First Co")
        wanted = Company.objects.create(name="Second Co")
        with override_settings(GOLDEN_DEFAULT_COMPANY_NAME="second co"):
            for form_class in FORMS:
                with self.subTest(form=form_class.__name__):
                    self.assertEqual(self.initial_company(form_class), wanted.pk)

    def test_a_setting_naming_no_company_falls_back(self):
        only = Company.objects.create(name="Only Co")
        with override_settings(GOLDEN_DEFAULT_COMPANY_NAME="Nobody Ltd"):
            self.assertEqual(self.initial_company(OrderForm), only.pk)
        Company.objects.create(name="Another Co")
        with override_settings(GOLDEN_DEFAULT_COMPANY_NAME="Nobody Ltd"):
            self.assertIsNone(self.initial_company(OrderForm))

    def test_a_bound_form_is_left_alone(self):
        Company.objects.create(name="Only Co")
        self.assertIsNone(OrderForm(data={}).initial.get("company"))
