"""Create or update a company's Connected account from the shell (docs/crm/adapters.md).

    manage.py integration_connect <provider> --company <uuid> [--settings-file s.json]
        [--credential KIND] [--enable | --disable]

``--credential KIND`` reads the secret from **stdin** (never an argument: it
would land in the shell history and the process list) and stores it
encrypted. Prints the account id and what changed, never a secret.
"""

import json
import sys

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.integrations import accounts as services
from core.models import Company, IntegrationAccount


class Command(BaseCommand):
    help = "Create/update an integration account; --credential reads its secret from stdin."

    def add_arguments(self, parser):
        parser.add_argument("provider")
        parser.add_argument("--company", required=True, help="Company id (uuid).")
        parser.add_argument("--settings-file", help="A JSON file of non-secret settings.")
        parser.add_argument("--credential", help="Credential kind to store; the secret is read from stdin.")
        parser.add_argument("--subject", default="", help="Label shown for the credential.")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--enable", action="store_true")
        group.add_argument("--disable", action="store_true")

    def handle(self, *args, provider, company, settings_file=None, credential=None, subject="",
               enable=False, disable=False, **options):
        try:
            company_obj = Company.objects.get(pk=company)
        except (Company.DoesNotExist, ValidationError, ValueError):
            raise CommandError(f"No company {company!r}.") from None
        settings = None
        if settings_file:
            try:
                with open(settings_file, encoding="utf-8") as fh:
                    settings = json.load(fh)
            except (OSError, ValueError) as exc:
                raise CommandError(f"Cannot read settings from {settings_file}: {type(exc).__name__}") from None
        try:
            account = IntegrationAccount.objects.filter(company=company_obj, provider=provider).first()
            if account is None:
                account = services.create_account(company_obj, provider, settings=settings or {})
                self.stdout.write(f"created {provider} account {account.pk}")
            elif settings is not None:
                services.update_account(account, settings=settings)
                self.stdout.write(f"updated settings of {account.pk}")
            if credential:
                secret = sys.stdin.readline().rstrip("\n")
                services.set_credential(account, credential, secret, subject=subject)
                self.stdout.write(f"stored {credential} (encrypted) on {account.pk}")
            if enable or disable:
                services.update_account(account, enabled=bool(enable))
                self.stdout.write(f"{'enabled' if enable else 'disabled'} {account.pk}")
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from None
