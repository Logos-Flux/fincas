"""golden_erp.settings.prod builds DATABASES the way the ORM reads it."""

import importlib
import os
from unittest import mock

from django.test import SimpleTestCase


class ProdDatabaseSettingsTests(SimpleTestCase):
    def _load_prod(self):
        env = {
            "DATABASE_URL": "postgres://u:p@db.example.invalid:6543/app",
            "DJANGO_SECRET_KEY": "test-only-secret-key-for-settings-import",
        }
        with mock.patch.dict(os.environ, env):
            import golden_erp.settings.prod as prod

            return importlib.reload(prod)

    def test_server_side_cursors_disabled_on_the_connection(self):
        # Django reads DISABLE_SERVER_SIDE_CURSORS from the DATABASES entry
        # (connection.settings_dict), never from a module-level setting.
        prod = self._load_prod()
        self.assertIs(prod.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"], True)
        self.assertFalse(hasattr(prod, "DISABLE_SERVER_SIDE_CURSORS"))
