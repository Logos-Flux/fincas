"""Re-encrypt every stored integration credential under the newest key (docs/crm/adapters.md#keys).

    GOLDEN_SECRET_KEYS=<new>,<old> manage.py rotate_secrets [--dry-run]

Every ``IntegrationCredential`` row, revoked ones included, is decrypted with
whichever configured key made it and encrypted again with the first key; its
``key_id`` is updated in the same statement. Afterwards the old key can leave
``GOLDEN_SECRET_KEYS``: nothing still needs it (``check`` then confirms with
no ``core.E040``). Rows no configured key can read are counted and left as
they are.

Prints counts only, never a value. Exit 0 when every readable row is under
the newest key, 1 when an *active* row could not be read. Unreadable revoked
rows (left behind when a credential was re-entered after its key was lost)
are counted and do not fail the run: nothing uses them.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import secrets
from core.models import IntegrationCredential


class Command(BaseCommand):
    help = "Re-encrypt every integration credential under the first GOLDEN_SECRET_KEYS key."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count what would change; write nothing.")

    def handle(self, *args, dry_run=False, **options):
        try:
            primary = secrets.primary_key_id()
        except Exception as exc:  # ImproperlyConfigured: no key, or a malformed one
            raise CommandError(str(exc)) from None
        rotated = current = unreadable = unreadable_revoked = 0
        ids = list(IntegrationCredential._base_manager.order_by("pk").values_list("pk", flat=True))
        for pk in ids:
            with transaction.atomic():
                row = IntegrationCredential._base_manager.select_for_update().filter(pk=pk).first()
                if row is None:
                    continue
                try:
                    ciphertext, key_id = secrets.rotate(row.ciphertext)
                except secrets.InvalidToken:
                    if row.revoked_at is None:
                        unreadable += 1
                    else:
                        unreadable_revoked += 1
                    continue
                if row.key_id == primary:
                    current += 1
                else:
                    rotated += 1
                if not dry_run:
                    IntegrationCredential._base_manager.filter(pk=pk).update(
                        ciphertext=ciphertext, key_id=key_id)
        verb = "would re-encrypt" if dry_run else "re-encrypted"
        self.stdout.write(
            f"rotate_secrets: {verb} {rotated + current} credential(s) under key {primary} "
            f"({rotated} from an older key); {unreadable} active unreadable, "
            f"{unreadable_revoked} revoked unreadable (left as they are)."
        )
        if unreadable:
            self.stderr.write(
                f"{unreadable} credential(s) were written under a key that is not configured. "
                "Add that key to GOLDEN_SECRET_KEYS and run again, or re-enter them."
            )
            raise SystemExit(1)
