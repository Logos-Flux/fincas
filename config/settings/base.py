"""golden's own project settings: the packaged base plus what is project-local.

The real base settings live in ``golden_erp.settings.base`` (shipped in the
wheel). This module is golden's runnable project shell — the same shape a
downstream deployment's overlay has — and is never shipped.
"""

from pathlib import Path

from dotenv import load_dotenv

# Before the import: the packaged base reads the environment at import time.
load_dotenv()

from golden_erp.settings.base import *  # noqa: E402,F401,F403

# The repository root (manage.py, db.sqlite3, staticfiles/).
BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATIC_ROOT = BASE_DIR / "staticfiles"

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
