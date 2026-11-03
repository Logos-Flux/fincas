"""Create or repair the storefront service identity (F-037).

    manage.py ensure_storefront_identity

Idempotent: creates the ``storefront`` group and the ``storefront-api`` user if
missing, re-adds any missing contract permission, and re-clears staff,
superuser and usable-password state. ``migrate`` already does this on every
run (post_migrate); this command is for operators and runbooks that need it
without a migrate. Exits 1 if a contract permission row does not exist yet
(the owning app is not migrated).
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.storefront import STOREFRONT_PERMISSIONS, ensure_storefront_identity


class Command(BaseCommand):
    help = "Create/repair the storefront-api user and storefront group (idempotent)."

    def handle(self, *args, **options):
        with transaction.atomic():
            user = ensure_storefront_identity()
        held = {
            tuple(perm.split(".", 1))
            for perm in user.get_all_permissions()
        }
        missing = [f"{a}.{c}" for a, c in STOREFRONT_PERMISSIONS if (a, c) not in held]
        self.stdout.write(
            f"storefront identity: user {user.username!r} active={user.is_active} "
            f"staff={user.is_staff} superuser={user.is_superuser} "
            f"usable_password={user.has_usable_password()}; "
            f"{len(STOREFRONT_PERMISSIONS) - len(missing)}/{len(STOREFRONT_PERMISSIONS)} "
            "contract permissions"
        )
        if missing:
            raise CommandError(
                "Contract permission rows missing (run `manage.py migrate` first): "
                + ", ".join(missing)
            )
