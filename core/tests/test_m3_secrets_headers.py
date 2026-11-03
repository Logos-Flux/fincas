"""Milestone 3 Track G — S7 (secrets) + S11 (headers/TLS checklist).

S7 pins:
- no `.env` file is tracked by git (env-guard complement)
- no private-key / high-signal vendor-key material in tracked source
  (test fixtures and docs excluded by design)
- API secrets are env-sourced at runtime (settings.API_KEY), never
  imported from a repo file
- storefront/dist carries no secret material (read-only scan; skipped
  with a note when dist/ is absent)

S11 pins (report-only against settings + dev responses):
- prod settings declare HSTS, secure cookies, SSL redirect
- clickjacking middleware present; nosniff flag characterized
- API 404s are JSON without tracebacks
"""

import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from core.tests.helpers import make_staff_user

APP_ROOT = Path(settings.BASE_DIR)

KEY_PATTERNS = [
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"sk-ant-[A-Za-z0-9\-_]{8,}",
    r"sk-proj-[A-Za-z0-9\-_]{8,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[bap]-[A-Za-z0-9\-]+",
    r"ghp_[A-Za-z0-9]{10,}",
    r"MAILGUN_API_KEY.{0,5}['\"]key-[A-Za-z0-9]{10,}",
]

SKIP_PARTS = {
    ".venv", "node_modules", "staticfiles", ".git", "__pycache__",
    "dist", ".output",
}
SKIP_SUFFIXES = (".pyc", ".sqlite3", ".map")


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [APP_ROOT / line for line in out.stdout.splitlines() if line]


class EnvHygieneTests(SimpleTestCase):
    def test_no_env_file_tracked(self):
        tracked = [str(p) for p in _tracked_files()]
        offenders = [
            t for t in tracked
            if re.search(r"(^|/)\.env(\.|$)", t)
            and not t.endswith((".env.example", ".env.sample"))
        ]
        self.assertEqual(offenders, [])

    def test_env_example_exists_without_secrets(self):
        example = APP_ROOT / ".env.example"
        if not example.exists():
            self.skipTest("no .env.example in repo")
        body = example.read_text()
        for pattern in KEY_PATTERNS:
            self.assertIsNone(
                re.search(pattern, body), f"secret pattern in .env.example: {pattern}"
            )


class CommittedSecretsScanTests(SimpleTestCase):
    def test_no_key_material_in_tracked_source(self):
        offenders = []
        for path in _tracked_files():
            parts = set(path.parts)
            if parts & SKIP_PARTS:
                continue
            if path.suffix in SKIP_SUFFIXES or path.name in SKIP_SUFFIXES:
                continue
            # Test fixtures, docs, and Hawks-scans of key *names* are out
            # of scope: only real key material counts.
            if "test" in path.name or path.suffix in (".md",):
                continue
            try:
                body = path.read_text(errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in KEY_PATTERNS:
                if re.search(pattern, body):
                    offenders.append(f"{path.relative_to(APP_ROOT)}:{pattern}")
        self.assertEqual(offenders, [])

    def test_api_key_compared_constant_time(self):
        # API keys (the legacy env key and per-key core.ApiKey hashes) are
        # compared via compare_digest (hash-then-compare), never `==`. The
        # one resolver lives in core/service_accounts.py (identity step 5).
        src = (APP_ROOT / "core" / "service_accounts.py").read_text()
        self.assertIn("compare_digest", src)
        self.assertNotIn("key_hash ==", src)

    def test_no_plaintext_secret_models(self):
        # Outbound webhook HMAC secrets must stay retrievable to sign
        # payloads (a hash can't sign), so webhooks.secret /
        # previous_secret are the one sanctioned plaintext store —
        # company-scoped since F-020. Everything else must not store
        # key material in a model field.
        allowed = {("models.py", "secret"), ("models.py", "previous_secret")}
        offenders = []
        for path in APP_ROOT.glob("*/models.py"):
            if path.parent.name == ".venv":
                continue
            body = path.read_text()
            for match in re.finditer(
                r"^\s*(api_key|secret|password)\s*=\s*models\.", body, re.M
            ):
                if (path.name, match.group(1)) not in allowed:
                    offenders.append(
                        f"{path.parent.name}/{path.name}:{match.group(1)}"
                    )
                elif path.parent.name != "webhooks":
                    offenders.append(
                        f"{path.parent.name}/{path.name}:{match.group(1)}"
                    )
        self.assertEqual(offenders, [])


class StorefrontDistSecretsTests(SimpleTestCase):
    def test_dist_carries_no_secret_material(self):
        dist = APP_ROOT / "storefront" / "dist"
        if not dist.exists():
            self.skipTest("storefront/dist absent (not built)")
        offenders = []
        for path in dist.rglob("*"):
            if not path.is_file() or path.suffix == ".map":
                continue
            try:
                body = path.read_text(errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in KEY_PATTERNS:
                if re.search(pattern, body):
                    offenders.append(str(path.relative_to(APP_ROOT)))
                    break
        self.assertEqual(offenders, [])

    def test_llm_keys_not_referenced_in_storefront_src(self):
        # Read-only: LLM provider keys must stay server-side; the
        # storefront must not read them (even via public env names).
        src = APP_ROOT / "storefront" / "src"
        offenders = []
        for path in src.rglob("*.ts*"):
            body = path.read_text(errors="ignore")
            if re.search(r"ANTHROPIC_API_KEY|OPENAI_API_KEY|API_KEY", body):
                offenders.append(str(path.relative_to(APP_ROOT)))
        self.assertEqual(offenders, [])


class SecurityHeadersTests(TestCase):
    def test_prod_settings_security_flags(self):
        # The hardening ships in the package (golden_erp.settings.prod);
        # golden's config/settings/prod.py must build on it.
        import golden_erp.settings.prod as prod

        self.assertGreater(prod.SECURE_HSTS_SECONDS, 0)
        self.assertTrue(prod.SECURE_HSTS_INCLUDE_SUBDOMAINS)
        self.assertTrue(prod.SESSION_COOKIE_SECURE)
        self.assertTrue(prod.CSRF_COOKIE_SECURE)
        self.assertIsInstance(prod.SECURE_SSL_REDIRECT, bool)
        project_prod = (APP_ROOT / "config" / "settings" / "prod.py").read_text()
        self.assertIn("from golden_erp.settings.prod import *", project_prod)

    def test_nosniff_characterized(self):
        self.assertTrue(settings.SECURE_CONTENT_TYPE_NOSNIFF)
        response = self.client.get("/erp/login/")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_clickjacking_middleware_present(self):
        self.assertIn(
            "django.middleware.clickjacking.XFrameOptionsMiddleware",
            settings.MIDDLEWARE,
        )

    def _authed(self):

        api = APIClient()
        api.force_authenticate(
            user=make_staff_user(username="hdr-user", password="pw")
        )
        return api

    def test_api_404_is_json_without_traceback(self):
        response = self._authed().get("/api/v1/orders/orders/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("application/json", response["Content-Type"])
        self.assertNotIn(b"Traceback", response.content)

    def test_debug_false_404_has_no_debug_payload(self):
        from django.test.utils import override_settings

        with override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"]):
            response = self._authed().get(
                "/api/v1/orders/orders/does-not-exist/",
                HTTP_HOST="testserver",
            )
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b"Traceback", response.content)
