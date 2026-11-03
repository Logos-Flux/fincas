"""Test-only Django settings.

Inherits from dev.py but:
1. Disables migrations so fresh test DBs are synced from the current
   model state instead of replaying every migration file.
2. Uses a file-based temp SQLite for the test DB instead of plain
   :memory: because the chat streaming view uses sync_to_async to
   bridge the ORM from async-land across threads.
"""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("DJANGO_SECRET_KEY", "django-insecure-golden-erp-test-only")

from .dev import *  # noqa: E402,F401,F403


class _DisableMigrations:
    def __contains__(self, item) -> bool:
        return True

    def __getitem__(self, item) -> None:
        return None


MIGRATION_MODULES = _DisableMigrations()

_TEST_DB_PATH = Path(tempfile.gettempdir()) / "golden_erp_test.sqlite3"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_TEST_DB_PATH),
    }
}

# Opt-in Postgres: TEST_DATABASE_URL=postgres://... runs the same suite on
# Postgres (CI job "Test Suite (Postgres 17)"). A separate variable from
# DATABASE_URL on purpose: base.py loads .env, and a developer's dev database
# must never become the test target by accident. Django creates and drops
# test_<name> on that server; the named database itself is not touched.
_test_db_url = os.environ.get("TEST_DATABASE_URL", "")
if _test_db_url:
    import dj_database_url

    DATABASES = {"default": dj_database_url.parse(_test_db_url, conn_max_age=0)}

# llmhub: no in-process settings cache in tests — every read sees the test's DB
# state, never a snapshot cached by a previous test (TestCase rolls back without
# firing post_delete, so a cached snapshot could outlive its rows).
LLMHUB_CACHE_TTL_S = 0

# Portal self-registration throttle off: suites create many accounts from one
# test IP. core.tests.test_portal_auth re-enables it to test it.
GOLDEN_PORTAL_SIGNUP_RATE = None

# A party-sync error fails the test that hit it instead of being logged
# (parties.sync.run_guarded).
GOLDEN_PARTY_SYNC_STRICT = True
# Likewise a lead-intake error (crm.intake.run_intake).
GOLDEN_CRM_INTAKE_STRICT = True

# No live sockets (docs/plugins.md section 6): the runner patches socket so a
# connection to anything but loopback, a Unix socket or the DB/cache hosts
# raises core.testing.no_network.LiveSocketError.
TEST_RUNNER = "core.testing.no_network.NoNetworkRunner"

# Connected accounts (docs/crm/phase6-adapters.md 5.1): one fixed, test-only
# Fernet key, so fixture credentials encrypt and decrypt. Never a real key.
GOLDEN_SECRET_KEYS = ["Z29sZGVuLXRlc3Qtb25seS1mZXJuZXQta2V5LTAwMDE="]
