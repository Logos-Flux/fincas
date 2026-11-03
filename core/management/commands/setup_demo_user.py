"""Create or refresh the public demo login (staging only).

Reads DEMO_LOGIN_USERNAME / DEMO_LOGIN_PASSWORD from settings and makes that
account active, staff, and never superuser. The login-page hint advertises it
and DemoGuardMiddleware restricts it, so it must not be added to
DEMO_USERNAMES-less deployments by accident: with no username set, this
command does nothing.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.role_registry import STAFF_ROLE
from core.roles import ensure_role


class Command(BaseCommand):
    help = "Create or refresh the non-superuser demo account from DEMO_LOGIN_* settings."

    def handle(self, *args, **options):
        username = settings.DEMO_LOGIN_USERNAME
        password = settings.DEMO_LOGIN_PASSWORD
        if not username:
            self.stdout.write("DEMO_LOGIN_USERNAME is not set; nothing to do.")
            return
        if not password:
            raise CommandError("DEMO_LOGIN_PASSWORD must be set alongside DEMO_LOGIN_USERNAME.")
        if username not in settings.DEMO_USERNAMES:
            raise CommandError(
                f"{username!r} is not in DEMO_USERNAMES, so DemoGuardMiddleware would not "
                "restrict it. Refusing to create an unguarded public login."
            )

        User = get_user_model()
        user, created = User.objects.get_or_create(username=username)
        if user.is_superuser and not created:
            raise CommandError(f"{username!r} is a superuser; pick another demo username.")
        user.is_active = True
        user.is_staff = True
        user.is_superuser = False
        user.first_name = user.first_name or "Demo"
        user.set_password(password)
        user.save()
        # Staff, and nothing more: exactly the built-in staff role (which
        # derives is_staff, core.roles) and no direct permissions.
        # ensure_role, never a bare get_or_create: if this command is what
        # creates the staff group, existing staff must be adopted first.
        user.groups.set([ensure_role(STAFF_ROLE)[0]])
        user.user_permissions.clear()
        self.stdout.write(f"{'created' if created else 'refreshed'} demo user {username!r} (staff, not superuser)")
