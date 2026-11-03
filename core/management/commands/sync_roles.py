"""Converge role groups and derived ``is_staff`` (identity-unification §3.2).

    manage.py sync_roles                       # groups + permissions, re-derive is_staff
    manage.py sync_roles --adopt-existing      # first add every is_staff user to `staff`
    manage.py sync_roles --accept-preexisting  # take over role groups that already
                                               # held non-staff members (promotes them)
    manage.py sync_roles --dry-run [...]       # report the same plan; write nothing

If this run creates the ``staff`` group, existing staff are adopted first (as
``migrate`` does). Otherwise, without ``--adopt-existing``, a non-superuser
whose ``is_staff`` was set by hand and who holds no staff role is DEMOTED.
Superusers are never touched.
"""

from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.role_registry import ROLES, STAFF_ROLE
from core.roles import (
    _accept_preexisting_default,
    adopt_existing_staff,
    ensure_roles,
    preexisting_lookalikes,
    sync_user_flags,
    unadopted_staff,
    unflagged_staff,
)


class Command(BaseCommand):
    help = "Create/repair role groups (GOLDEN_ROLES) and re-derive is_staff from them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--adopt-existing",
            action="store_true",
            help=f"Add every is_staff non-superuser without a staff role to {STAFF_ROLE!r} first.",
        )
        parser.add_argument(
            "--accept-preexisting",
            action="store_true",
            help="Treat members of an already-existing staff-role group as staff (promotes them).",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change; write nothing."
        )

    def _plan(self, adopt_existing, accept_preexisting):
        """The same decisions the real run makes, computed read-only."""
        lookalikes = preexisting_lookalikes()
        if lookalikes and not accept_preexisting:
            return {"refuse": lookalikes}
        missing_groups = [n for n in ROLES if not Group.objects.filter(name=n).exists()]
        unadopted = list(unadopted_staff().values_list("username", flat=True))
        adopts = STAFF_ROLE in missing_groups or adopt_existing or bool(lookalikes)
        promote = sorted(set(unflagged_staff().values_list("username", flat=True)))
        return {
            "create": missing_groups,
            "adopt": unadopted if adopts else [],
            "demote": [] if adopts else unadopted,
            "promote": promote,
        }

    def handle(self, *args, adopt_existing=False, accept_preexisting=False, dry_run=False,
               **options):
        if dry_run:
            # Same effective opt-in as the real run (flag, else the setting).
            plan = self._plan(adopt_existing, accept_preexisting or _accept_preexisting_default())
            if "refuse" in plan:
                self.stdout.write(f"would refuse: pre-existing role group members without "
                                  f"is_staff {plan['refuse']} (pass --accept-preexisting)")
                return
            self.stdout.write(f"would create groups: {plan['create'] or 'none'}")
            self.stdout.write(f"would adopt into {STAFF_ROLE!r}: {plan['adopt'] or 'nobody'}")
            self.stdout.write(f"would demote (no staff role): {plan['demote'] or 'nobody'}")
            self.stdout.write(f"would promote (staff role, no flag): {plan['promote'] or 'nobody'}")
            return

        try:
            with transaction.atomic():
                report = ensure_roles(accept_preexisting=accept_preexisting or None)
                adopted = report["adopted"] + (adopt_existing_staff() if adopt_existing else [])
                flags = sync_user_flags()
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"groups created: {report['created'] or 'none'}")
        for role, perms in report["missing_permissions"].items():
            self.stderr.write(f"role {role!r}: permission(s) not found: {', '.join(perms)}")
        if adopt_existing or adopted:
            self.stdout.write(f"adopted into {STAFF_ROLE!r}: {adopted or 'nobody'}")
        promoted = report["promoted"] + flags["promoted"]
        self.stdout.write(f"is_staff set: {promoted or 'nobody'}")
        if flags["demoted"]:
            self.stdout.write(self.style.WARNING(f"is_staff cleared: {flags['demoted']}"))
        else:
            self.stdout.write("is_staff cleared: nobody")
