"""``manage.py ads_conversions --account <uuid> --dry-run``: what would upload.

Prints, as JSON, the request bodies (one per request, in the handler's
order and chunking) that the pending ``google_ads.conversion``
rows of the account would send (the same data as a fixture's
``requests.expected.json`` body) and the rows that upload nothing, with the
reason. Uploads themselves only ever happen through ``process_outbox``.
Needs the ``golden_erp.addons.google_ads`` plugin.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Print the Google Ads conversion rows an account's pending outbox rows would upload (--dry-run)."

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True, help="IntegrationAccount id (provider google_ads).")
        parser.add_argument("--dry-run", action="store_true", help="Required: print, never send.")

    def handle(self, *args, **options):
        if not options["dry_run"]:
            raise CommandError("Only --dry-run is supported: uploads run through process_outbox.")
        try:
            from golden_erp.addons.google_ads import conversions, mapping, values
        except ImportError as exc:  # pragma: no cover - the add-on ships in-tree
            raise CommandError("The google_ads add-on is not installed.") from exc
        import uuid

        from core.models import IntegrationAccount, OutboxEvent

        try:
            account_id = uuid.UUID(str(options["account"]))
        except ValueError:
            raise CommandError(f"--account must be an account id (uuid), not {options['account']!r}.") from None
        account = IntegrationAccount.objects.filter(pk=account_id, provider=values.PROVIDER_ID).first()
        if account is None:
            raise CommandError(f"No google_ads account {options['account']}.")
        settings = account.settings or {}
        planned = {values.UPLOAD: [], values.RETRACT: []}
        skipped, value_unknown = [], []
        pending = OutboxEvent.objects.filter(company_id=account.company_id, consumer=values.CONSUMER,
                                             state="pending").order_by("occurred_at", "pk")
        for event in pending:
            plan = (conversions.plan_upload(event, settings) if event.event == values.UPLOAD
                    else conversions.plan_retraction(event, settings))
            if plan.row is None:
                skipped.append({"event": event.event, "object": str(event.object_id), "reason": plan.skip})
                continue
            planned[event.event].append((plan.row, event))
            if plan.value_unknown:
                value_unknown.append(plan.row["orderId"])

        def bodies(event):
            # The handler's order and chunking (conversions.row_order / chunks).
            rows = sorted(planned[event], key=lambda pair: conversions.row_order(*pair))
            return [mapping.request_body(event, [rows[i][0] for i in chunk])
                    for chunk in conversions.chunks(list(range(len(rows))))]

        out = {
            "account": str(account.pk),
            "upload": bodies(values.UPLOAD),
            "retract": bodies(values.RETRACT),
            values.VALUE_UNKNOWN: value_unknown,
            "skipped": skipped,
        }
        self.stdout.write(json.dumps(out, indent=2, sort_keys=True))
