"""Tests for Sentry before_send filter (a production Sentry issue).

Verifies that errors and typos originating from interactive Django shell sessions
(manage.py shell / shell -c "...") are dropped by Sentry before_send and do not page
on-call as URGENT incidents.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from golden_erp.settings.prod import (
    _is_django_shell_event,
    _is_running_django_shell,
    sentry_before_send,
)


class RunningDjangoShellDetectionTests(SimpleTestCase):
    def test_detects_standard_manage_py_shell(self):
        with patch.object(sys, "argv", ["manage.py", "shell"]):
            self.assertTrue(_is_running_django_shell())

    def test_detects_manage_py_shell_with_c_argument(self):
        with patch.object(
            sys, "argv", ["./manage.py", "shell", "-c", "Order.objects.first().lines"]
        ):
            self.assertTrue(_is_running_django_shell())

    def test_detects_absolute_path_manage_py_shell(self):
        with patch.object(
            sys, "argv", ["/app/manage.py", "shell", "--interface", "ipython"]
        ):
            self.assertTrue(_is_running_django_shell())

    def test_detects_django_admin_shell(self):
        with patch.object(sys, "argv", ["django-admin", "shell"]):
            self.assertTrue(_is_running_django_shell())

    def test_ignores_non_shell_management_commands(self):
        non_shell_commands = [
            ["manage.py", "migrate"],
            ["manage.py", "check"],
            ["manage.py", "runserver"],
            ["manage.py", "import_qbo_export", "file.xlsx"],
            ["manage.py", "makemigrations"],
            ["uvicorn", "config.asgi:application"],
            ["gunicorn", "config.wsgi:application"],
        ]
        for argv in non_shell_commands:
            with self.subTest(argv=argv):
                with patch.object(sys, "argv", argv):
                    self.assertFalse(_is_running_django_shell())

    def test_handles_empty_or_malformed_argv(self):
        with patch.object(sys, "argv", []):
            self.assertFalse(_is_running_django_shell())
        with patch.object(sys, "argv", None):
            self.assertFalse(_is_running_django_shell())


class DjangoShellEventDetectionTests(SimpleTestCase):
    def setUp(self):
        # Ensure sys.argv is a neutral command during event-structure tests
        self._argv_patcher = patch.object(sys, "argv", ["manage.py", "runserver"])
        self._argv_patcher.start()

    def tearDown(self):
        self._argv_patcher.stop()

    def test_detects_by_culprit(self):
        event = {
            "event_id": "e1",
            "culprit": "django.core.management.commands.shell in <module>",
        }
        self.assertTrue(_is_django_shell_event(event))

    def test_detects_by_transaction(self):
        event = {
            "event_id": "e2",
            "transaction": "django.core.management.commands.shell",
        }
        self.assertTrue(_is_django_shell_event(event))

    def test_detects_by_stacktrace_module(self):
        event = {
            "event_id": "e3",
            "exception": {
                "values": [
                    {
                        "type": "AttributeError",
                        "value": "'Order' object has no attribute 'lines'",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "django/core/management/base.py",
                                    "module": "django.core.management.base",
                                    "function": "execute",
                                },
                                {
                                    "filename": "django/core/management/commands/shell.py",
                                    "module": "django.core.management.commands.shell",
                                    "function": "handle",
                                },
                                {
                                    "filename": "<string>",
                                    "module": "django.core.management.commands.shell",
                                    "function": "<module>",
                                },
                            ]
                        },
                    }
                ]
            },
        }
        self.assertTrue(_is_django_shell_event(event))

    def test_detects_by_stacktrace_filename(self):
        event = {
            "event_id": "e4",
            "exception": {
                "values": [
                    {
                        "type": "NameError",
                        "value": "name 'foo' is not defined",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": (
                                        "/usr/local/lib/python3.12/site-packages/"
                                        "django/core/management/commands/shell.py"
                                    ),
                                    "module": "custom_shell",
                                    "function": "handle",
                                },
                                {
                                    "filename": "<console>",
                                    "function": "<module>",
                                },
                            ]
                        },
                    }
                ]
            },
        }
        self.assertTrue(_is_django_shell_event(event))

    def test_detects_by_traceback_hint(self):
        # Create a mock traceback structure
        mock_frame = MagicMock()
        mock_frame.f_code.co_filename = "/app/django/core/management/commands/shell.py"
        mock_frame.f_code.co_name = "handle"

        mock_tb = MagicMock()
        mock_tb.tb_frame = mock_frame
        mock_tb.tb_next = None

        hint = {"exc_info": (AttributeError, AttributeError("test"), mock_tb)}
        event = {"event_id": "e5"}
        self.assertTrue(_is_django_shell_event(event, hint))

    def test_passes_legitimate_application_errors(self):
        normal_event = {
            "event_id": "e6",
            "culprit": "apps.erp.orders.views.order_detail in get",
            "transaction": "/erp/orders/123/",
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": "Invalid order status transition",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "apps/erp/orders/views.py",
                                    "module": "apps.erp.orders.views",
                                    "function": "order_detail",
                                },
                                {
                                    "filename": "apps/erp/orders/services.py",
                                    "module": "apps.erp.orders.services",
                                    "function": "transition_order",
                                },
                            ]
                        },
                    }
                ]
            },
        }
        self.assertFalse(_is_django_shell_event(normal_event))

    def test_passes_other_management_command_errors(self):
        qbo_event = {
            "event_id": "e7",
            "culprit": "accounting.management.commands.import_qbo_export in handle",
            "transaction": "manage.py import_qbo_export",
            "exception": {
                "values": [
                    {
                        "type": "RuntimeError",
                        "value": "Failed to parse QBO spreadsheet",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": (
                                        "accounting/management/commands/"
                                        "import_qbo_export.py"
                                    ),
                                    "module": "accounting.management.commands.import_qbo_export",
                                    "function": "handle",
                                },
                            ]
                        },
                    }
                ]
            },
        }
        self.assertFalse(_is_django_shell_event(qbo_event))

    def test_handles_malformed_events_defensively(self):
        malformed_events = [
            {},
            {"exception": None},
            {"exception": {}},
            {"exception": {"values": None}},
            {"exception": {"values": [{}]}},
            {"exception": {"values": [{"stacktrace": None}]}},
            {"exception": {"values": [{"stacktrace": {"frames": None}}]}},
            {"exception": {"values": [{"stacktrace": {"frames": [{}]}}]}},
        ]
        for event in malformed_events:
            with self.subTest(event=event):
                self.assertFalse(_is_django_shell_event(event))


class SentryBeforeSendTests(SimpleTestCase):
    def test_drops_shell_event_and_logs_warning(self):
        event = {
            "event_id": "drop-me",
            "culprit": "django.core.management.commands.shell in <module>",
        }
        with self.assertLogs("sentry", level=logging.WARNING) as cm:
            result = sentry_before_send(event)
            self.assertIsNone(result)
            self.assertTrue(any("Dropping Sentry event" in msg for msg in cm.output))

    def test_preserves_application_event(self):
        event = {
            "event_id": "keep-me",
            "culprit": "apps.erp.orders.models.Order in recalculate",
        }
        with patch.object(sys, "argv", ["gunicorn", "config.wsgi"]):
            result = sentry_before_send(event)
            self.assertEqual(result, event)

    def test_drops_events_when_running_in_shell_process(self):
        event = {
            "event_id": "shell-proc-error",
            "culprit": "some_script.py in <module>",
        }
        with patch.object(sys, "argv", ["manage.py", "shell", "-c", "import script; script.run()"]):
            result = sentry_before_send(event)
            self.assertIsNone(result)

    def test_handles_non_dict_input(self):
        self.assertEqual(sentry_before_send("invalid"), "invalid")
        self.assertIsNone(sentry_before_send(None))
