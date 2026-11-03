"""golden's own production settings: the packaged prod plus project-local values.

Production hardening lives in ``golden_erp.settings.prod`` (shipped in the
wheel). Importing it re-exports the packaged base, so the project-local values
from ``config/settings/base.py`` are re-applied afterwards.
"""

# Order matters (isort off): config.settings.base loads .env before the
# packaged settings read the environment.
from . import base as _project  # noqa: I001
from golden_erp.settings.prod import *  # noqa: E402,F401,F403

BASE_DIR = _project.BASE_DIR
STATIC_ROOT = _project.STATIC_ROOT
ROOT_URLCONF = _project.ROOT_URLCONF
WSGI_APPLICATION = _project.WSGI_APPLICATION
