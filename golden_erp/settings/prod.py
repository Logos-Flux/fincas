"""Production settings — PostgreSQL via dj-database-url (shipped in the package).

The packaged base plus production hardening: DATABASES from DATABASE_URL,
SECURE_* / HSTS / secure cookies behind a TLS-terminating proxy, Mailgun via
anymail, and Sentry with shell-originated events dropped. A downstream
production overlay starts with ``from golden_erp.settings.prod import *``;
golden's own ``config/settings/prod.py`` is a thin layer on this module.
"""

import logging
import os

import dj_database_url

from golden_erp.settings.base import *  # noqa: F401, F403

DEBUG = False
# A production deploy needs its own signing/encryption keys, never ones
# derived from SECRET_KEY (system check core.E060, docs/deployment.md).
GOLDEN_REQUIRE_DEPLOY_SECRETS = True
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(",") if os.environ.get("ALLOWED_HOSTS") else []

_db_url = os.environ.get("DATABASE_URL", "")
_ssl_env = os.environ.get("DB_SSL_REQUIRE")
if _ssl_env is not None:
    _ssl_require = _ssl_env.lower() in ("true", "1", "yes")
else:
    _ssl_require = bool(_db_url) and not (
        _db_url.startswith("sqlite")
        or "@db:" in _db_url
        or "@localhost:" in _db_url
        or "@127.0.0.1:" in _db_url
    )

DATABASES = {
    "default": dj_database_url.config(
        default=_db_url,
        conn_max_age=60,
        ssl_require=_ssl_require,
        # Transaction-mode poolers (pgbouncer, Supavisor) can run a named
        # cursor's DECLARE and its FETCH on different backends
        # (InvalidCursorName). Django reads this per connection, from the
        # DATABASES entry; a module-level DISABLE_SERVER_SIDE_CURSORS is ignored.
        disable_server_side_cursors=True,
    )
}

# Security
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "True").lower() == "true"
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

CSRF_TRUSTED_ORIGINS = os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if os.environ.get("CSRF_TRUSTED_ORIGINS") else []

# Email — Mailgun HTTP API via django-anymail
EMAIL_BACKEND = "anymail.backends.mailgun.EmailBackend"
# A public demo login (DEMO_USERNAMES) must never deliver mail: order
# confirmations and shipping notices fire on ordinary order edits, to any
# address a visitor types into a customer record. Log instead.
if DEMO_USERNAMES:  # noqa: F405
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
ANYMAIL = {
    "MAILGUN_API_KEY": os.environ.get("MAILGUN_API_KEY", ""),
    "MAILGUN_SENDER_DOMAIN": os.environ.get("MAILGUN_SENDER_DOMAIN", ""),
}
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "")


def _is_running_django_shell() -> bool:
    """Return True if the current process was launched as manage.py shell."""
    import sys

    argv = getattr(sys, "argv", None)
    if not argv or not isinstance(argv, (list, tuple)):
        return False
    for i, arg in enumerate(argv):
        arg_str = str(arg)
        if (arg_str.endswith("manage.py") or arg_str.endswith("django-admin")) and i + 1 < len(argv):
            if argv[i + 1] == "shell":
                return True
        if arg_str == "shell" and i > 0 and ("manage.py" in str(argv[i - 1]) or "django-admin" in str(argv[i - 1])):
            return True
    if len(argv) >= 2 and argv[1] == "shell":
        return True
    return False


def _is_django_shell_event(event: dict, hint: dict | None = None) -> bool:
    """Check if an event originated from an interactive Django shell session."""
    if _is_running_django_shell():
        return True

    culprit = event.get("culprit") or ""
    if "django.core.management.commands.shell" in culprit:
        return True

    transaction = event.get("transaction") or ""
    if "django.core.management.commands.shell" in transaction:
        return True

    exceptions = event.get("exception", {})
    if isinstance(exceptions, dict):
        values = exceptions.get("values", [])
        if isinstance(values, list):
            for exc in values:
                if not isinstance(exc, dict):
                    continue
                stacktrace = exc.get("stacktrace")
                if not isinstance(stacktrace, dict):
                    continue
                frames = stacktrace.get("frames", [])
                if not isinstance(frames, list):
                    continue
                for frame in frames:
                    if not isinstance(frame, dict):
                        continue
                    module = frame.get("module") or ""
                    filename = frame.get("filename") or ""
                    abs_path = frame.get("abs_path") or ""
                    if "django.core.management.commands.shell" in module:
                        return True
                    if (
                        "django/core/management/commands/shell.py" in filename
                        or filename.endswith("commands/shell.py")
                        or "django/core/management/commands/shell.py" in abs_path
                        or abs_path.endswith("commands/shell.py")
                    ):
                        return True

    if hint and isinstance(hint, dict) and "exc_info" in hint:
        exc_info = hint.get("exc_info")
        if isinstance(exc_info, tuple) and len(exc_info) == 3 and exc_info[2]:
            tb = exc_info[2]
            while tb:
                frame = getattr(tb, "tb_frame", None)
                if frame:
                    code = getattr(frame, "f_code", None)
                    if code:
                        co_filename = getattr(code, "co_filename", "") or ""
                        if (
                            "django/core/management/commands/shell.py" in co_filename
                            or co_filename.endswith("commands/shell.py")
                        ):
                            return True
                tb = getattr(tb, "tb_next", None)

    return False


def sentry_before_send(event: dict, hint: dict | None = None) -> dict | None:
    """Filter out errors originating from interactive management shells."""
    if not isinstance(event, dict):
        return event

    try:
        if _is_django_shell_event(event, hint):
            logger = logging.getLogger("sentry")
            logger.warning(
                "Dropping Sentry event originating from django shell (event_id=%s, culprit=%s)",
                event.get("event_id"),
                event.get("culprit"),
            )
            return None
    except Exception:
        pass

    return event


# Sentry
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=0,
        send_default_pii=False,
        before_send=sentry_before_send,
        integrations=[
            DjangoIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
    )
