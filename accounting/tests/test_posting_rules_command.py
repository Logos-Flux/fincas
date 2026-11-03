"""`manage.py posting_rules`: show/set rules and provider overrides by account
number or full name, refusing ambiguity, unknown accounts, and incomplete rules."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounting.models import Account, DocumentPostingRule, PaymentProviderPostingOverride
from core.models import Company


def _acct(company, number, name, atype, subtype, full_name=""):
    return Account.objects.create(
        company=company, account_number=number, name=name, full_name=full_name,
        account_type=atype, account_subtype=subtype,
    )


class PostingRulesCommandTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Rule Co")
        c = self.company
        self.ar = _acct(c, "1200", "A/R", "asset", "accounts_receivable", "Accounts receivable (A/R)")
        self.holding = _acct(c, "", "Undeposited Funds", "asset", "undeposited_funds", "Undeposited Funds")
        self.paypal = _acct(c, "", "PayPal Account", "asset", "bank", "PayPal Account")
        self.stripe_fees = _acct(c, "6020", "Stripe processing fees", "expense", "processing_fee_expense",
                                 "Stripe processing fees")
        self.paypal_fees = _acct(c, "", "PayPal fees", "expense", "cost_of_goods_sold",
                                 "Channel selling fees:PayPal fees")
        # Two accounts named "Insurance" under different parents: bare name is ambiguous.
        _acct(c, "", "Insurance", "expense", "operating_expense", "Expenses:Insurance")
        _acct(c, "", "Insurance", "expense", "cost_of_goods_sold", "COGS:Insurance")

    def _run(self, *args):
        out = StringIO()
        call_command("posting_rules", *args, stdout=out)
        return out.getvalue()

    def test_set_creates_rule_by_number_and_full_name(self):
        out = self._run(
            "set", "--doc-type", "payment", "--receivable", "1200",
            "--cash", "Undeposited Funds", "--fee", "Stripe processing fees",
        )
        self.assertIn("Created payment rule", out)
        rule = DocumentPostingRule.objects.get(company=self.company, doc_type="payment")
        self.assertEqual(rule.receivable_account, self.ar)
        self.assertEqual(rule.cash_account, self.holding)
        self.assertEqual(rule.fee_account, self.stripe_fees)

    def test_set_updates_only_named_fields(self):
        self._run("set", "--doc-type", "payment", "--receivable", "1200",
                  "--cash", "Undeposited Funds", "--fee", "6020")
        out = self._run("set", "--doc-type", "payment", "--cash", "PayPal Account")
        self.assertIn("Updated payment rule", out)
        rule = DocumentPostingRule.objects.get(company=self.company, doc_type="payment")
        self.assertEqual(rule.cash_account, self.paypal)
        self.assertEqual(rule.fee_account, self.stripe_fees)

    def test_set_refuses_incomplete_rule_and_writes_nothing(self):
        with self.assertRaises(CommandError) as ctx:
            self._run("set", "--doc-type", "payment", "--cash", "Undeposited Funds")
        self.assertIn("missing accounts", str(ctx.exception))
        self.assertFalse(DocumentPostingRule.objects.filter(company=self.company).exists())

    def test_set_refuses_ambiguous_and_unknown_accounts(self):
        with self.assertRaises(CommandError) as ctx:
            self._run("set", "--doc-type", "payment", "--receivable", "1200",
                      "--cash", "Undeposited Funds", "--fee", "Insurance")
        self.assertIn("matches 2 accounts", str(ctx.exception))
        with self.assertRaises(CommandError) as ctx:
            self._run("set", "--doc-type", "payment", "--receivable", "1200",
                      "--cash", "Undeposited Funds", "--fee", "Nope")
        self.assertIn("No account 'Nope'", str(ctx.exception))

    def test_set_provider_then_show_then_unset(self):
        self._run("set", "--doc-type", "payment", "--receivable", "1200",
                  "--cash", "Undeposited Funds", "--fee", "6020")
        out = self._run("set-provider", "--provider", "paypal", "--cash", "PayPal Account",
                        "--fee", "Channel selling fees:PayPal fees")
        self.assertIn("Created paypal override", out)
        override = PaymentProviderPostingOverride.objects.get(company=self.company, provider="paypal")
        self.assertEqual(override.cash_account, self.paypal)
        self.assertEqual(override.fee_account, self.paypal_fees)

        out = self._run("show")
        self.assertIn("payment:", out)
        self.assertIn("Undeposited Funds", out)
        self.assertIn("paypal", out)
        self.assertIn("sales_invoice: MISSING", out)

        self._run("unset-provider", "--provider", "paypal")
        self.assertFalse(PaymentProviderPostingOverride.objects.filter(company=self.company).exists())
        with self.assertRaises(CommandError):
            self._run("unset-provider", "--provider", "paypal")

    def test_set_provider_refuses_unknown_provider(self):
        with self.assertRaises(CommandError) as ctx:
            self._run("set-provider", "--provider", "venmo", "--cash", "PayPal Account")
        self.assertIn("Unknown payment provider", str(ctx.exception))
        self.assertFalse(PaymentProviderPostingOverride.objects.exists())

    def test_no_fee_clears_and_stray_flags_refused(self):
        self._run("set", "--doc-type", "payment", "--receivable", "1200",
                  "--cash", "Undeposited Funds", "--fee", "6020")
        self._run("set-provider", "--provider", "paypal", "--cash", "PayPal Account",
                  "--fee", "Channel selling fees:PayPal fees")
        out = self._run("set-provider", "--provider", "paypal", "--cash", "PayPal Account", "--no-fee")
        self.assertIn("(rule's fee account)", out)
        override = PaymentProviderPostingOverride.objects.get(company=self.company, provider="paypal")
        self.assertIsNone(override.fee_account)
        with self.assertRaises(CommandError) as ctx:
            self._run("set-provider", "--provider", "paypal", "--cash", "PayPal Account",
                      "--fee", "6020", "--no-fee")
        self.assertIn("not both", str(ctx.exception))
        with self.assertRaises(CommandError) as ctx:
            self._run("set-provider", "--provider", "paypal", "--cash", "PayPal Account",
                      "--receivable", "1200")
        self.assertIn("does not take --receivable", str(ctx.exception))
        with self.assertRaises(CommandError) as ctx:
            self._run("set", "--doc-type", "refund", "--receivable", "1200",
                      "--cash", "Undeposited Funds", "--provider", "paypal")
        self.assertIn("does not take --provider", str(ctx.exception))

    def test_malformed_company_is_a_command_error(self):
        with self.assertRaises(CommandError) as ctx:
            self._run("show", "--company", "not-a-uuid")
        self.assertIn("No company", str(ctx.exception))

    def test_company_required_when_several(self):
        Company.objects.create(name="Second Co")
        with self.assertRaises(CommandError) as ctx:
            self._run("show")
        self.assertIn("--company", str(ctx.exception))
        out = self._run("show", "--company", str(self.company.pk))
        self.assertIn("Rule Co", out)
