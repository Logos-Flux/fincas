"""Multiply a scrubbed-copy clone to N times its volume (plan section 8, the 10x trend).

Every business row of the models below is copied ``factor - 1`` times with a
fresh primary key, a suffixed document number, a rewritten e-mail address and
every date shifted back by ``3 * k`` days for copy ``k``. Money is copied as
it is: a document's lines, its totals, its payments and its journal lines all
move together, so an invoice still balances and a journal entry still has
debits equal to credits. Foreign keys between copied rows are re-pointed to
the copies; keys to catalog, settings and people rows (products, warehouses,
price lists, user profiles, teams) are kept. Rows that must stay unique are
not copied: the tenant's own ("self") party, merged and erased parties,
portal logins (``Customer.user`` / ``Party.user`` are blanked on the copies),
purchase-order and pick-ticket links.

Writes go through ``bulk_create`` inside ``core.plugins.dispatch.suppressed()``
in one transaction (Django's Postgres FKs are deferred, so insertion order
does not matter). Nothing fires the lifecycle signals, nothing runs
``save()``; afterwards the script runs the repairs the plan names:
``reconcile_parties --fix``, ``backfill_crm --step 5`` and ``6``,
``reconcile_engagements --fix``, then ``reconcile_engagements --check``,
``reconcile_parties --check`` and ``check_engagements`` must be clean.

It refuses any database whose name does not contain ``_perf_``: it is for a
disposable clone, never for ``golden_synth`` or a deployment's database.

    DATABASE_URL=postgres://.../golden_perf_10x python scripts/perf/synthesize_volume.py --factor 10

``--dry-run`` rolls the copy back after printing the counts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.apps import apps  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection, models, transaction  # noqa: E402

from core.plugins.dispatch import suppressed  # noqa: E402

BATCH = 1000
DAYS_PER_COPY = 3

#: Copy order (dependencies first, for readability; the FKs are deferred).
COPIED = [
    "parties.Party", "parties.PartyEmail", "parties.PartyPhone", "parties.PartyAddress",
    "parties.PartyRole", "parties.PartyRelationship",
    "customers.Customer", "customers.CustomerContact", "customers.CustomerAddress",
    "crm.Engagement", "crm.EngagementStageChange",
    "communications.Communication", "communications.CommunicationParticipant",
    "crm.Task", "crm.Lead",
    "orders.Quote", "orders.ShipmentGroup", "orders.QuoteLineItem",
    "orders.Order", "orders.SubOrder", "orders.OrderLineItem", "orders.DocumentContact",
    "orders.SalesInvoice", "payments.Payment",
    "accounting.JournalEntry", "accounting.JournalLine",
]

#: FKs blanked on every copy: logins, links to rows that are not copied.
NULL_OUT = {
    "parties.Party": ("user",),
    "customers.Customer": ("user", "account_holder"),
    "crm.Engagement": ("first_touch", "last_touch"),
    "communications.Communication": ("email_log",),
    "orders.SubOrder": ("purchase_order", "pick_ticket"),
    "orders.OrderLineItem": ("purchase_order_line", "pick_ticket_line"),
}


def rows_to_copy(label: str):
    model = apps.get_model(label)
    qs = model.objects.all()
    if label == "parties.Party":
        qs = qs.filter(is_self=False, merged_into__isnull=True, erased_at__isnull=True)
    return model, qs.order_by("pk").values()


def email_for(value: str, k: int) -> str:
    if not value:
        return value
    local, sep, domain = value.partition("@")
    return f"s{k}.{local}{sep}{domain}"[:254]


def suffix(value: str, k: int, limit: int) -> str:
    return f"{value}-S{k}"[:limit] if value else value


def rehash(value: str, k: int) -> str:
    return hashlib.sha256(f"{value}:{k}".encode()).hexdigest()[: len(value)] if value else value


def identifiers(label: str, row: dict, k: int) -> None:
    """Rewrite the fields a copy must not share with its original."""
    if label == "parties.Party":
        row["primary_email_normalized"] = email_for(row["primary_email_normalized"], k)
    elif label == "parties.PartyEmail":
        row["email"] = email_for(row["email"], k)
        row["email_normalized"] = email_for(row["email_normalized"], k)
    elif label == "customers.Customer":
        row["email"] = email_for(row["email"], k)
        row["stripe_customer_id"] = suffix(row["stripe_customer_id"], k, 64)
        # External ids key the party sync's ExternalRef lookup: a copy that
        # shares them with its original is "another row" to reconcile_parties.
        row["erpnext_name"] = suffix(row["erpnext_name"], k, 255)
        row["bc_id"] = suffix(row["bc_id"], k, 64)
    elif label == "customers.CustomerContact":
        row["email"] = email_for(row["email"], k)
    elif label == "customers.CustomerAddress":
        row["bc_address_id"] = suffix(row["bc_address_id"], k, 64)
    elif label == "crm.Lead":
        row["lead_number"] = suffix(row["lead_number"], k, 20)
        row["email"] = email_for(row["email"], k)
    elif label == "crm.Engagement":
        row["number"] = suffix(row["number"], k, 20)
    elif label == "orders.Quote":
        row["quote_number"] = suffix(row["quote_number"], k, 20)
    elif label == "orders.Order":
        row["order_number"] = suffix(row["order_number"], k, 20)
        row["slug"] = suffix(row["slug"][:44], k, 50).lower()
        row["stripe_payment_intent_id"] = suffix(row["stripe_payment_intent_id"], k, 255)
        row["external_id"] = suffix(row["external_id"], k, 255)
        row["bc_id"] = suffix(row["bc_id"], k, 64)
        row["erpnext_name"] = suffix(row["erpnext_name"], k, 255)
        row["d1_id"] = suffix(row["d1_id"], k, 64)
    elif label == "orders.SubOrder":
        row["sub_order_number"] = suffix(row["sub_order_number"], k, 25)
    elif label == "orders.SalesInvoice":
        row["invoice_number"] = suffix(row["invoice_number"], k, 20)
        row["payment_token"] = uuid.uuid4()
    elif label == "payments.Payment":
        row["payment_number"] = suffix(row["payment_number"], k, 20)
        row["transaction_id"] = suffix(row["transaction_id"], k, 255)
        row["idempotency_key"] = rehash(row["idempotency_key"], k)
    elif label == "accounting.JournalEntry":
        row["entry_number"] = suffix(row["entry_number"], k, 50)
        row["qbo_id"] = suffix(row["qbo_id"], k, 100)
        row["import_hash"] = rehash(row["import_hash"], k)
        row["posting_key"] = suffix(row["posting_key"], k, 320)


def shift(value, delta: dt.timedelta):
    if isinstance(value, dt.datetime):
        return value - delta
    if isinstance(value, dt.date):
        return value - delta
    return value


class Synthesizer:
    def __init__(self, factor: int, log=print):
        self.factor = factor
        self.log = log
        self.maps: dict[str, dict] = {label: {} for label in COPIED}
        self.sources = {label: rows_to_copy(label) for label in COPIED}
        self.counts: dict[str, int] = {}

    def assign_pks(self) -> None:
        """Every copy's primary key, before any row is built: self-references
        and the quote/order cycle then resolve in one pass."""
        for label, (model, rows) in self.sources.items():
            table = self.maps[label]
            for row in rows:
                table[row["id"]] = [uuid.uuid4() for _ in range(1, self.factor)]

    def build(self, label: str, model, row: dict, k: int) -> models.Model:
        delta = dt.timedelta(days=DAYS_PER_COPY * k)
        out = dict(row)
        out["id"] = self.maps[label][row["id"]][k - 1]
        for field in model._meta.concrete_fields:
            name = field.attname
            if field.primary_key:
                continue
            value = out.get(name)
            if field.is_relation:
                target = field.related_model._meta.label
                if field.name in NULL_OUT.get(label, ()):
                    out[name] = None
                elif target in self.maps and value is not None:
                    copies = self.maps[target].get(value)
                    out[name] = copies[k - 1] if copies else value  # an uncopied row keeps its key
            elif isinstance(field, (models.DateTimeField, models.DateField)) and value is not None:
                out[name] = shift(value, delta)
        identifiers(label, out, k)
        if label == "parties.PartyRelationship" and out["is_symmetric"] and out["from_party_id"] > out["to_party_id"]:
            out["from_party_id"], out["to_party_id"] = out["to_party_id"], out["from_party_id"]
        return model(**out)

    def copy_all(self) -> None:
        for label, (model, rows) in self.sources.items():
            rows = list(rows)
            batch, created = [], 0
            with keep_timestamps(model):
                for k in range(1, self.factor):
                    for row in rows:
                        batch.append(self.build(label, model, row, k))
                        if len(batch) >= BATCH:
                            model.objects.bulk_create(batch)
                            created += len(batch)
                            batch = []
                if batch:
                    model.objects.bulk_create(batch)
                    created += len(batch)
            self.counts[label] = created
            self.log(f"  {label:42} +{created:7d} (from {len(rows)})")


class keep_timestamps:
    """``bulk_create`` runs ``pre_save`` on ``auto_now`` / ``auto_now_add``
    fields, which would stamp every copy with the synthesis time. Shifted
    ``created_at`` values are what make the copies look like history."""

    def __init__(self, model):
        self.fields = [f for f in model._meta.concrete_fields
                       if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)]
        self.saved = [(f, f.auto_now, f.auto_now_add) for f in self.fields]

    def __enter__(self):
        for f in self.fields:
            f.auto_now = f.auto_now_add = False

    def __exit__(self, *exc):
        for f, now, add in self.saved:
            f.auto_now, f.auto_now_add = now, add


def table_counts() -> dict[str, int]:
    return {label: apps.get_model(label).objects.count() for label in COPIED}


def run_repairs(log) -> int:
    """The plan's post-synthesis sequence. Returns the number of checks that failed."""
    failed = 0
    # --limit 0: counts only, so a log of a scrubbed-copy run holds no names.
    steps = [
        ("reconcile_parties", ["--fix", "--limit", "0"]),
        ("backfill_crm", ["--step", "5"]),
        ("backfill_crm", ["--step", "6"]),
        ("reconcile_engagements", ["--fix"]),
    ]
    for name, args in steps:
        log(f"== {name} {' '.join(args)}")
        try:
            call_command(name, *args)
        except SystemExit as e:  # a command that exits non-zero
            log(f"   exited {e.code}")
        except Exception as e:  # CommandError: a fix that still reports drift; the checks below decide
            log(f"   {e}")
    for name, args in [("reconcile_engagements", ["--check"]), ("reconcile_parties", ["--check", "--limit", "0"]),
                       ("check_engagements", [])]:
        log(f"== {name} {' '.join(args)}")
        try:
            call_command(name, *args)
            log("   clean")
        except SystemExit as e:
            if e.code not in (0, None):
                failed += 1
                log(f"   NOT CLEAN (exit {e.code})")
        except Exception as e:  # CommandError
            failed += 1
            log(f"   NOT CLEAN: {e}")
    return failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Multiply a perf clone's business rows.")
    ap.add_argument("--factor", type=int, default=10, help="target volume as a multiple of today's (default 10)")
    ap.add_argument("--dry-run", action="store_true", help="copy, print counts, roll back")
    ap.add_argument("--skip-repairs", action="store_true", help="do not run the reconcile/backfill commands")
    args = ap.parse_args(argv)
    if args.factor < 2:
        ap.error("--factor must be at least 2")

    name = connection.settings_dict["NAME"]
    if "_perf_" not in name:
        sys.exit(f"refusing: database {name!r} is not a perf clone (name must contain '_perf_')")
    print(f"database {name}, factor {args.factor}, copies {args.factor - 1}")
    before = table_counts()
    t0 = time.perf_counter()
    synth = Synthesizer(args.factor)
    synth.assign_pks()
    with transaction.atomic(), suppressed():
        synth.copy_all()
        if args.dry_run:
            transaction.set_rollback(True)
            print("dry run: rolled back")
            return 0
    print(f"copied in {time.perf_counter() - t0:.1f}s")
    after = table_counts()
    print("\n| model | before | after |\n|---|---:|---:|")
    for label in COPIED:
        print(f"| {label} | {before[label]} | {after[label]} |")
    if args.skip_repairs:
        return 0
    failed = run_repairs(print)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
