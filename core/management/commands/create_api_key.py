"""Issue a per-integration API key (identity-unification D7, spec §2.6).

    manage.py create_api_key --name crm-sync --role crm-sync
    manage.py create_api_key --name forge --role forge-agent --scope forge-agent --expires-in 90d
    manage.py create_api_key --name crm-sync --key-name crm-sync-2026q4   # rotate: a second key

Creates (or reuses) the service account ``svc-<name>`` — unusable password,
not staff, not superuser — adds it to the given ``kind="service"`` roles
(``GOLDEN_ROLES``), and issues a ``core.ApiKey``. The key is printed ONCE, on
stdout, and only its SHA-256 is stored; everything else goes to stderr.

``--role`` is the contract: the account may do exactly what those roles'
groups permit. ``--scope`` is a registered bearer-token scope (default
``api``); a plugin scope also binds the key, and every token minted from it,
to that scope's paths. ``--company`` pins the account to a company so
company-scoped endpoints its roles permit return that company's rows; it is
required once more than one company exists, and an unpinned account may not
write at all.
"""

import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import ApiKey, Company, UserProfile
from core.service_accounts import ensure_service_account, issue_api_key, service_username

_DURATION = re.compile(r"\A(\d+)([smhdw]?)\Z")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(value: str) -> timedelta:
    match = _DURATION.match((value or "").strip().lower())
    if not match or int(match.group(1)) <= 0:
        raise CommandError(f"--expires-in {value!r}: use e.g. 3600, 12h, 90d or 4w.")
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])


class Command(BaseCommand):
    help = "Create a service account svc-<name> (if needed) and print a new API key once."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Service name; the account is svc-<name>.")
        parser.add_argument(
            "--role", action="append", dest="roles", default=[], metavar="ROLE",
            help="A kind='service' role from GOLDEN_ROLES (repeatable). Required for a new account.",
        )
        parser.add_argument("--scope", default="api", help="Bearer-token scope (default: api).")
        parser.add_argument("--expires-in", help="Lifetime, e.g. 3600, 12h, 90d, 4w. Default: none.")
        parser.add_argument("--key-name", help="Unique label for this key. Default: the service name.")
        parser.add_argument("--company", help="Company id or exact name to pin the account to.")
        parser.add_argument("--created-by", help="Username recorded as the key's creator.")

    def handle(self, *args, **options):
        name = options["name"]
        roles = options["roles"]
        key_name = options["key_name"] or name
        if ApiKey.objects.filter(name=key_name).exists():
            raise CommandError(f"A key named {key_name!r} exists; pass --key-name for another key.")
        exists = User.objects.filter(username=service_username(name)).exists()
        if not exists and not roles:
            raise CommandError("A new service account needs at least one --role (its contract).")
        expires_at = None
        if options["expires_in"]:
            expires_at = timezone.now() + parse_duration(options["expires_in"])
        company = None
        if options["company"]:
            ref = options["company"]
            company = (
                Company.objects.filter(pk=ref).first() if _looks_like_uuid(ref) else None
            ) or Company.objects.filter(name=ref).first()
            if company is None:
                raise CommandError(f"No company {ref!r}.")
        if company is None and Company.objects.count() > 1:
            pinned = UserProfile.objects.filter(
                user__username=service_username(name), company__isnull=False
            ).exists()
            if not pinned:
                raise CommandError(
                    "More than one company exists: pass --company so the account reads and "
                    "writes one company's rows (an unpinned service account cannot write)."
                )
        created_by = None
        if options["created_by"]:
            created_by = User.objects.filter(username=options["created_by"]).first()
            if created_by is None:
                raise CommandError(f"No user {options['created_by']!r}.")
        try:
            with transaction.atomic():
                user = ensure_service_account(name, roles, company=company)
                key, raw = issue_api_key(
                    user, name=key_name, scope=options["scope"], expires_at=expires_at,
                    created_by=created_by,
                )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        groups = ", ".join(sorted(user.groups.values_list("name", flat=True))) or "none"
        self.stderr.write(
            f"Created key {key.name!r} (prefix {key.prefix}) for {user.username}; "
            f"roles: {groups}; scope: {key.scope}; "
            f"expires: {key.expires_at.isoformat() if key.expires_at else 'never'}.\n"
            "The key is shown once. Send it as `Authorization: Api-Key <key>`."
        )
        self.stdout.write(raw)


def _looks_like_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True
