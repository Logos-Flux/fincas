"""Production refuses to deploy without its own secrets (core.E060), and
``generate_secrets`` prints a usable set."""

from io import StringIO

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from core.checks import DEPLOY_SECRETS, deploy_secrets_check

FERNET = Fernet.generate_key().decode()
ALL_SET = {
    "API_JWT_SIGNING_KEY": "a-dedicated-signing-secret",
    "LLMHUB_FERNET_KEY": FERNET,
    "GOLDEN_SECRET_KEYS": [FERNET],
}


class DeploySecretsCheckTests(SimpleTestCase):
    @override_settings(GOLDEN_REQUIRE_DEPLOY_SECRETS=False, API_JWT_SIGNING_KEY="",
                       LLMHUB_FERNET_KEY="", GOLDEN_SECRET_KEYS=[])
    def test_off_outside_production(self):
        self.assertEqual(deploy_secrets_check(None), [])

    @override_settings(GOLDEN_REQUIRE_DEPLOY_SECRETS=True, **ALL_SET)
    def test_quiet_when_every_secret_is_set(self):
        self.assertEqual(deploy_secrets_check(None), [])

    @override_settings(GOLDEN_REQUIRE_DEPLOY_SECRETS=True, **{**ALL_SET, "API_JWT_SIGNING_KEY": "",
                                                              "GOLDEN_SECRET_KEYS": []})
    def test_one_error_names_every_missing_secret(self):
        errors = deploy_secrets_check(None)
        self.assertEqual([e.id for e in errors], ["core.E060"])
        self.assertIn("API_JWT_SIGNING_KEY", errors[0].msg)
        self.assertIn("GOLDEN_SECRET_KEYS", errors[0].msg)
        self.assertNotIn("LLMHUB_FERNET_KEY", errors[0].msg)
        self.assertIn("generate_secrets", errors[0].hint)

    def test_production_settings_turn_it_on(self):
        import golden_erp.settings.base as base
        import golden_erp.settings.prod as prod

        self.assertIs(base.GOLDEN_REQUIRE_DEPLOY_SECRETS, False)
        self.assertIs(prod.GOLDEN_REQUIRE_DEPLOY_SECRETS, True)


class GenerateSecretsTests(SimpleTestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("generate_secrets", *args, stdout=out)
        return dict(line.split("=", 1) for line in out.getvalue().splitlines())

    def test_prints_every_required_secret_usable_as_is(self):
        values = self._run()
        self.assertLessEqual(set(DEPLOY_SECRETS) | {"DJANGO_SECRET_KEY"}, set(values))
        Fernet(values["LLMHUB_FERNET_KEY"].encode())
        Fernet(values["GOLDEN_SECRET_KEYS"].encode())
        self.assertGreaterEqual(len(values["API_JWT_SIGNING_KEY"]), 48)
        self.assertEqual(len(set(values.values())), len(values))

    def test_fresh_values_every_run(self):
        self.assertNotEqual(self._run()["API_JWT_SIGNING_KEY"], self._run()["API_JWT_SIGNING_KEY"])

    def test_not_blocked_by_core_e060(self):
        # The command is how you fix E060, so it must skip system checks
        # (call_command skips them anyway, so assert the attribute itself).
        from core.management.commands.generate_secrets import Command

        self.assertEqual(Command.requires_system_checks, [])

    def test_only(self):
        self.assertEqual(list(self._run("--only", "API_JWT_SIGNING_KEY")), ["API_JWT_SIGNING_KEY"])


class EachSecretAloneTests(SimpleTestCase):
    def test_each_secret_missing_alone_is_named_alone(self):
        empty = {"API_JWT_SIGNING_KEY": "", "LLMHUB_FERNET_KEY": "", "GOLDEN_SECRET_KEYS": []}
        for name in DEPLOY_SECRETS:
            with self.subTest(name=name), override_settings(GOLDEN_REQUIRE_DEPLOY_SECRETS=True,
                                                            **{**ALL_SET, name: empty[name]}):
                [error] = deploy_secrets_check(None)
                self.assertIn(name, error.msg)
                for other in DEPLOY_SECRETS:
                    if other != name:
                        self.assertNotIn(other, error.msg)
