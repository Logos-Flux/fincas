"""Guard the production compose file.

Static files: the image runs `collectstatic` at build time into STATIC_ROOT. A
named volume mounted over that path is seeded only once, when it is first
created, so every later deploy kept serving the first deploy's CSS/JS: staging
served September's static files into October, and the new ERP themes 404'd
(2026-10-04).

Environment: the web service used to list the variables it passed, so every
setting the list forgot never reached Django. With TRUSTED_PROXIES and
GOLDEN_PORTAL_COMPANY missing, `parties.E001` failed every management command on
staging (16 Companies), including the entrypoint's migrate, and web
crash-looped (2026-10-09). The web service now takes `.env` whole; these tests
check that every name the settings read is still covered.
"""

import re
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
COMPOSE = ROOT / "docker-compose.prod.yml"
# Every module config.settings.prod executes that reads the environment.
SETTINGS_FILES = [
    ROOT / "golden_erp" / "settings" / "base.py",
    ROOT / "golden_erp" / "settings" / "prod.py",
    ROOT / "config" / "settings" / "base.py",
    ROOT / "config" / "settings" / "prod.py",
]
ENV_READ = re.compile(
    r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""
)
# `${NAME:-}` / `${NAME-}`: sets the variable to "" when the operator left it
# unset, which overrides the settings default instead of keeping it.
EMPTY_DEFAULT = re.compile(r"^\$\{[A-Z0-9_]+:?-\}$")


def _services():
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _web():
    return _services()["web"]


def _env_files(service) -> list[str]:
    raw = service.get("env_file") or []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    return [e["path"] if isinstance(e, dict) else e for e in raw]


def _environment(service) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, list):
        raw = dict(item.split("=", 1) if "=" in item else (item, "") for item in raw)
    return {k: str(v) for k, v in raw.items()}


def _settings_env_names() -> set[str]:
    names = set()
    for path in SETTINGS_FILES:
        for m in ENV_READ.finditer(path.read_text()):
            names.add(m.group(1) or m.group(2))
    return names


class ProdComposeTests(SimpleTestCase):
    def test_nothing_is_mounted_over_static_root(self):
        web = _web()
        targets = [str(v).split(":")[1] for v in web.get("volumes", []) if ":" in str(v)]
        self.assertNotIn("/app/staticfiles", targets)

    def test_settings_env_names_are_found(self):
        # A regex that silently matched nothing would pass the coverage test.
        names = _settings_env_names()
        self.assertGreater(len(names), 40)
        self.assertIn("DJANGO_SECRET_KEY", names)

    def test_web_takes_dot_env_whole(self):
        self.assertIn(".env", _env_files(_web()))

    def test_every_setting_env_name_reaches_web(self):
        web = _web()
        explicit = _environment(web)
        whole_env_file = ".env" in _env_files(web)
        missing = sorted(n for n in _settings_env_names() if n not in explicit and not whole_env_file)
        self.assertEqual(missing, [], "settings read these but docker-compose.prod.yml never passes them")

    def test_no_empty_default_passthroughs(self):
        bad = sorted(k for k, v in _environment(_web()).items() if EMPTY_DEFAULT.match(v))
        self.assertEqual(bad, [], "an unset .env value would override the settings default with ''")

    def test_portal_tenant_and_proxies_come_from_dot_env(self):
        names = _settings_env_names()
        web = _web()
        explicit = _environment(web)
        for name in ("TRUSTED_PROXIES", "GOLDEN_PORTAL_COMPANY"):
            with self.subTest(name=name):
                self.assertIn(name, names)
                self.assertIn(".env", _env_files(web))
                # An explicit entry would shadow the .env value.
                self.assertNotIn(name, explicit)


class OutboxServiceTests(SimpleTestCase):
    """The outbox worker (docs/crm/phase6-adapters.md 1.7): without it webhook
    rows only accumulate as pending."""

    def test_an_outbox_service_runs_process_outbox_loop(self):
        service = _services().get("outbox")
        self.assertIsNotNone(service, "docker-compose.prod.yml has no outbox service")
        command = service["command"]
        command = command if isinstance(command, list) else command.split()
        self.assertEqual(command, ["python", "manage.py", "process_outbox", "--loop"])

    def test_it_runs_the_web_image_with_the_web_settings(self):
        services = _services()
        outbox, web = services["outbox"], services["web"]
        self.assertEqual(outbox["build"], web["build"])
        self.assertIn(".env", _env_files(outbox))
        env, web_env = _environment(outbox), _environment(web)
        for name in ("DJANGO_SETTINGS_MODULE", "DATABASE_URL", "DJANGO_SECRET_KEY"):
            with self.subTest(name=name):
                self.assertEqual(env[name], web_env[name])
        self.assertEqual(env.get("AUTO_MIGRATE"), "0")  # web migrates; the worker never races it
        self.assertFalse(outbox.get("ports"))

    def test_it_waits_for_web_to_be_healthy(self):
        # web migrates at start-up; with service_started the worker raced the
        # migration and crashed once on a missing core_outboxevent (2026-10-14).
        services = _services()
        self.assertEqual(services["outbox"]["depends_on"]["web"]["condition"], "service_healthy")
        test = services["web"].get("healthcheck", {}).get("test")
        self.assertTrue(test, "web needs a healthcheck for outbox to wait on")
        cmd = " ".join(test) if isinstance(test, list) else test
        self.assertIn("127.0.0.1:8000", cmd)
        # A bare `$h` is interpolated by compose on the host (to ""), silently;
        # container-side variables must be written `$$`.
        self.assertIsNone(re.search(r"(?<!\$)\$(?!\$)[{A-Za-z_]", cmd), cmd)
