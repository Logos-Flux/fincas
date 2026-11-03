"""The party schema (phase2-party.md section 1, step 3).

Every constraint in the ``parties`` app has a ``test_constraint_<name>``
method that violates it (``test_every_constraint_has_a_violation_test``
enforces that), plus the ``company`` copy-on-save and the normalisation
that ``save()`` applies.
"""

from __future__ import annotations

import datetime
import hashlib
from io import StringIO

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, models, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Company
from parties.models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
    Suppression,
)

TODAY = datetime.date(2026, 10, 8)


def person(company, given="Ada", family="Lovelace", **kwargs):
    return Party.objects.create(
        company=company, kind=Party.Kind.PERSON, given_name=given, family_name=family,
        origin=Party.Origin.ERP, **kwargs,
    )


def organisation(company, name="Acme Widgets", **kwargs):
    return Party.objects.create(
        company=company, kind=Party.Kind.ORGANISATION, display_name=name,
        origin=Party.Origin.ERP, **kwargs,
    )


class PartyTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.user = get_user_model().objects.create_user("ada", password="x")

    def assertViolates(self, name, create):
        """``create()`` raises IntegrityError, naming ``name`` where the backend does."""
        with self.assertRaises(IntegrityError) as ctx, transaction.atomic():
            create()
        # Postgres names every constraint; SQLite names CHECK constraints only
        # (a partial unique index failure lists its columns instead).
        names_constraint = connection.vendor == "postgresql" or isinstance(
            self._constraint(name), models.CheckConstraint
        )
        if names_constraint:
            self.assertIn(name, str(ctx.exception))

    @staticmethod
    def _constraint(name):
        for model in apps.get_app_config("parties").get_models():
            for constraint in model._meta.constraints:
                if constraint.name == name:
                    return constraint
        raise AssertionError(f"no constraint named {name!r} in parties")


class ConstraintTests(PartyTestCase):
    def test_every_constraint_has_a_violation_test(self):
        names = {
            constraint.name
            for model in apps.get_app_config("parties").get_models()
            for constraint in model._meta.constraints
        }
        self.assertEqual(len(names), 18)
        missing = sorted(n for n in names if not hasattr(self, f"test_constraint_{n}"))
        self.assertEqual(missing, [])

    # --- Party (1.1) ---

    def test_constraint_uniq_party_user_per_company(self):
        person(self.company, user=self.user)
        # The same login may reach a person in another tenant.
        person(self.other_company, user=self.user)
        self.assertViolates("uniq_party_user_per_company",
                            lambda: person(self.company, "Other", "Person", user=self.user))

    def test_constraint_uniq_party_self_per_company(self):
        organisation(self.company, "Us", is_self=True)
        organisation(self.other_company, "Them", is_self=True)
        organisation(self.company, "Not us")
        self.assertViolates("uniq_party_self_per_company",
                            lambda: organisation(self.company, "Us again", is_self=True))

    def test_constraint_party_user_person_only(self):
        self.assertViolates("party_user_person_only",
                            lambda: organisation(self.company, user=self.user))

    def test_constraint_party_org_has_no_person_name(self):
        self.assertViolates("party_org_has_no_person_name",
                            lambda: organisation(self.company, given_name="Ada"))
        self.assertViolates("party_org_has_no_person_name",
                            lambda: organisation(self.company, family_name="Lovelace"))

    def test_constraint_party_not_merged_into_self(self):
        loser = person(self.company)
        survivor = person(self.company, "Ada", "King")
        loser.merged_into = survivor
        loser.save()

        def merge_into_self():
            survivor.merged_into = survivor
            survivor.save()

        self.assertViolates("party_not_merged_into_self", merge_into_self)

    # --- PartyMergeLog (3.4, step 6) ---

    def test_constraint_partymergelog_not_self(self):
        import uuid

        from parties.models import PartyMergeLog

        survivor = person(self.company)
        loser = person(self.company, "Ada", "King")
        common = dict(company=self.company, merge_id=uuid.uuid4(), after_digest="0" * 64, actor="t")
        PartyMergeLog.objects.create(survivor=survivor, loser=loser, **common)
        self.assertViolates("partymergelog_not_self",
                            lambda: PartyMergeLog.objects.create(survivor=survivor, loser=survivor, **common))

    # --- PartyRole (1.2) ---

    def test_constraint_uniq_partyrole_open(self):
        party = person(self.company)
        closed = dict(valid_from=TODAY - datetime.timedelta(days=30), valid_to=TODAY)
        PartyRole.objects.create(party=party, role="partner", **closed)
        PartyRole.objects.create(party=party, role="partner", **closed)  # history may repeat
        PartyRole.objects.create(party=party, role="partner", valid_from=TODAY)
        PartyRole.objects.create(party=party, role="supplier", valid_from=TODAY)
        self.assertViolates("uniq_partyrole_open", lambda: PartyRole.objects.create(
            party=party, role="partner", valid_from=TODAY))

    def test_constraint_partyrole_dates_ordered(self):
        party = person(self.company)
        PartyRole.objects.create(party=party, role="partner", valid_from=TODAY, valid_to=TODAY)
        self.assertViolates("partyrole_dates_ordered", lambda: PartyRole.objects.create(
            party=party, role="supplier", valid_from=TODAY,
            valid_to=TODAY - datetime.timedelta(days=1)))

    # --- PartyRelationship (1.3) ---

    def _ordered(self, *parties):
        return sorted(parties, key=lambda p: p.pk)

    def test_constraint_partyrel_not_self(self):
        party = person(self.company)
        self.assertViolates("partyrel_not_self", lambda: PartyRelationship.objects.create(
            from_party=party, to_party=party, kind="referral"))

    def test_constraint_partyrel_symmetric_canonical(self):
        low, high = self._ordered(person(self.company), person(self.company, "Bo", "B"))
        PartyRelationship.objects.create(from_party=low, to_party=high, kind="spouse")
        # Directed kinds may point either way.
        PartyRelationship.objects.create(from_party=high, to_party=low, kind="referral")
        self.assertViolates("partyrel_symmetric_canonical",
                            lambda: PartyRelationship.objects.create(
                                from_party=high, to_party=low, kind="partnership"))

    def test_constraint_uniq_partyrel_open(self):
        ada = person(self.company)
        acme = organisation(self.company)
        PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="employment",
                                         valid_to=TODAY)
        PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="employment")
        PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="contact_for")
        self.assertViolates("uniq_partyrel_open", lambda: PartyRelationship.objects.create(
            from_party=ada, to_party=acme, kind="employment"))

    def test_constraint_partyrel_dates_ordered(self):
        ada = person(self.company)
        acme = organisation(self.company)
        # Either end may be unknown.
        PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="employment",
                                         valid_to=TODAY)
        PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="contact_for",
                                         valid_from=TODAY)
        self.assertViolates("partyrel_dates_ordered", lambda: PartyRelationship.objects.create(
            from_party=ada, to_party=acme, kind="referral", valid_from=TODAY,
            valid_to=TODAY - datetime.timedelta(days=1)))

    # --- contact points (1.4) ---

    def test_constraint_uniq_partyemail_party_addr(self):
        ada = person(self.company)
        PartyEmail.objects.create(party=ada, email="Ada@Example.com", purpose="work")
        # Never unique across parties.
        PartyEmail.objects.create(party=person(self.company, "Bo", "B"), email="ada@example.com",
                                  purpose="work")
        # The normalised form collides even though the typed text differs.
        self.assertViolates("uniq_partyemail_party_addr", lambda: PartyEmail.objects.create(
            party=ada, email=" ADA@EXAMPLE.COM ", purpose="billing"))

    def test_constraint_uniq_partyemail_one_primary(self):
        ada = person(self.company)
        PartyEmail.objects.create(party=ada, email="a@example.com", purpose="work", is_primary=True)
        PartyEmail.objects.create(party=ada, email="b@example.com", purpose="work")
        self.assertViolates("uniq_partyemail_one_primary", lambda: PartyEmail.objects.create(
            party=ada, email="c@example.com", purpose="billing", is_primary=True))

    def test_constraint_uniq_partyphone_party_e164(self):
        ada = person(self.company)
        PartyPhone.objects.create(party=ada, number_raw="+1 415 555 2671", purpose="work")
        # Unparseable numbers are blank and never collide.
        PartyPhone.objects.create(party=ada, number_raw="call reception", purpose="work")
        PartyPhone.objects.create(party=ada, number_raw="ask for Bo", purpose="work")
        self.assertViolates("uniq_partyphone_party_e164", lambda: PartyPhone.objects.create(
            party=ada, number_raw="(415) 555-2671", purpose="billing"))

    def test_constraint_uniq_partyphone_one_primary(self):
        ada = person(self.company)
        PartyPhone.objects.create(party=ada, number_raw="+14155552671", purpose="work",
                                  is_primary=True)
        self.assertViolates("uniq_partyphone_one_primary", lambda: PartyPhone.objects.create(
            party=ada, number_raw="+14155552672", purpose="work", is_primary=True))

    def _address(self, party, **kwargs):
        return PartyAddress.objects.create(
            party=party, purpose="billing", address_line_1="1 Main St", city="Springfield",
            state_province="IL", postal_code="62701", country="us", **kwargs,
        )

    def test_constraint_uniq_partyaddress_one_primary(self):
        ada = person(self.company)
        self._address(ada, is_primary=True)
        self._address(ada)
        self.assertViolates("uniq_partyaddress_one_primary",
                            lambda: self._address(ada, is_primary=True))

    # --- consent (1.6) ---

    def _suppression(self, company, **kwargs):
        return Suppression.objects.create(
            company=company, channel="email",
            address_hash=hashlib.sha256(b"ada@example.com").hexdigest(),
            reason="opt_out", source="erp", **kwargs,
        )

    def test_constraint_uniq_suppression_active(self):
        self._suppression(self.company, lifted_at=timezone.now(), lift_reason="asked again")
        self._suppression(self.company)
        self._suppression(self.other_company)
        self.assertViolates("uniq_suppression_active", lambda: self._suppression(self.company))


class CompanyCopyOnSaveTests(PartyTestCase):
    """Child rows take ``company`` from their party and ignore the caller's (1, 3.1)."""

    def test_children_copy_party_company_and_ignore_input(self):
        ada = person(self.company)
        acme = organisation(self.company)
        rows = [
            PartyRole(party=ada, role="partner", valid_from=TODAY),
            PartyEmail(party=ada, email="ada@example.com", purpose="work"),
            PartyPhone(party=ada, number_raw="+14155552671", purpose="work"),
            PartyAddress(party=ada, purpose="billing", address_line_1="1 Main St",
                         city="Springfield", state_province="IL", postal_code="62701",
                         country="US"),
            ConsentRecord(party=ada, channel="email", purpose="marketing", state="granted",
                          occurred_at=timezone.now(), source="erp"),
            PartyRelationship(from_party=ada, to_party=acme, kind="employment"),
        ]
        for row in rows:
            with self.subTest(model=type(row).__name__):
                row.company = self.other_company
                row.save()
                row.refresh_from_db()
                self.assertEqual(row.company_id, self.company.pk)

    def test_unset_company_is_filled(self):
        role = PartyRole.objects.create(party=person(self.company), role="partner",
                                        valid_from=TODAY)
        self.assertEqual(role.company_id, self.company.pk)

    def test_update_fields_save_still_copies(self):
        ada = person(self.company)
        email = PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work")
        PartyEmail.objects.filter(pk=email.pk).update(company=self.other_company)
        email.refresh_from_db()
        email.status = PartyEmail.Status.BOUNCED
        email.save(update_fields=["status"])
        email.refresh_from_db()
        self.assertEqual(email.company_id, self.company.pk)
        self.assertEqual(email.status, PartyEmail.Status.BOUNCED)

    def test_relationship_copies_from_party(self):
        ada = person(self.company)
        acme = organisation(self.company)
        rel = PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="employment",
                                               company=self.other_company)
        self.assertEqual(rel.company_id, self.company.pk)

    def test_consent_without_party_keeps_given_company(self):
        record = ConsentRecord.objects.create(
            company=self.other_company, channel="email", purpose="marketing", state="withdrawn",
            occurred_at=timezone.now(), source="import",
        )
        self.assertEqual(record.company_id, self.other_company.pk)


class NormalisationOnSaveTests(PartyTestCase):
    def test_email_normalised_on_save(self):
        email = PartyEmail.objects.create(party=person(self.company), email=" Ada@ÜBER.example ",
                                          purpose="work")
        self.assertEqual(email.email_normalized, "ada@xn--ber-goa.example")
        email.email = "STRASSE@example.com"
        email.save(update_fields=["email"])
        email.refresh_from_db()
        self.assertEqual(email.email_normalized, "strasse@example.com")

    def test_invalid_email_refused(self):
        with self.assertRaises(ValidationError):
            PartyEmail.objects.create(party=person(self.company), email="no-at-sign",
                                      purpose="work")
        self.assertFalse(PartyEmail.objects.exists())

    def test_phone_normalised_on_save(self):
        phone = PartyPhone.objects.create(party=person(self.company),
                                          number_raw="(415) 555-2671 ext. 12", purpose="work")
        self.assertEqual(phone.number_raw, "(415) 555-2671 ext. 12")
        self.assertEqual(phone.number_e164, "+14155552671")
        self.assertEqual(phone.extension, "12")
        self.assertEqual(phone.line_type, PartyPhone.LineType.UNKNOWN)  # NANP: ambiguous

    def test_phone_line_type_inferred_and_fax_kept(self):
        ada = person(self.company)
        mobile = PartyPhone.objects.create(party=ada, number_raw="+44 7400 123456", purpose="work")
        self.assertEqual(mobile.line_type, PartyPhone.LineType.MOBILE)
        fax = PartyPhone.objects.create(party=ada, number_raw="+44 20 7946 0958", purpose="work",
                                        line_type=PartyPhone.LineType.FAX)
        self.assertEqual(fax.line_type, PartyPhone.LineType.FAX)

    def test_unparseable_phone_keeps_raw_and_blanks_e164(self):
        phone = PartyPhone.objects.create(party=person(self.company), number_raw="555-0143",
                                          purpose="work")
        self.assertEqual((phone.number_raw, phone.number_e164), ("555-0143", ""))

    def test_address_country_upper_cased(self):
        address = PartyAddress.objects.create(
            party=person(self.company), purpose="shipping", address_line_1="1 Main St",
            city="Springfield", state_province="IL", postal_code="62701", country=" gb",
        )
        self.assertEqual(address.country, "GB")

    def test_domain_normalised(self):
        acme = organisation(self.company, primary_domain="ÜBER.Example.")
        self.assertEqual(acme.primary_domain, "xn--ber-goa.example")

    def test_primary_contact_denormalised_to_party(self):
        ada = person(self.company)
        email = PartyEmail.objects.create(party=ada, email="Ada@Example.com", purpose="work",
                                          is_primary=True)
        PartyEmail.objects.create(party=ada, email="other@example.com", purpose="work")
        phone = PartyPhone.objects.create(party=ada, number_raw="+14155552671", purpose="work",
                                          is_primary=True)
        ada.refresh_from_db()
        self.assertEqual(ada.primary_email_normalized, "ada@example.com")
        self.assertEqual(ada.primary_phone_e164, "+14155552671")
        email.delete()
        phone.is_primary = False
        phone.save()
        ada.refresh_from_db()
        self.assertEqual((ada.primary_email_normalized, ada.primary_phone_e164), ("", ""))


class PrimaryContactDenormalisationTests(PartyTestCase):
    def test_stale_party_save_keeps_primary_columns(self):
        ada = person(self.company)
        stale = Party.objects.get(pk=ada.pk)
        PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work",
                                  is_primary=True)
        stale.display_name_locked = True
        stale.save()
        ada.refresh_from_db()
        self.assertEqual(ada.primary_email_normalized, "ada@example.com")
        self.assertTrue(ada.display_name_locked)

    def test_refresh_writes_even_when_cached_copy_matches(self):
        ada = person(self.company)
        email = PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work",
                                          is_primary=True)
        Party.objects.filter(pk=ada.pk).update(primary_email_normalized="")
        email.save()
        ada.refresh_from_db()
        self.assertEqual(ada.primary_email_normalized, "ada@example.com")

    def test_queryset_delete_refreshes(self):
        ada = person(self.company)
        PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work",
                                  is_primary=True)
        PartyPhone.objects.create(party=ada, number_raw="+14155552671", purpose="work",
                                  is_primary=True)
        ada.emails.all().delete()
        ada.phones.all().delete()
        ada.refresh_from_db()
        self.assertEqual((ada.primary_email_normalized, ada.primary_phone_e164), ("", ""))

    def test_party_hard_delete_cascades_cleanly(self):
        ada = person(self.company)
        PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work",
                                  is_primary=True)
        ada.delete()
        self.assertFalse(PartyEmail.objects.exists())

    def test_empty_update_fields_writes_nothing(self):
        ada = person(self.company)
        role = PartyRole.objects.create(party=ada, role="partner", valid_from=TODAY)
        with self.assertNumQueries(0):
            ada.save(update_fields=[])
            role.save(update_fields=[])


class PartyBehaviourTests(PartyTestCase):
    def test_phone_change_rederives_line_type_and_extension(self):
        phone = PartyPhone.objects.create(party=person(self.company),
                                          number_raw="+44 7400 123456 ext 55", purpose="work")
        self.assertEqual((phone.line_type, phone.extension), (PartyPhone.LineType.MOBILE, "55"))
        phone = PartyPhone.objects.get(pk=phone.pk)
        phone.number_raw = "+44 20 7946 0958"
        phone.save()
        self.assertEqual((phone.line_type, phone.extension), (PartyPhone.LineType.LANDLINE, ""))

    def test_explicit_line_type_and_extension_kept_on_create(self):
        phone = PartyPhone.objects.create(party=person(self.company), number_raw="+14155552671",
                                          purpose="work", line_type=PartyPhone.LineType.MOBILE,
                                          extension="7")
        self.assertEqual((phone.line_type, phone.extension), (PartyPhone.LineType.MOBILE, "7"))

    def test_person_display_name_derived_unless_locked(self):
        ada = person(self.company, "  Ada ", "Lovelace")
        self.assertEqual(ada.display_name, "Ada Lovelace")
        ada.given_name = "Augusta Ada"
        ada.save(update_fields=["given_name"])
        ada.refresh_from_db()
        self.assertEqual(ada.display_name, "Augusta Ada Lovelace")
        ada.display_name, ada.display_name_locked = "Countess of Lovelace", True
        ada.given_name = "Ada"
        ada.save()
        self.assertEqual(ada.display_name, "Countess of Lovelace")

    def test_kind_is_immutable(self):
        ada = Party.objects.get(pk=person(self.company).pk)
        ada.full_clean()
        ada.kind = Party.Kind.ORGANISATION
        ada.given_name = ada.family_name = ""
        with self.assertRaises(ValidationError) as ctx:
            ada.full_clean()
        self.assertIn("kind", ctx.exception.message_dict)

    def test_permissions(self):
        codenames = {codename for codename, _ in Party._meta.permissions}
        self.assertEqual(codenames, {"merge_party", "erase_party", "lift_suppression", "export_pii"})

    def test_role_clean_validates_against_registry(self):
        acme = organisation(self.company)
        with self.assertRaises(ValidationError):
            PartyRole(party=acme, role="no_such_role", valid_from=TODAY).full_clean()
        with self.assertRaises(ValidationError):  # lead is person-only
            PartyRole(party=acme, role="lead", valid_from=TODAY, company=self.company).full_clean()
        PartyRole(party=acme, role="supplier", valid_from=TODAY, company=self.company).full_clean()

    def test_relationship_kind_validated_against_registry(self):
        ada = person(self.company)
        acme = organisation(self.company)
        with self.assertRaises(ValidationError):
            PartyRelationship.objects.create(from_party=ada, to_party=acme, kind="no_such_kind")
        rel = PartyRelationship(from_party=acme, to_party=ada, kind="employment",
                                company=self.company, is_symmetric=False)
        with self.assertRaises(ValidationError):  # employment is person -> organisation
            rel.full_clean()
        PartyRelationship(from_party=ada, to_party=acme, kind="employment",
                          company=self.company, is_symmetric=False).full_clean()

    def test_relationship_symmetry_copied_from_registry(self):
        low, high = sorted([person(self.company), person(self.company, "Bo", "B")],
                           key=lambda p: p.pk)
        spouse = PartyRelationship.objects.create(from_party=low, to_party=high, kind="spouse",
                                                  is_symmetric=False)
        self.assertTrue(spouse.is_symmetric)
        referral = PartyRelationship.objects.create(from_party=high, to_party=low,
                                                    kind="referral", is_symmetric=True)
        self.assertFalse(referral.is_symmetric)

    def test_relationship_across_tenants_refused_by_clean(self):
        rel = PartyRelationship(from_party=person(self.company),
                                to_party=organisation(self.other_company), kind="employment",
                                company=self.company, is_symmetric=False)
        with self.assertRaises(ValidationError):
            rel.full_clean()

    def test_consent_record_is_append_only(self):
        record = ConsentRecord.objects.create(
            party=person(self.company), channel="email", purpose="marketing", state="granted",
            occurred_at=timezone.now(), source="portal",
        )
        record.state = ConsentRecord.State.WITHDRAWN
        with self.assertRaises(ValidationError):
            record.save()
        record.refresh_from_db()
        self.assertEqual(record.state, ConsentRecord.State.GRANTED)


class CheckTenancyCommandTests(PartyTestCase):
    def run_check(self):
        out = StringIO()
        call_command("check_tenancy", "--app", "parties", stdout=out)
        return out.getvalue()

    def test_clean_data_passes(self):
        ada = person(self.company)
        PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work")
        PartyRelationship.objects.create(from_party=ada, to_party=organisation(self.company),
                                         kind="employment")
        self.assertIn("0 mismatched", self.run_check())

    def test_mismatch_reported_with_exit_1(self):
        ada = person(self.company)
        email = PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work")
        acme = organisation(self.other_company)
        # Rows written around save() can disagree.
        PartyEmail.objects.filter(pk=email.pk).update(company=self.other_company)
        PartyRelationship.objects.bulk_create([PartyRelationship(
            company=self.company, from_party=ada, to_party=acme, kind="employment",
            is_symmetric=False)])
        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("check_tenancy", "--app", "parties", stdout=out)
        self.assertEqual(ctx.exception.returncode, 1)
        self.assertIn("parties.PartyEmail: 1 row(s)", out.getvalue())
        self.assertIn("parties.PartyRelationship: 1 row(s) whose company differs from to_party",
                      out.getvalue())
        self.assertIn(str(email.pk), out.getvalue())

    def test_unknown_app_refused(self):
        with self.assertRaises(CommandError):
            call_command("check_tenancy", "--app", "no_such_app", stdout=StringIO())
