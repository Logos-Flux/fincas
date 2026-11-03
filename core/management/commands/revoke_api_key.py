"""Revoke per-integration API keys (identity-unification §2.6).

    manage.py revoke_api_key <prefix> [<prefix> ...]
    manage.py revoke_api_key --name crm-sync          # by key label
    manage.py revoke_api_key --service crm-sync       # every key of svc-crm-sync

Revocation is immediate for the key and for every bearer minted from it
(those carry the key's prefix and are re-checked on each request). Rows are
kept, with ``revoked_at`` set, for the audit trail.
"""

from django.core.management.base import BaseCommand, CommandError

from core.models import ApiKey
from core.service_accounts import revoke, service_username


class Command(BaseCommand):
    help = "Revoke API keys by prefix, key name or service."

    def add_arguments(self, parser):
        parser.add_argument("prefixes", nargs="*", metavar="PREFIX")
        parser.add_argument("--name", help="Key label (ApiKey.name).")
        parser.add_argument("--service", help="Service name; revokes every key of svc-<name>.")

    def handle(self, *args, **options):
        if not (options["prefixes"] or options["name"] or options["service"]):
            raise CommandError("Name what to revoke: PREFIX..., --name or --service.")
        queryset = ApiKey.objects.none()
        if options["prefixes"]:
            queryset = queryset | ApiKey.objects.filter(prefix__in=options["prefixes"])
        if options["name"]:
            queryset = queryset | ApiKey.objects.filter(name=options["name"])
        if options["service"]:
            queryset = queryset | ApiKey.objects.filter(
                user__username=service_username(options["service"])
            )
        matched = list(queryset.values_list("prefix", flat=True))
        if not matched:
            raise CommandError("No matching API key.")
        count = revoke(ApiKey.objects.filter(prefix__in=matched))
        self.stdout.write(
            f"Revoked {count} key(s); {len(matched) - count} already revoked: {', '.join(sorted(matched))}"
        )
