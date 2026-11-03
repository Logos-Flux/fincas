"""Poll every enabled Connected account whose adapter polls (docs/crm/phase6-adapters.md 2.4).

    manage.py integrations_poll [--provider P] [--account UUID] [--max-seconds 50]

Run it every 5 minutes from the scheduler. Paused accounts (rate limit,
rejected credentials) are skipped until the pause ends. Prints counts only.
"""

from django.core.management.base import BaseCommand

from core.integrations.poll import poll_accounts


class Command(BaseCommand):
    help = "Poll enabled integration accounts (inbound) and ingest what they return."

    def add_arguments(self, parser):
        parser.add_argument("--provider", help="Only this provider id.")
        parser.add_argument("--account", help="Only this IntegrationAccount id.")
        parser.add_argument("--max-seconds", type=float, default=50.0, help="Time budget for the run.")
        parser.add_argument("--max-pages", type=int, default=100, help="Pages per account and grant.")

    def handle(self, *args, provider=None, account=None, max_seconds=50.0, max_pages=100, **options):
        report = poll_accounts(provider=provider, account_id=account, max_seconds=max_seconds,
                               max_pages=max_pages)
        self.stdout.write(
            f"integrations_poll: {report.accounts} account(s), {report.pages} page(s), "
            f"{report.items} item(s), {report.changed} changed, {report.failed} failed, "
            f"{report.skipped_paused} paused skipped, {len(report.stopped)} stopped early."
        )
