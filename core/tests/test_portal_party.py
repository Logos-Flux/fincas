"""Portal auth on Party (CRM Phase 2 step 7, docs/crm/phase2-party.md 2.6).

One test class per row of the portal-auth table, resolved through the
portal tenant's parties (``parties.portal``) instead of Customer emails, plus
the extra cases 2.6 names: a race between two first logins, a missing portal
company (system check), a staff email through an untrusted provider, and a
merge that turns match-N into match-1. The step-4 suite
(``test_portal_auth.py``) still covers the token and provider side.
"""

from __future__ import annotations

import uuid
from unittest import mock, skipUnless

from django.contrib.auth.models import User
from django.core.checks import run_checks
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.identity.models import UserIdentity
from core.identity.resolve import (
    CODE_AMBIGUOUS,
    CODE_CLOSED,
    CODE_COLLISION,
    CODE_MERGE_CYCLE,
    CODE_NOT_CONFIGURED,
    CODE_VERIFY_EMAIL,
    PortalAuthFailed,
    PortalRefused,
    _collision,
)
from core.models import AuditLog, Company
from core.tests.helpers import (
    LOGIN_LOCK_ORDER,
    login_lock_order,
    make_portal_customer,
    make_staff_user,
)
from core.tests.test_portal_auth import SECRET, _authenticate, _code, make_token, provider_confirms
from customers.models import Customer
from parties.models import Party, PartyEmail, PartyRole


def person(company, email, **kw):
    """A live person party holding ``email`` and no commerce account (a lead, an import)."""
    party = Party.objects.create(
        company=company, kind="person", display_name=kw.pop("display_name", "Pat Person"),
        given_name=kw.pop("given_name", "Pat"), family_name=kw.pop("family_name", "Person"),
        origin="import", **kw,
    )
    PartyEmail.objects.create(party=party, email=email, purpose="personal", is_primary=True)
    return party


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class _Base(TestCase):
    CONFIRMS = True

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Portal Tenant")
        if self.CONFIRMS is not None:
            patcher = provider_confirms(self.CONFIRMS)
            patcher.start()
            self.addCleanup(patcher.stop)

    def refused(self, token, code, exc_type=PortalRefused):
        with self.assertRaises(exc_type) as ctx:
            _authenticate(token)
        self.assertEqual(_code(ctx.exception), code)
        return ctx.exception

    def login(self, email, sub=None, verified=True, **kw):
        return _authenticate(make_token(sub=sub or str(uuid.uuid4()), email=email,
                                        email_verified=verified, **kw))[0]


class LinkedTests(_Base):
    def test_identity_resolves_through_the_party(self):
        customer, sub = make_portal_customer("linked@example.com", company=self.company)
        party = customer.account_holder
        self.assertEqual(party.user, customer.user)
        user = _authenticate(make_token(sub=sub))[0]
        self.assertEqual(user, customer.user)

    def test_party_drift_is_repaired_on_demand(self):
        """A login whose customer has no account holder (sync skipped) gets the sync run."""
        customer, sub = make_portal_customer("drift@example.com", company=self.company)
        Customer.objects.filter(pk=customer.pk).update(account_holder=None)
        Party.objects.filter(pk=customer.party_id).update(user=None)
        user = _authenticate(make_token(sub=sub))[0]
        customer.refresh_from_db()
        self.assertEqual(user, customer.user)
        self.assertIsNotNone(customer.account_holder_id)
        self.assertEqual(customer.account_holder.user, user)

    def test_customer_of_another_tenant_is_not_this_portal(self):
        other = Company.objects.create(name="Other Tenant")
        customer, sub = make_portal_customer("elsewhere@example.com", company=other)
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.company.pk)):
            self.refused(make_token(sub=sub), CODE_CLOSED)


class Match0Tests(_Base):
    def test_creates_party_customer_role_and_verified_email(self):
        user = self.login("fresh@example.com", user_metadata={"first_name": "Fre", "last_name": "Sh"})
        customer = Customer.objects.get(user=user)
        self.assertEqual(customer.company, self.company)
        party = customer.account_holder
        self.assertEqual(customer.party, party)
        self.assertEqual((party.origin, party.user, party.company), ("portal", user, self.company))
        email = party.emails.get()
        self.assertIsNotNone(email.verified_at)
        self.assertTrue(PartyRole.objects.filter(party=party, role="customer", valid_to__isnull=True).exists())

    def test_unverified_sign_up_creates_nothing(self):
        self.refused(make_token(email="nobody@example.com", email_verified=False),
                     CODE_VERIFY_EMAIL, PortalAuthFailed)
        self.assertFalse(Party.objects.filter(origin="portal").exists())
        self.assertFalse(Customer.objects.exists())

    def test_match_is_per_tenant(self):
        """A party with E in another tenant is not a candidate here (3.1)."""
        other = Company.objects.create(name="Other Tenant")
        person(other, "split@example.com")
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.company.pk)):
            user = self.login("split@example.com")
        self.assertEqual(Customer.objects.get(user=user).company, self.company)


class LookalikeSignUpTests(_Base):
    def test_sign_up_never_adopts_a_lookalike_party(self):
        lead = person(self.company, "straße@example.com")
        user = self.login("strasse@example.com")
        lead.refresh_from_db()
        self.assertIsNone(lead.user)
        customer = Customer.objects.get(user=user)
        self.assertNotEqual(customer.account_holder_id, lead.pk)
        self.assertEqual(customer.account_holder.origin, "portal")
        self.assertFalse(Customer.objects.filter(account_holder=lead).exists())


class Match1Tests(_Base):
    def test_party_without_customer_gets_an_individual_customer(self):
        party = person(self.company, "lead@example.com", given_name="Lee", family_name="Ead")
        user = self.login("lead@example.com")
        party.refresh_from_db()
        self.assertEqual(party.user, user)
        customer = Customer.objects.get(user=user)
        self.assertEqual((customer.party, customer.account_holder), (party, party))
        self.assertEqual((customer.first_name, customer.last_name), ("Lee", "Ead"))
        self.assertEqual(customer.customer_type, "individual")
        self.assertIsNotNone(party.emails.get().verified_at)
        self.assertEqual(Party.objects.filter(company=self.company, kind="person").count(), 1)

    def test_customer_without_login_is_linked(self):
        customer = Customer.objects.create(first_name="No", last_name="Login", email="nologin@example.com",
                                           company=self.company)
        user = self.login("nologin@example.com")
        customer.refresh_from_db()
        self.assertEqual(customer.user, user)
        self.assertEqual(customer.account_holder.user, user)
        self.assertEqual(customer.account_holder, customer.party)

    def test_business_customer_person_is_linked_to_the_business_account(self):
        customer = Customer.objects.create(
            first_name="Bo", last_name="Ss", email="boss@example.com", company=self.company,
            customer_type="business", company_name="Acme",
        )
        user = self.login("boss@example.com")
        customer.refresh_from_db()
        self.assertEqual(customer.user, user)
        self.assertEqual(customer.account_holder.user, user)
        self.assertEqual(customer.party.kind, "organisation")

    def test_case_differences_still_link(self):
        party = person(self.company, "Casey@Example.com")
        user = self.login("casey@example.COM")
        party.refresh_from_db()
        self.assertEqual(party.user, user)

    def test_lookalike_addresses_never_reach_another_party(self):
        """NFKC + casefold folds these onto each other; a login never crosses that fold."""
        for victim_email, login_email in (("straße@example.com", "strasse@example.com"),
                                          ("strasse2@example.com", "straße2@example.com"),
                                          ("ﬁnance@example.com", "finance@example.com"),
                                          ("ｊohn@example.com", "john@example.com"),
                                          ("\u212aate@example.com", "kate@example.com")):
            victim = Customer.objects.create(first_name="V", last_name="C", email=victim_email,
                                             company=self.company)
            if Customer.email_taken(login_email, self.company.pk):
                # The database's LOWER() folds this pair, so
                # uniq_customer_email_ci_company leaves no room for a second
                # account: the sign-up is refused as a collision (someone
                # else's account), with the same code and message as an exact
                # match held by another login, so it tells the caller nothing
                # that path doesn't. Backend-dependent: Postgres folds U+212A
                # KELVIN SIGN to "k"; SQLite's LOWER is ASCII-only. Asking the
                # database keeps the test honest on both.
                before = (User.objects.count(), Party.objects.count(),
                          UserIdentity.objects.count())
                refusal = self.refused(make_token(sub=str(uuid.uuid4()), email=login_email,
                                                  email_verified=True), CODE_COLLISION)
                self.assertEqual((refusal.status_code, refusal.detail),
                                 (_collision().status_code, _collision().detail))
                self.assertEqual(before, (User.objects.count(), Party.objects.count(),
                                          UserIdentity.objects.count()))
            else:
                user = self.login(login_email)
                self.assertNotEqual(Customer.objects.get(user=user).pk, victim.pk)
            victim.refresh_from_db()
            self.assertIsNone(victim.user, victim_email)
            self.assertIsNone(victim.account_holder_id, victim_email)
            self.assertIsNone(victim.party.emails.get().verified_at, victim_email)


    def test_sign_up_the_email_index_refuses_is_a_collision(self):
        """match-0 hitting uniq_customer_email_ci_company answers collision, not "no account".

        On Postgres a KELVIN SIGN look-alike gets here (above); the matcher is
        stubbed so every backend exercises the same branch with an exact copy.
        """
        victim = Customer.objects.create(first_name="V", last_name="C", email="kate@example.com",
                                         company=self.company)
        with mock.patch("core.identity.resolve.find_candidates", return_value=[]):
            refusal = self.refused(make_token(sub=str(uuid.uuid4()), email="kate@example.com",
                                              email_verified=True), CODE_COLLISION)
        self.assertEqual(refusal.status_code, 403)
        victim.refresh_from_db()
        self.assertIsNone(victim.user)
        self.assertFalse(UserIdentity.objects.exists())

    def test_sign_up_race_on_the_subject_still_resolves_the_winner(self):
        """An IntegrityError from a concurrent first login is not read as a collision."""
        customer, sub = make_portal_customer("race0@example.com", company=self.company)
        with mock.patch("core.identity.resolve.find_candidates", return_value=[]), \
                mock.patch("core.identity.resolve.UserIdentity.objects.select_related") as first:
            first.return_value.filter.return_value.first.side_effect = [
                None, UserIdentity.objects.get(subject=sub)]
            user = _authenticate(make_token(sub=sub, email="race0@example.com",
                                            email_verified=True))[0]
        self.assertEqual(user, customer.user)


class UnverifiedTests(_Base):
    def test_candidate_with_unverified_email_is_refused(self):
        party = person(self.company, "unv@example.com")
        self.refused(make_token(email="unv@example.com", email_verified=False),
                     CODE_VERIFY_EMAIL, PortalAuthFailed)
        party.refresh_from_db()
        self.assertIsNone(party.user)
        self.assertFalse(Customer.objects.exists())


class UnconfirmedTests(_Base):
    CONFIRMS = False

    def test_provider_not_confirming_refuses_the_link(self):
        party = person(self.company, "unconf@example.com")
        self.refused(make_token(email="unconf@example.com", email_verified=True),
                     CODE_VERIFY_EMAIL, PortalAuthFailed)
        party.refresh_from_db()
        self.assertIsNone(party.user)


class MatchNTests(_Base):
    def test_two_parties_refuse_and_audit_each(self):
        a = person(self.company, "twin@example.com")
        b = person(self.company, "twin@example.com")
        self.refused(make_token(email="twin@example.com", email_verified=True), CODE_AMBIGUOUS)
        audited = set(AuditLog.objects.filter(action="portal_refused", model_name="parties.Party")
                      .values_list("object_id", flat=True))
        self.assertEqual(audited, {a.pk, b.pk})
        self.assertFalse(UserIdentity.objects.exists())

    def test_never_picks_the_one_holding_a_customer(self):
        Customer.objects.create(first_name="Has", last_name="Acct", email="pick@example.com", company=self.company)
        person(self.company, "pick@example.com")
        self.refused(make_token(email="pick@example.com", email_verified=True), CODE_AMBIGUOUS)

    def test_merge_turns_match_n_into_match_1(self):
        a = person(self.company, "merge@example.com")
        b = person(self.company, "merge@example.com")
        token = make_token(email="merge@example.com", email_verified=True)
        self.refused(token, CODE_AMBIGUOUS)
        Party.objects.filter(pk=b.pk).update(merged_into=a, merged_at=timezone.now())
        user = _authenticate(token)[0]
        a.refresh_from_db()
        self.assertEqual(a.user, user)


class CollisionTests(_Base):
    def test_staff_email_through_untrusted_provider(self):
        staff = make_staff_user("staffer", "staff@example.com")
        party = person(self.company, "staff@example.com", user=staff)
        self.refused(make_token(email="staff@example.com", email_verified=True), CODE_COLLISION)
        party.refresh_from_db()
        self.assertEqual(party.user, staff)
        self.assertFalse(UserIdentity.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="portal_refused", object_id=party.pk).exists())

    def test_party_held_by_another_live_subject(self):
        customer, held = make_portal_customer("held@example.com", company=self.company)
        with mock.patch("core.identity.providers.SupabaseJWT.subject_status", return_value="live"):
            self.refused(make_token(email="held@example.com", email_verified=True), CODE_COLLISION)
        self.assertEqual(list(UserIdentity.objects.values_list("subject", flat=True)), [held])

    def test_party_and_customer_with_different_logins(self):
        customer, _sub = make_portal_customer("split.login@example.com", company=self.company)
        other = User.objects.create_user("other-login")
        Party.objects.filter(pk=customer.account_holder_id).update(user=other)
        self.refused(make_token(email="split.login@example.com", email_verified=True), CODE_COLLISION)


class ClosedTests(_Base):
    def test_inactive_customer(self):
        customer, sub = make_portal_customer("closed@example.com", company=self.company)
        customer.is_active = False
        customer.save()
        self.refused(make_token(sub=sub), CODE_CLOSED)

    def test_erased_party(self):
        """Closed even while the Customer still looks active (no fallback to Customer.user)."""
        customer, sub = make_portal_customer("erased@example.com", company=self.company)
        Party.objects.filter(pk=customer.account_holder_id).update(erased_at=timezone.now())
        self.refused(make_token(sub=sub), CODE_CLOSED)

    def test_erased_merge_survivor(self):
        customer, sub = make_portal_customer("erased2@example.com", company=self.company)
        survivor = person(self.company, "survivor2@example.com", erased_at=timezone.now())
        Party.objects.filter(pk=customer.account_holder_id).update(merged_into=survivor)
        self.refused(make_token(sub=sub), CODE_CLOSED)


class MergedTests(_Base):
    def test_merge_chain_never_leaves_the_tenant(self):
        customer, sub = make_portal_customer("hop@example.com", company=self.company)
        elsewhere = person(Company.objects.create(name="Elsewhere"), "hop@example.com")
        Party.objects.filter(pk=customer.account_holder_id).update(merged_into=elsewhere)
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.company.pk)):
            self.refused(make_token(sub=sub), CODE_MERGE_CYCLE)

    def test_login_follows_the_merge_to_the_survivor(self):
        customer, sub = make_portal_customer("loser@example.com", company=self.company)
        loser = customer.account_holder
        survivor = person(self.company, "survivor@example.com")
        # What a merge (step 6) leaves: the account and the login on the survivor.
        Party.objects.filter(pk=loser.pk).update(merged_into=survivor, merged_at=timezone.now(), user=None)
        Party.objects.filter(pk=survivor.pk).update(user=customer.user)
        Customer.objects.filter(pk=customer.pk).update(party=survivor, account_holder=survivor)
        user = _authenticate(make_token(sub=sub))[0]
        self.assertEqual(user, customer.user)

    def test_merge_cycle_is_a_500(self):
        customer, sub = make_portal_customer("cycle@example.com", company=self.company)
        a = customer.account_holder
        b = person(self.company, "cycle2@example.com")
        Party.objects.filter(pk=a.pk).update(merged_into=b)
        Party.objects.filter(pk=b.pk).update(merged_into=a)
        exc = self.refused(make_token(sub=sub), CODE_MERGE_CYCLE)
        self.assertEqual(exc.status_code, 500)


class RaceTests(_Base):
    def test_two_first_logins_for_the_same_subject(self):
        """The loser of the race hits IntegrityError and re-fetches the winner's link."""
        from core.identity import resolve

        sub = str(uuid.uuid4())
        winner = self.login("race@example.com", sub=sub)
        real = resolve.UserIdentity.objects.select_related

        def no_identity_once(*args, **kwargs):
            qs = real(*args, **kwargs)
            if not getattr(no_identity_once, "done", False):
                no_identity_once.done = True
                return qs.none()
            return qs

        with mock.patch.object(resolve.UserIdentity.objects, "select_related", side_effect=no_identity_once):
            again = self.login("race@example.com", sub=sub)
        self.assertEqual(again, winner)
        self.assertEqual(UserIdentity.objects.filter(subject=sub).count(), 1)


class PortalCompanyTests(TestCase):
    def test_e001_when_several_companies_and_no_setting(self):
        Company.objects.create(name="A")
        Company.objects.create(name="B")
        ids = [m.id for m in run_checks()]
        self.assertIn("parties.E001", ids)
        with override_settings(GOLDEN_PORTAL_COMPANY=str(Company.objects.first().pk)):
            self.assertNotIn("parties.E001", [m.id for m in run_checks()])

    def test_no_e001_without_a_portal_provider(self):
        Company.objects.create(name="A")
        Company.objects.create(name="B")
        with override_settings(GOLDEN_IDENTITY_PROVIDERS=[]):
            self.assertNotIn("parties.E001", [m.id for m in run_checks()])

    def test_w001_when_the_setting_names_no_company(self):
        Company.objects.create(name="A")
        with override_settings(GOLDEN_PORTAL_COMPANY=str(uuid.uuid4())):
            self.assertIn("parties.W001", [m.id for m in run_checks()])

    @override_settings(SUPABASE_JWT_SECRET=SECRET)
    def test_resolver_refuses_without_a_portal_company(self):
        Company.objects.create(name="A")
        Company.objects.create(name="B")
        with provider_confirms(True), self.assertRaises(PortalRefused) as ctx:
            _authenticate(make_token(email="who@example.com", email_verified=True))
        self.assertEqual(_code(ctx.exception), CODE_NOT_CONFIGURED)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertFalse(Customer.objects.exists())


@skipUnless(connection.features.has_select_for_update, "FOR UPDATE is a no-op on this backend")
class LockQueryTests(_Base):
    """match-1 locks Customer, Party, then User, each by itself (resolve.lock_users).

    A join in a locking query is refused by Postgres when outer, and returns
    the joined row as it was before the lock wait, so it is never used.
    """

    def test_match_1_locks_each_row_without_a_join(self):
        user = User.objects.create_user("lock-shopper")
        Customer.objects.create(first_name="L", last_name="Q", email="lockq@example.com",
                                company=self.company, user=user)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.login("lockq@example.com"), user)
        locks = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
        for table in ("customers_customer", "parties_party", "auth_user"):
            self.assertTrue(any(f'FROM "{table}"' in sql for sql in locks), table)
        for sql in locks:
            self.assertNotIn(" JOIN ", sql)

    def test_match_1_locks_in_the_login_order(self):
        user = User.objects.create_user("order-shopper")
        Customer.objects.create(first_name="O", last_name="Q", email="orderq@example.com",
                                company=self.company, user=user)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.login("orderq@example.com"), user)
        self.assertEqual(login_lock_order(queries), list(LOGIN_LOCK_ORDER))
