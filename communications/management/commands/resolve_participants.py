"""``manage.py resolve_participants``: link participants to parties (spec 2.2).

An unlinked participant (``party`` NULL) is linked when exactly one live
party of its tenant holds its normalised address (``match-1``). ``match-N``
stays unlinked. Never creates a party. Idempotent; ``--dry-run`` counts only.
The PartyEmail / PartyPhone save hook does the same per address as contact
points are added; this command catches up rows written around it
(``bulk_create``, imports, ``update()``).
"""

from django.core.management.base import BaseCommand

from communications.participants import resolve_unlinked


class Command(BaseCommand):
    help = "Link unlinked communication participants whose address matches exactly one party."

    def add_arguments(self, parser):
        parser.add_argument("--company", metavar="COMPANY_ID", help="Only this company.")
        parser.add_argument("--dry-run", action="store_true", help="Count, do not write.")

    def handle(self, *args, company=None, dry_run=False, **options):
        linked = resolve_unlinked(company_id=company, dry_run=dry_run)
        verb = "would link" if dry_run else "linked"
        self.stdout.write(f"resolve_participants: {verb} {linked} participant(s).")
