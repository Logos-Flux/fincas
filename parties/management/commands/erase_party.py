"""Erase a person's personal data (docs/crm/phase2-party.md 7.4, step 12).

    python manage.py erase_party --party <uuid> --reason "GDPR request #123" [--actor <username>] [--noinput]

Same service as the ERP danger zone and ``POST /api/v1/parties/parties/<id>/erase/``
(``parties.erasure.erase_party``). Not reversible, so it asks for the party's
display name unless ``--noinput`` is given. Prints what was kept and why;
never prints the erased data.
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from parties.erasure import ErasureError, erase_party, erasure_summary
from parties.models import Party


class Command(BaseCommand):
    help = "Erase a person party's personal data (not reversible)."

    def add_arguments(self, parser):
        parser.add_argument("--party", required=True, help="The party's id (UUID).")
        parser.add_argument("--reason", required=True, help="Why; kept in the audit log.")
        parser.add_argument("--actor", help="Username of the person doing it (recorded as the actor).")
        parser.add_argument("--noinput", "--no-input", action="store_false", dest="interactive",
                            help="Do not ask for confirmation.")
        parser.add_argument("--json", action="store_true", help="Print the result as JSON.")

    def handle(self, *args, **options):
        try:
            party = Party._base_manager.get(pk=options["party"])
        except (Party.DoesNotExist, ValidationError, ValueError):
            raise CommandError(f"No party with id {options['party']}.") from None
        actor = None
        if options["actor"]:
            actor = get_user_model().objects.filter(username=options["actor"]).first()
            if actor is None:
                raise CommandError(f"No user {options['actor']!r}.")
        if options["interactive"]:
            answer = input(
                f"Erase party {party.pk} permanently? Type its display name to confirm: "
            )
            if answer.strip() != party.display_name.strip():
                raise CommandError("Confirmation did not match; nothing was erased.")
        try:
            result = erase_party(party, actor, options["reason"], source="erp")
        except ErasureError as exc:
            raise CommandError(str(exc)) from exc
        if options["json"]:
            self.stdout.write(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        else:
            self.stdout.write(self.style.SUCCESS(f"Erased party {party.pk}. {erasure_summary(result)}"))
