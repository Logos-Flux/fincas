"""Shared apps import cleanly with no add-on installed.

golden-erp ships its shared apps without any deployment's add-ons, so no module
in them may import ``forge`` (the downstream product-intake add-on) or a
``*_price_scout`` add-on. Where shared code needs add-on behaviour it goes through
``core/hook_registry.py`` instead (docs/plugins.md, "Hook points").

A child interpreter configures Django from the test settings with no plugin
apps or plugins at all, imports every non-test module of every installed app
in this repo, and reports which add-on modules ended up in ``sys.modules``.
A module that imports a missing add-on fails here as an import error, which is
how the products commands used to crash.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

REPO_DIR = Path(__file__).resolve().parents[2]

#: Top-level add-on packages, matched exactly or as a ``<prefix>_<name>`` suffix
#: (a deployment's ``<brand>_price_scout``).
ABSENT = ("forge", "price_scout")

SCRIPT = r"""
import importlib, json, os, pkgutil, sys

absent = json.loads(sys.argv[1])
os.environ.setdefault("DJANGO_SECRET_KEY", "forge-absent-probe")
import config.settings.test as base

names = {k: getattr(base, k) for k in dir(base) if k.isupper()}
plugin_apps = set(names.get("GOLDEN_PLUGIN_APPS") or [])
names["INSTALLED_APPS"] = [a for a in names["INSTALLED_APPS"] if a not in plugin_apps]
names["GOLDEN_PLUGINS"] = []
names["GOLDEN_PLUGIN_APPS"] = []

from django.conf import settings
settings.configure(**names)
import django
django.setup()
from django.apps import apps

walked, failed = [], {}
for cfg in apps.get_app_configs():
    # Only this repo's top-level app packages, never a venv's third-party apps.
    if os.path.dirname(cfg.path) != os.getcwd():
        continue
    for mod in pkgutil.walk_packages([cfg.path], prefix=cfg.name + "."):
        parts = mod.name.split(".")
        if "tests" in parts or "migrations" in parts or parts[-1].startswith("test"):
            continue
        try:
            importlib.import_module(mod.name)
            walked.append(mod.name)
        except Exception as exc:
            failed[mod.name] = repr(exc)[:300]

from core import hook_registry
print(json.dumps({
    "walked": walked,
    "failed": failed,
    "leaked": sorted(
        m for m in sys.modules
        if any(m.split(".")[0] == a or m.split(".")[0].endswith("_" + a) for a in absent)
    ),
    "hooks": {name: hook_registry.get(name) is not None for name in hook_registry.HOOKS},
}))
"""


class SharedAppsImportWithoutAddonsTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = {**os.environ, "PYTHONPATH": str(REPO_DIR)}
        env.pop("DJANGO_SETTINGS_MODULE", None)
        proc = subprocess.run(
            [sys.executable, "-c", SCRIPT, json.dumps(ABSENT)],
            cwd=REPO_DIR, env=env, capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise AssertionError(f"probe crashed:\n{proc.stderr[-3000:]}")
        cls.result = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_the_probe_walked_the_shared_apps(self):
        walked = set(self.result["walked"])
        self.assertFalse(
            {m for m in walked if m.split(".")[0] in ("rest_framework", "anymail", "django")}
        )
        for module in (
            "core.hook_registry",
            "pricing.views",
            "products.management.commands.set_structured_specs",
            "products.management.commands.audit_mpn_coverage",
        ):
            self.assertIn(module, walked)
        self.assertGreater(len(walked), 100)

    def test_every_shared_module_imports(self):
        self.assertEqual(self.result["failed"], {})

    def test_no_forge_or_price_scout_module_is_imported(self):
        self.assertEqual(self.result["leaked"], [])

    def test_hooks_are_unprovided(self):
        self.assertEqual(set(self.result["hooks"].values()), {False})
