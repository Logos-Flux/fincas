"""(Re)create the dedicated legacy ``API_KEY`` account (identity spec §2.6).

    manage.py ensure_legacy_api_user

Migration ``core.0009_api_service_account`` creates it once; requests never
do, so a deleted account keeps the legacy key switched off until this runs.
An existing account is reported, never modified.
"""

from django.core.management.base import BaseCommand, CommandError

from core.service_accounts import ensure_legacy_api_user, legacy_api_username, legacy_user_problem


class Command(BaseCommand):
    help = "Create the GOLDEN_LEGACY_API_KEY_USER account if it is missing."

    def handle(self, *args, **options):
        if legacy_api_username() is None:
            raise CommandError("The legacy path is disabled (GOLDEN_LEGACY_API_KEY_USER is empty).")
        user = ensure_legacy_api_user()
        problem = legacy_user_problem(user)
        state = f"refused by the key resolver: {problem}" if problem else "usable"
        self.stdout.write(f"{user.username}: superuser={user.is_superuser}, {state}")
