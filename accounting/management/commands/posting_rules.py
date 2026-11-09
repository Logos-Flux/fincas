"""Show or set a company's document posting rules and provider overrides.

    manage.py posting_rules show [--company <uuid>]
    manage.py posting_rules set --doc-type payment \\
        --receivable "Accounts receivable (A/R)" --cash "Undeposited Funds" \\
        --fee "Stripe processing fees" [--deposit "Customer deposits"] [--company <uuid>]
    manage.py posting_rules set-provider --provider paypal \\
        --cash "PayPal Account" [--fee "Channel selling fees:PayPal fees"]
    manage.py posting_rules set-provider --provider ebay --cash "Undeposited Funds" --no-fee
    manage.py posting_rules unset-provider --provider paypal

Accounts are named by ``account_number``, exact ``full_name``, or bare
``name``, tried in that order. A reference that matches several accounts
("Insurance" under two parents) is refused, never resolved by order; an
unknown one is refused too.
``--deposit`` (payment rule only, optional) names the liability a payment with
no invoice credits instead of A/R; it moves to A/R when the invoice is issued.
``set`` creates the rule or replaces the named accounts on it, leaving the
others as they were, then ``full_clean``s it, so a rule missing a required
account is refused before anything is written (AD-13: postings never guess
accounts). Every write is one transaction.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting.models import Account, DocumentPostingRule, PaymentProviderPostingOverride
from core.models import Company

RULE_FIELDS = (
    "receivable", "revenue", "tax", "discount", "shipping_income",
    "cash", "fee", "inventory", "payable", "deposit",
)


class Command(BaseCommand):
    help = "Show or set document posting rules and per-provider cash/fee overrides."

    def add_arguments(self, parser):
        parser.add_argument(
            "action", choices=("show", "set", "set-provider", "unset-provider")
        )
        parser.add_argument("--company", help="Company pk (default: the only company).")
        parser.add_argument(
            "--doc-type", choices=[v for v, _ in DocumentPostingRule.DocType.choices],
            help="set: which rule.",
        )
        parser.add_argument("--provider", help="set-provider/unset-provider: payment provider.")
        parser.add_argument(
            "--no-fee", action="store_true",
            help="set-provider: clear the override's fee account (use the rule's).",
        )
        parser.add_argument(
            "--no-deposit", action="store_true",
            help="set: clear the payment rule's deposit account (payments credit A/R again).",
        )
        for field in RULE_FIELDS:
            parser.add_argument(
                f"--{field.replace('_', '-')}", dest=field, metavar="ACCOUNT",
                help=f"{field}_account, by account number or full name.",
            )

    def handle(self, *args, **options):
        company = self._company(options.get("company"))
        action = options["action"]
        if action == "show":
            self._show(company)
        elif action == "set":
            self._set(company, options)
        elif action == "set-provider":
            self._set_provider(company, options)
        else:
            self._unset_provider(company, options)

    # -- actions -------------------------------------------------------------

    def _show(self, company):
        self.stdout.write(f"Posting rules for {company.name} ({company.pk})")
        rules = {r.doc_type: r for r in DocumentPostingRule.objects.filter(company=company)}
        for doc_type, _label in DocumentPostingRule.DocType.choices:
            rule = rules.get(doc_type)
            if rule is None:
                self.stdout.write(f"  {doc_type}: MISSING")
                continue
            self.stdout.write(f"  {doc_type}:")
            for field in DocumentPostingRule.REQUIRED[doc_type]:
                account = getattr(rule, field)
                self.stdout.write(f"    {field:<24} {self._label(account)}")
            for field in DocumentPostingRule.OPTIONAL.get(doc_type, ()):
                account = getattr(rule, field)
                label = self._label(account) if account is not None else "(none: optional)"
                self.stdout.write(f"    {field:<24} {label}")
        overrides = PaymentProviderPostingOverride.objects.filter(company=company).order_by("provider")
        self.stdout.write("  provider overrides:" + ("" if overrides else " none"))
        for o in overrides:
            fee = self._label(o.fee_account) if o.fee_account_id else "(rule's fee account)"
            self.stdout.write(f"    {o.provider:<12} cash {self._label(o.cash_account)}; fee {fee}")

    def _set(self, company, options):
        doc_type = options.get("doc_type")
        if not doc_type:
            raise CommandError("set needs --doc-type.")
        self._refuse_flags(options, ("provider", "no_fee"), "set")
        given = {f: options[f] for f in RULE_FIELDS if options.get(f)}
        clear_deposit = bool(options.get("no_deposit"))
        if clear_deposit and "deposit" in given:
            raise CommandError("Pass --deposit ACCOUNT or --no-deposit, not both.")
        if not given and not clear_deposit:
            raise CommandError("set needs at least one --<field> ACCOUNT.")
        with transaction.atomic():
            rule, created = DocumentPostingRule.objects.get_or_create(
                company=company, doc_type=doc_type
            )
            for field, ref in given.items():
                setattr(rule, f"{field}_account", self._account(company, ref))
            if clear_deposit:
                rule.deposit_account = None
                given["deposit"] = None
            self._clean(rule)
            rule.save()
        self.stdout.write(
            f"{'Created' if created else 'Updated'} {doc_type} rule for {company.name}: "
            + ", ".join(f"{f}={self._label(getattr(rule, f + '_account'))}" for f in given)
        )

    def _set_provider(self, company, options):
        provider = options.get("provider")
        if not provider:
            raise CommandError("set-provider needs --provider.")
        if not options.get("cash"):
            raise CommandError("set-provider needs --cash ACCOUNT.")
        self._refuse_flags(
            options,
            tuple(f for f in RULE_FIELDS if f not in ("cash", "fee")) + ("doc_type", "no_deposit"),
            "set-provider",
        )
        if options.get("fee") and options.get("no_fee"):
            raise CommandError("Pass --fee ACCOUNT or --no-fee, not both.")
        with transaction.atomic():
            override, created = PaymentProviderPostingOverride.objects.get_or_create(
                company=company, provider=provider,
                defaults={"cash_account": self._account(company, options["cash"])},
            )
            override.cash_account = self._account(company, options["cash"])
            if options.get("fee"):
                override.fee_account = self._account(company, options["fee"])
            elif options.get("no_fee"):
                override.fee_account = None
            self._clean(override)
            override.save()
        fee = self._label(override.fee_account) if override.fee_account_id else "(rule's fee account)"
        self.stdout.write(
            f"{'Created' if created else 'Updated'} {provider} override for {company.name}: "
            f"cash={self._label(override.cash_account)}, fee={fee}"
        )

    def _unset_provider(self, company, options):
        provider = options.get("provider")
        if not provider:
            raise CommandError("unset-provider needs --provider.")
        deleted, _ = PaymentProviderPostingOverride.objects.filter(
            company=company, provider=provider
        ).delete()
        if not deleted:
            raise CommandError(f"No {provider} override for {company.name}.")
        self.stdout.write(f"Removed {provider} override for {company.name}.")

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _refuse_flags(options, names, action):
        stray = [n for n in names if options.get(n)]
        if stray:
            flags = ", ".join("--" + n.replace("_", "-") for n in stray)
            raise CommandError(f"{action} does not take {flags}.")

    @staticmethod
    def _company(pk):
        if pk:
            try:
                return Company.objects.get(pk=pk)
            except (Company.DoesNotExist, ValueError, ValidationError) as exc:
                raise CommandError(f"No company {pk!r}.") from exc
        companies = list(Company.objects.order_by("created_at")[:2])
        if len(companies) != 1:
            raise CommandError("Pass --company: there is not exactly one company.")
        return companies[0]

    @staticmethod
    def _account(company, ref):
        qs = Account.objects.filter(company=company)
        matches = list(qs.filter(account_number=ref)) if ref else []
        if not matches:
            matches = list(qs.filter(full_name=ref))
        if not matches:
            matches = list(qs.filter(name=ref))
        if len(matches) > 1:
            raise CommandError(
                f"Account {ref!r} matches {len(matches)} accounts; use the account number."
            )
        if not matches:
            raise CommandError(f"No account {ref!r} at {company.name}.")
        return matches[0]

    @staticmethod
    def _clean(obj):
        try:
            obj.full_clean()
        except ValidationError as exc:
            raise CommandError("; ".join(
                f"{field}: {' '.join(msgs)}" if field != "__all__" else " ".join(msgs)
                for field, msgs in exc.message_dict.items()
            )) from exc

    @staticmethod
    def _label(account):
        if account is None:
            return "(none)"
        number = f"{account.account_number} " if account.account_number else ""
        return f"{number}{account.full_name or account.name}"
