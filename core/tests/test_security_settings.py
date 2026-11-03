"""Release hardening 6.4: secrets and settings.

- `manage.py check --deploy` on `golden_erp.settings.prod` with every
  required secret set reports nothing beyond the recorded allow-list (run
  in a subprocess with a minimal environment, about 3 s);
- `scripts/check_bundle_secrets.sh` (the CI step after the storefront
  build) flags server-side key material in a built bundle and passes a
  clean one.

Cited, not repeated: `core.E060` refuses to migrate on production settings
without the deploy secrets, and `generate_secrets` prints every one of
them (`core/tests/test_deploy_secrets.py`); the tracked tree and
`storefront/dist` carry no key material and the storefront source never
reads an LLM key (`core/tests/test_m3_secrets_headers.py`, S7); production
security flags and headers (`SecurityHeadersTests` there, S11). The Docker
image is grepped with `docker history` in the cutover rehearsal
(`docs/pin/cutover-runbook.md`); no script duplicates that here.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, tag

ROOT = Path(settings.BASE_DIR)
SCRIPT = ROOT / "scripts" / "check_bundle_secrets.sh"

#: Warnings `check --deploy` may report on production settings. Anything
#: else (a `security.W*` in particular) fails the test: fix the setting or
#: add the id here with a reason.
ALLOWED_DEPLOY_WARNINGS = {
    # drf-spectacular schema hints on APIViews without a serializer_class;
    # documentation quality, not a deployment setting.
    "drf_spectacular.W001",
    "drf_spectacular.W002",
}


def _generated_secrets() -> dict[str, str]:
    out = io.StringIO()
    call_command("generate_secrets", stdout=out)
    pairs = (line.split("=", 1) for line in out.getvalue().splitlines() if "=" in line)
    return {name.strip(): value.strip() for name, value in pairs}


@tag("slow")
class CheckDeployTests(SimpleTestCase):
    def test_check_deploy_is_clean_beyond_the_allow_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": tmp,
                "DJANGO_SETTINGS_MODULE": "golden_erp.settings.prod",
                "ALLOWED_HOSTS": "erp.example.com",
                "DATABASE_URL": f"sqlite:///{tmp}/check.sqlite3",
                "CSRF_TRUSTED_ORIGINS": "https://erp.example.com",
                # prod sets SECURE_PROXY_SSL_HEADER, so a deployment names its
                # proxy or core.W114 says the throttles share one bucket (F-087).
                "TRUSTED_PROXIES": "203.0.113.1",
                **_generated_secrets(),
            }
            # actions/setup-python's interpreter links its own libpython
            # through LD_LIBRARY_PATH; without it the binary loads the system
            # libpython (prefix /usr, no Django) on the self-hosted runner.
            if "LD_LIBRARY_PATH" in os.environ:
                env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
            result = subprocess.run(
                [sys.executable, "manage.py", "check", "--deploy"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
            )
        output = result.stdout + result.stderr
        ids = set(re.findall(r"\((\w+\.[EWC]\d+)\)", output))
        self.assertEqual(result.returncode, 0, output[-2000:])
        self.assertFalse([i for i in ids if i.startswith("security.")], output[-2000:])
        self.assertLessEqual(ids, ALLOWED_DEPLOY_WARNINGS, output[-2000:])
        self.assertNotIn("Traceback", output)


class BundleSecretsScriptTests(SimpleTestCase):
    def _run(self, directory):
        return subprocess.run(["bash", str(SCRIPT), str(directory)], capture_output=True, text=True)

    def test_script_is_executable_and_refuses_a_missing_dir(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK), "chmod +x scripts/check_bundle_secrets.sh")
        self.assertEqual(self._run(Path("/nonexistent/dist")).returncode, 2)

    def test_clean_bundle_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "q-abc.js").write_text('const anon = "public-anon-key"; export const VITE_API_BASE_URL = "/api";')
            Path(tmp, "q-abc.js.map").write_text("DJANGO_SECRET_KEY")  # maps are ignored
            result = self._run(tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_server_side_key_material_fails(self):
        # Built from parts so the secret scanner never sees a credential-shaped literal.
        planted = {
            "names.js": "fetch(url, {headers: {Authorization: " + "SUPABASE_SERVICE_" + "ROLE_KEY}})",
            "stripe.js": "const k = '" + "sk_" + "live_" + "a" * 12 + "';",
            "pem.txt": "-----BEGIN " + "PRIVATE KEY-----\nabc\n",
            "fernet.js": "process.env." + "LLMHUB_" + "FERNET_KEY",
        }
        for name, body in planted.items():
            with self.subTest(file=name), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "clean.js").write_text("export default 1;")
                Path(tmp, name).write_text(body)
                result = self._run(tmp)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(name, result.stdout)

    def test_ci_runs_the_check_after_the_storefront_build(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        build = workflow.index("bun run build\n")
        self.assertIn("scripts/check_bundle_secrets.sh", workflow[build:])
