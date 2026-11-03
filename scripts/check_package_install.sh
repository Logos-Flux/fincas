#!/usr/bin/env bash
# Prove fincas works as an installed package (docs/packaging.md).
#
#  1. builds the wheel from a clean copy of the working tree (tracked files plus
#     untracked-but-not-ignored ones; never a stale build/ dir);
#  2. installs ONLY that wheel into a fresh venv — no requirements file, so
#     [project].dependencies must be complete;
#  3. outside the repo, creates a minimal downstream project whose own
#     top-level package is called `config` (as the downstream deployment's is),
#     with settings = golden's base + SQLite + compose_installed_apps, and urls
#     including golden_erp.urls;
#  4. runs `django-admin check`, `migrate`, `collectstatic`, then GETs
#     /erp/login/ through the test client and asserts the packaged template
#     rendered — loaded from site-packages, not from this checkout;
#  5. with the shipped CRM adapters listed in GOLDEN_PLUGINS (as the downstream
#     lists them), imports each `golden_erp.addons.<x>.plugin` and asserts it came from
#     the venv, its adapter is registered, and an adapter fixture shipped.
#
# Exits non-zero on any failure. Needs network (pip downloads the deps) and
# WeasyPrint's system libraries (pango/cairo), like the test suite.
#
#   bash scripts/check_package_install.sh      # or: make package-check
#   KEEP_WORK=1 bash scripts/check_package_install.sh   # keep the temp dir
#   PYTHON=python3.12 bash scripts/check_package_install.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/golden-pkg-check.XXXXXX")"
if [ "${KEEP_WORK:-0}" = "1" ]; then
  echo "work dir kept: $WORK"
else
  trap 'rm -rf "$WORK"' EXIT
fi

step() { printf '\n==> %s\n' "$*"; }

step "copy working tree (git ls-files -co --exclude-standard)"
mkdir -p "$WORK/src"
(cd "$REPO" && git ls-files -z -co --exclude-standard | while IFS= read -r -d '' f; do
  [ -e "$f" ] && printf '%s\0' "$f"   # skip files deleted but not yet staged
done | tar --null -T - -cf -) | tar -xf - -C "$WORK/src"

step "build wheel"
"$PY" -m pip wheel "$WORK/src" --no-deps --quiet -w "$WORK/dist"
WHEEL="$(ls "$WORK"/dist/fincas-*.whl)"
echo "$WHEEL"

step "wheel must not ship golden's project shell or non-package dirs"
"$PY" - "$WHEEL" <<'EOF'
import sys
import zipfile

names = zipfile.ZipFile(sys.argv[1]).namelist()
tops = {n.split("/")[0] for n in names}
banned = {"config", "storefront", "scripts", "docs", "requirements", "brainstorming"}
leaked = sorted(tops & banned)
assert not leaked, f"wheel ships {leaked}"
for required in ("golden_erp/templates/registration/login.html",
                 "golden_erp/static/assets/css/themes/golden.css",
                 "golden_erp/settings/base.py",
                 "golden_erp/settings/prod.py",
                 "golden_erp/urls.py",
                 "core/migrations/0001_initial.py",
                 "golden_erp/addons/__init__.py",
                 "addons/__init__.py",  # deprecated alias (#120); remove in v0.6.0
                 "golden_erp/addons/clickup/plugin.py",
                 "golden_erp/addons/clickup/fixtures/erase/case.json"):
    assert required in names, f"wheel is missing {required}"
print(f"ok: {len(names)} files, top-level: {', '.join(sorted(t for t in tops if '.dist-info' not in t))}")
EOF

step "fresh venv + install the wheel alone"
"$PY" -m venv "$WORK/venv"
VPY="$WORK/venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet "$WHEEL"

step "create a downstream-style project outside the repo"
PROJ="$WORK/downstream"
mkdir -p "$PROJ/config"
: > "$PROJ/config/__init__.py"
cat > "$PROJ/config/settings.py" <<'EOF'
from pathlib import Path

from core.plugin_apps import compose_installed_apps
from golden_erp.settings.base import *  # noqa: F401,F403

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "package-check-not-a-secret"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
STATIC_ROOT = BASE_DIR / "staticfiles"
ROOT_URLCONF = "config.urls"

# The shipped adapters, by the dotted paths docs/crm tells the downstream to use.
GOLDEN_PLUGINS = [
    "golden_erp.addons.aircall.plugin.AircallPlugin",
    "golden_erp.addons.gmail.plugin.GmailPlugin",
    "golden_erp.addons.clickup.plugin.ClickUpPlugin",
    "golden_erp.addons.klaviyo.plugin.KlaviyoPlugin",
    "golden_erp.addons.google_ads.plugin.GoogleAdsPlugin",
    "golden_erp.addons.hubspot_export.plugin.HubSpotExportPlugin",
]
GOLDEN_PLUGIN_APPS = []
INSTALLED_APPS = compose_installed_apps(INSTALLED_APPS, GOLDEN_PLUGIN_APPS)
EOF
cat > "$PROJ/config/urls.py" <<'EOF'
from django.urls import include, path

urlpatterns = [path("", include("golden_erp.urls"))]
EOF

cd "$PROJ"
# Nothing from the checkout on the path; the base settings fail fast without
# DJANGO_SECRET_KEY in the environment (production contract), so provide it.
unset PYTHONPATH VIRTUAL_ENV DJANGO_SETTINGS_MODULE
export PYTHONPATH="$PROJ" DJANGO_SETTINGS_MODULE=config.settings
export DJANGO_SECRET_KEY="package-check-not-a-secret"
ADMIN="$WORK/venv/bin/django-admin"

step "golden_erp.settings.prod imports from the wheel (production overlay start)"
DATABASE_URL="sqlite:///$PROJ/prod-check.db" "$VPY" -c '
import golden_erp.settings.prod as prod
assert prod.DEBUG is False and prod.SESSION_COOKIE_SECURE and prod.CSRF_COOKIE_SECURE
assert prod.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
assert prod.DATABASES["default"]["ENGINE"].endswith("sqlite3")
print("ok: prod settings import; HSTS", prod.SECURE_HSTS_SECONDS)
'

step "django-admin check"
"$ADMIN" check

step "django-admin migrate"
"$ADMIN" migrate --noinput -v 0
"$ADMIN" migrate --check -v 0   # nothing unapplied

step "django-admin collectstatic"
"$ADMIN" collectstatic --noinput -v 0
for f in assets/css/themes/golden.css assets/css/tabler-bindings.css chat/chat.css; do
  [ -f "$PROJ/staticfiles/$f" ] || { echo "collectstatic did not collect $f" >&2; exit 1; }
done
echo "ok: $(find "$PROJ/staticfiles" -type f | wc -l) files collected"

step "GET /erp/login/ through the test client"
"$VPY" - "$WORK/venv" <<'EOF'
import sys
from pathlib import Path

import django
from django.test.utils import setup_test_environment

venv = Path(sys.argv[1]).resolve()
django.setup()
setup_test_environment()

import config
import core
import golden_erp
from django.test import Client

for mod in (golden_erp, core):
    where = Path(mod.__file__).resolve()
    assert venv in where.parents, f"{mod.__name__} imported from {where}, not the installed wheel"
assert Path(config.__file__).resolve().parent == Path.cwd() / "config", "downstream config shadowed"
print(f"golden_erp {golden_erp.__version__} from {Path(golden_erp.__file__).parent}")

response = Client().get("/erp/login/")
assert response.status_code == 200, f"/erp/login/ -> {response.status_code}"
used = {t.name: t.origin.name for t in response.templates if t.origin}
origin = used.get("registration/login.html")
assert origin, f"registration/login.html not rendered; templates: {sorted(used)}"
assert venv in Path(origin).resolve().parents, f"login template came from {origin}"
body = response.content.decode()
assert 'name="password"' in body and "csrfmiddlewaretoken" in body, "login form missing"
assert "assets/css/themes/" in body, "ERP theme stylesheet not linked"
print(f"ok: 200, rendered {origin}")
EOF

step "CRM adapters import from the wheel and register"
"$VPY" - "$WORK/venv" <<'EOF'
import importlib
import sys
from pathlib import Path

import django

venv = Path(sys.argv[1]).resolve()
django.setup()

from core.plugins.registry import plugin_registry

for name in ("golden_erp.addons.aircall.plugin", "golden_erp.addons.gmail.plugin", "golden_erp.addons.clickup.plugin",
             "golden_erp.addons.klaviyo.plugin", "golden_erp.addons.google_ads.plugin", "golden_erp.addons.hubspot_export",
             "golden_erp.addons.hubspot_export.plugin"):
    mod = importlib.import_module(name)
    where = Path(mod.__file__).resolve()
    assert venv in where.parents, f"{name} imported from {where}, not the installed wheel"

live = set(plugin_registry.get_crm_adapters())
assert {"aircall", "gmail", "clickup", "klaviyo", "google_ads"} <= live, f"live CRM adapters: {sorted(live)}"
export = set(plugin_registry.get_crm_export_adapters())
assert "hubspot" in export, f"export adapters: {sorted(export)}"

import golden_erp.addons.clickup

fixture = Path(golden_erp.addons.clickup.__file__).parent / "fixtures" / "erase" / "case.json"
assert fixture.is_file(), f"adapter fixture not shipped: {fixture}"
print(f"ok: live {sorted(live)}, export {sorted(export)}, fixture {fixture}")

# The deprecated top-level `addons` alias ships through 0.5.x (#120; remove
# in v0.6.0): same module objects, never a second copy.
import warnings

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    import addons.clickup.plugin as old_plugin
assert old_plugin is importlib.import_module("golden_erp.addons.clickup.plugin"), "alias made a copy"
assert any(issubclass(w.category, DeprecationWarning) for w in caught), "alias did not warn"
print("ok: deprecated addons alias resolves to golden_erp.addons")
EOF

printf '\nPACKAGE CHECK PASSED\n'
