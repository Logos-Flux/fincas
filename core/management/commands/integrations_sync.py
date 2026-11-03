"""Ask an adapter to reconcile its accounts (docs/crm/phase6-adapters.md 3.2).

    manage.py integrations_sync <provider> --reconcile [--full] [--account UUID]

For adapters that sync state outward from changes that send no signal
(Klaviyo: consent and suppression rows). ``--reconcile`` enqueues a sync for
every party whose provider state may have changed since the last run;
``--full`` takes every candidate. It makes no provider call itself: the
enqueued rows are delivered by ``process_outbox``. Run it hourly from the
scheduler. Disabled accounts are skipped. Prints counts only.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from core.integrations.accounts import adapter_class
from core.integrations.transport import get_transport
from core.models import IntegrationAccount

logger = logging.getLogger("golden_erp.integrations")


class Command(BaseCommand):
    help = "Reconcile an integration provider's accounts (enqueues outbox syncs; no provider call)."

    def add_arguments(self, parser):
        parser.add_argument("provider", help="The adapter's provider id, e.g. klaviyo.")
        parser.add_argument("--reconcile", action="store_true", help="Enqueue syncs for what changed.")
        parser.add_argument("--full", action="store_true", help="With --reconcile: every candidate.")
        parser.add_argument("--account", help="Only this IntegrationAccount id.")

    def handle(self, *args, provider, reconcile=False, full=False, account=None, **options):
        cls = adapter_class(provider)
        if cls is None:
            raise CommandError(f"No installed adapter {provider!r} (GOLDEN_PLUGINS).")
        if not reconcile:
            raise CommandError("Nothing to do: pass --reconcile.")
        if not callable(getattr(cls, "reconcile", None)):
            raise CommandError(f"{provider} has no reconcile; it syncs from signals and polls only.")
        qs = IntegrationAccount.objects.filter(provider=provider, enabled=True).order_by("company_id")
        if account:
            qs = qs.filter(pk=account)
        total = {}
        failed = 0
        for row in qs:
            try:
                report = cls(row, get_transport(row)).reconcile(full=full)
            except Exception as exc:  # one account's failure never stops the others
                failed += 1
                logger.warning("integrations_sync %s: account %s failed: %s", provider, row.pk, type(exc).__name__)
                continue
            for key, value in report.items():
                total[key] = total.get(key, 0) + value
        if failed:
            total["failed_accounts"] = failed
        counts = ", ".join(f"{value} {key.replace('_', ' ')}" for key, value in sorted(total.items()))
        self.stdout.write(f"integrations_sync {provider}: {qs.count()} account(s){', ' + counts if counts else ''}.")
        if failed:
            raise CommandError(f"{failed} account(s) failed; see the log.")
