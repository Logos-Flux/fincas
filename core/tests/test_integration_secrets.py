"""Credentials at rest (docs/crm/phase6-adapters.md 2.6-2.7, step 1 "done when").

- a secret is Fernet-encrypted under the newest ``GOLDEN_SECRET_KEYS`` key,
  with no fallback derived from ``SECRET_KEY``;
- ``rotate_secrets`` re-encrypts every row, after which the old key alone
  can no longer read any of them;
- ``check`` fails with ``core.E040`` when no configured key can decrypt a
  stored credential, ``core.E041`` on a malformed key;
- accounts are one per provider per company, credentials one active per
  ``(account, owner, kind)``, grants only for profiles of the account's company.
"""

from __future__ import annotations

from io import StringIO

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from core import secrets
from core.integrations import accounts as services
from core.integrations.checks import secret_keys_check
from core.models import AuditLog, Company, IntegrationAccount, IntegrationCredential
from crm.tests.helpers import profile

PLUGINS = ["golden_erp.addons.fixture_crm.plugin.FixtureCrmPlugin"]
OLD = Fernet.generate_key().decode()
NEW = Fernet.generate_key().decode()
SECRET = "fixture-plaintext-Qx7-not-a-real-key"


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class IntegrationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other = Company.objects.create(name="Tenant B")

    def account(self, company=None, provider="fixture", **kwargs):
        return IntegrationAccount.objects.create(company=company or self.company, provider=provider, **kwargs)


class SecretsModuleTests(TestCase):
    def test_round_trip_under_the_first_key(self):
        with override_settings(GOLDEN_SECRET_KEYS=[NEW, OLD]):
            token, key_id = secrets.encrypt(SECRET)
            self.assertEqual(key_id, secrets.key_id(NEW))
            self.assertNotIn(SECRET, token)
            self.assertEqual(secrets.decrypt(token), SECRET)

    def test_no_key_refuses_to_encrypt_and_never_derives_one(self):
        with override_settings(GOLDEN_SECRET_KEYS=[], SECRET_KEY="x" * 50):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                secrets.encrypt(SECRET)
            self.assertIn("GOLDEN_SECRET_KEYS", str(ctx.exception))
            self.assertNotIn(SECRET, str(ctx.exception))

    def test_a_malformed_key_is_named_by_position_not_value(self):
        with override_settings(GOLDEN_SECRET_KEYS=[NEW, "not-a-fernet-key"]):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                secrets.configured_key_ids()
        self.assertIn("GOLDEN_SECRET_KEYS[1]", str(ctx.exception))
        self.assertNotIn("not-a-fernet-key", str(ctx.exception))

    def test_a_comma_separated_string_setting_also_works(self):
        with override_settings(GOLDEN_SECRET_KEYS=f"{NEW}, {OLD}"):
            self.assertEqual(secrets.configured_key_ids(), [secrets.key_id(NEW), secrets.key_id(OLD)])

    def test_key_id_is_a_fingerprint_not_the_key(self):
        kid = secrets.key_id(NEW)
        self.assertEqual(len(kid), 16)
        self.assertNotIn(kid, NEW)

    def test_revealed_never_prints_its_value(self):
        revealed = secrets.Revealed(SECRET, "api_key")
        for text in (str(revealed), repr(revealed), f"{revealed}", "%s" % revealed, f"{revealed!r}"):
            self.assertEqual(text, "<redacted:api_key>")
        self.assertEqual(revealed.get(), SECRET)
        with self.assertRaises(TypeError):
            import pickle

            pickle.dumps(revealed)


class CredentialModelTests(IntegrationTestCase):
    def test_set_secret_encrypts_and_reveal_decrypts(self):
        cred = services.set_credential(self.account(), "api_key", SECRET)
        cred.refresh_from_db()
        self.assertNotIn(SECRET, cred.ciphertext)
        self.assertEqual(cred.key_id, secrets.primary_key_id())
        self.assertEqual(cred.reveal().get(), SECRET)
        self.assertEqual(cred.company, self.company)

    def test_str_and_repr_name_the_kind_only(self):
        cred = services.set_credential(self.account(), "api_key", SECRET)
        for text in (str(cred), repr(cred), str(cred.account), repr(cred.account)):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(cred.ciphertext, text)

    def test_saving_without_a_secret_is_refused(self):
        with self.assertRaises(ValidationError):
            IntegrationCredential(account=self.account(), kind="api_key").save()

    def test_one_account_per_provider_per_company(self):
        self.account()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.account()
        self.account(company=self.other)  # another tenant is fine
        with self.assertRaises(ValidationError):
            services.create_account(self.company, "fixture")

    def test_create_account_refuses_an_uninstalled_provider_and_bad_settings(self):
        with self.assertRaises(ValidationError):
            services.create_account(self.company, "nope")
        with self.assertRaises(ValidationError) as ctx:
            services.create_account(self.company, "fixture", settings={"surprise": 1})
        self.assertIn("surprise", str(ctx.exception))

    def test_a_new_credential_revokes_the_previous_one_of_its_kind(self):
        account = self.account()
        first = services.set_credential(account, "api_key", SECRET)
        second = services.set_credential(account, "api_key", SECRET + "-2")
        first.refresh_from_db()
        self.assertIsNotNone(first.revoked_at)
        self.assertIsNone(second.revoked_at)
        self.assertEqual(IntegrationCredential.objects.filter(account=account, revoked_at=None).count(), 1)

    def test_one_active_credential_per_account_owner_kind(self):
        account = self.account()
        services.set_credential(account, "api_key", SECRET)
        dup = IntegrationCredential(account=account, kind="api_key")
        dup.set_secret(SECRET)
        with self.assertRaises(IntegrityError), transaction.atomic():
            dup.save()
        rep = profile(self.company, "rep")
        services.set_credential(account, "oauth_refresh", SECRET, owner=rep)
        grant = IntegrationCredential(account=account, owner=rep, kind="oauth_refresh")
        grant.set_secret(SECRET)
        with self.assertRaises(IntegrityError), transaction.atomic():
            grant.save()

    def test_a_grant_for_another_tenants_profile_is_refused(self):
        account = self.account()
        stranger = profile(self.other, "stranger")
        with self.assertRaises(ValidationError):
            services.set_credential(account, "oauth_refresh", SECRET, owner=stranger)
        cred = IntegrationCredential(account=account, owner=stranger, kind="oauth_refresh")
        cred.set_secret(SECRET)
        with self.assertRaises(ValidationError):
            cred.save()
        self.assertFalse(IntegrationCredential.objects.filter(owner=stranger).exists())

    def test_company_is_copied_from_the_account(self):
        account = self.account()
        cred = IntegrationCredential(account=account, kind="api_key", company=self.other)
        cred.set_secret(SECRET)
        cred.save()
        self.assertEqual(cred.company, self.company)
        out = StringIO()
        call_command("check_tenancy", "--app", "core", stdout=out)

    def test_audit_rows_never_hold_the_secret(self):
        account = services.create_account(self.company, "fixture")
        services.set_credential(account, "api_key", SECRET, subject="ops key")
        services.update_account(account, enabled=True)
        cred = IntegrationCredential.objects.get(account=account)
        rows = list(AuditLog.objects.filter(object_id=account.pk).values_list("changes", flat=True))
        self.assertEqual(len(rows), 3)
        text = repr(rows)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(cred.ciphertext, text)
        self.assertIn("api_key", text)


class RotationTests(IntegrationTestCase):
    def test_rotation_re_encrypts_every_row_and_the_old_key_alone_reads_nothing(self):
        account = self.account()
        rep = profile(self.company, "rep")
        with override_settings(GOLDEN_SECRET_KEYS=[OLD]):
            services.set_credential(account, "api_key", SECRET)
            services.set_credential(account, "webhook_secret", SECRET + "-wh")
            services.set_credential(account, "oauth_refresh", SECRET + "-rt", owner=rep)
            services.set_credential(account, "api_key", SECRET + "-old")  # revokes the first: still rotated
        rows = IntegrationCredential.objects.filter(account=account)
        self.assertEqual(rows.count(), 4)
        before = {c.pk: c.ciphertext for c in rows}
        with override_settings(GOLDEN_SECRET_KEYS=[NEW, OLD]):
            out = StringIO()
            call_command("rotate_secrets", stdout=out)
            self.assertIn("4 credential(s)", out.getvalue())
            self.assertIn("4 from an older key", out.getvalue())
            self.assertNotIn(SECRET, out.getvalue())
        for cred in IntegrationCredential.objects.filter(account=account):
            self.assertEqual(cred.key_id, secrets.key_id(NEW))
            self.assertNotEqual(cred.ciphertext, before[cred.pk])
            with override_settings(GOLDEN_SECRET_KEYS=[NEW]):
                self.assertTrue(cred.reveal().get().startswith(SECRET))
            with override_settings(GOLDEN_SECRET_KEYS=[OLD]):
                with self.assertRaises(secrets.InvalidToken):
                    cred.reveal()

    def test_dry_run_writes_nothing(self):
        account = self.account()
        with override_settings(GOLDEN_SECRET_KEYS=[OLD]):
            cred = services.set_credential(account, "api_key", SECRET)
        with override_settings(GOLDEN_SECRET_KEYS=[NEW, OLD]):
            call_command("rotate_secrets", "--dry-run", stdout=StringIO())
        cred.refresh_from_db()
        self.assertEqual(cred.key_id, secrets.key_id(OLD))

    def test_unreadable_rows_fail_the_command_and_stay(self):
        account = self.account()
        with override_settings(GOLDEN_SECRET_KEYS=[OLD]):
            cred = services.set_credential(account, "api_key", SECRET)
        with override_settings(GOLDEN_SECRET_KEYS=[NEW]):
            with self.assertRaises(SystemExit):
                call_command("rotate_secrets", stdout=StringIO(), stderr=StringIO())
        cred.refresh_from_db()
        self.assertEqual(cred.key_id, secrets.key_id(OLD))


class SystemCheckTests(IntegrationTestCase):
    def ids(self):
        return [e.id for e in secret_keys_check(None)]

    def test_clean_when_every_credential_is_readable(self):
        services.set_credential(self.account(), "api_key", SECRET)
        self.assertEqual(self.ids(), [])

    def test_e040_when_the_key_left_the_list(self):
        with override_settings(GOLDEN_SECRET_KEYS=[OLD]):
            services.set_credential(self.account(), "api_key", SECRET)
        with override_settings(GOLDEN_SECRET_KEYS=[NEW]):
            errors = secret_keys_check(None)
        self.assertEqual([e.id for e in errors], ["core.E040"])
        self.assertIn("1 stored integration credential", errors[0].msg)
        self.assertNotIn(OLD, errors[0].msg + errors[0].hint)

    def test_e040_when_no_key_is_configured(self):
        services.set_credential(self.account(), "api_key", SECRET)
        with override_settings(GOLDEN_SECRET_KEYS=[]):
            errors = secret_keys_check(None)
        self.assertEqual([e.id for e in errors], ["core.E040"])
        self.assertIn("GOLDEN_SECRET_KEYS is empty", errors[0].msg)

    def test_revoked_rows_do_not_fail_the_check(self):
        with override_settings(GOLDEN_SECRET_KEYS=[OLD]):
            cred = services.set_credential(self.account(), "api_key", SECRET)
        services.revoke_credential(cred)
        with override_settings(GOLDEN_SECRET_KEYS=[NEW]):
            self.assertEqual(self.ids(), [])

    def test_e041_on_a_malformed_key(self):
        with override_settings(GOLDEN_SECRET_KEYS=["garbage"]):
            self.assertEqual(self.ids(), ["core.E041"])

    def test_manage_py_check_fails_with_e040(self):
        from django.core.management.base import SystemCheckError

        with override_settings(GOLDEN_SECRET_KEYS=[OLD]):
            services.set_credential(self.account(), "api_key", SECRET)
        with override_settings(GOLDEN_SECRET_KEYS=[NEW]):
            with self.assertRaises(SystemCheckError) as ctx:
                call_command("check", stdout=StringIO(), stderr=StringIO())
        self.assertIn("core.E040", str(ctx.exception))
