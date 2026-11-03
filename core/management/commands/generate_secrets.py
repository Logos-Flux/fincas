"""Print a fresh set of the secrets a production deployment must set.

    python manage.py generate_secrets            # all of them, as KEY=value lines
    python manage.py generate_secrets --only API_JWT_SIGNING_KEY

The output is environment-file syntax, so it pastes into any host: a compose
``.env``, a systemd ``EnvironmentFile``, ``flyctl secrets import``, a
Kubernetes Secret. Nothing is stored or logged. Setting one of these on a
running install has side effects (docs/deployment.md, "Required secrets"):
in short, a new ``API_JWT_SIGNING_KEY`` voids issued bearer tokens and a new
``DJANGO_SECRET_KEY`` signs everyone out.
"""

from __future__ import annotations

import secrets

from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand

from core.checks import DEPLOY_SECRETS


def _fernet_key() -> str:
    return Fernet.generate_key().decode()


GENERATORS = {
    "DJANGO_SECRET_KEY": lambda: secrets.token_urlsafe(50),
    "API_JWT_SIGNING_KEY": lambda: secrets.token_urlsafe(48),
    "LLMHUB_FERNET_KEY": _fernet_key,
    # A one-key list; rotation puts the new key first (docs/crm/adapters.md#keys).
    "GOLDEN_SECRET_KEYS": _fernet_key,
}
assert set(DEPLOY_SECRETS) <= set(GENERATORS), "every required secret needs a generator"


class Command(BaseCommand):
    help = "Print freshly generated production secrets as KEY=value lines."
    # It is the fix for core.E060, so that check must not block it.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--only", action="append", metavar="NAME", choices=sorted(GENERATORS),
            help="Print just this secret (repeatable).",
        )

    def handle(self, *args, only=None, **options):
        for name in only or GENERATORS:
            self.stdout.write(f"{name}={GENERATORS[name]()}")
