"""Step 0 (AD-12, F-026): session-user profile→company resolution.

A staff user logging into /erp/ must resolve a company through
``user.userprofile.company`` so company-scoped views return rows. With exactly
one Company the link auto-attaches; with several it stays unresolved
(fail closed) until an admin assigns one.
"""

from django.contrib.auth.models import User
from django.test import TestCase

from accounting.models import Account
from core.managers import scope_to_company
from core.models import Company
from core.tests.helpers import make_staff_user


def _account(company, name="Cash"):
    return Account.objects.create(
        name=name, account_type=Account.AccountType.ASSET, company=company
    )


class SessionCompanyResolutionTests(TestCase):
    def test_login_links_profile_and_resolves_single_company(self):
        company = Company.objects.create(name="Acme")
        _account(company)
        user = make_staff_user("staff", password="pw")
        self.assertTrue(self.client.login(username="staff", password="pw"))

        user.refresh_from_db()
        self.assertEqual(user.userprofile.company, company)

        request = type("R", (), {"user": user})()
        self.assertEqual(scope_to_company(Account.objects.all(), request).count(), 1)

    def test_multiple_companies_stay_unresolved_and_fail_closed(self):
        mine = Company.objects.create(name="Mine")
        other = Company.objects.create(name="Other")
        _account(mine)
        _account(other, name="Bank")
        user = make_staff_user("staff2", password="pw")
        self.assertTrue(self.client.login(username="staff2", password="pw"))

        user.refresh_from_db()
        self.assertIsNotNone(user.userprofile)
        self.assertIsNone(user.userprofile.company)

        request = type("R", (), {"user": user})()
        self.assertEqual(
            list(scope_to_company(Account.objects.all(), request)), []
        )

    def test_superuser_bypass_unaffected(self):
        mine = Company.objects.create(name="Mine")
        Company.objects.create(name="Other")
        _account(mine)
        admin = User.objects.create_superuser("root", password="pw", email="r@x.io")
        self.client.login(username="root", password="pw")

        request = type("R", (), {"user": admin})()
        self.assertEqual(scope_to_company(Account.objects.all(), request).count(), 1)
