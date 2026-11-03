"""``manage.py check_tighten``: the CRM Phase 2 step 11 (D13) pre-check.

Lists every row the tightening migrations (``customers.0007_tighten``,
``crm.0014_tighten``) would refuse: NULL ``Customer.company`` /
``Customer.party`` / ``CustomerContact.party`` / ``ActivityLog.company``, and
customer emails that differ only by case inside one tenant (the future
``uniq_customer_email_ci_company``). Exit 0 when clean, 1 otherwise. Read-only.
Emails are masked; rows are named by id.
"""

from django.core.management.base import BaseCommand

from customers.tighten import find_violations, format_violations, has_violations


class Command(BaseCommand):
    help = "List rows that would block the CRM Phase 2 step 11 tightening (exit 1 if any)."

    def handle(self, *args, **opts):
        report = find_violations()
        if not has_violations(report):
            self.stdout.write("check_tighten: clean (no NULLs, no case-duplicate customer emails)")
            return
        self.stdout.write("check_tighten: the step-11 tightening would refuse:")
        for line in format_violations(report):
            self.stdout.write(f"  {line}")
        raise SystemExit(1)
