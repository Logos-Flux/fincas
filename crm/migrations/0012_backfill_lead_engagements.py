# CRM Phase 3 step 6 (docs/crm/phase3-engagement.md 1.7, section 8): every
# lead without an engagement goes onto the thread by the intake rule. After
# step 5's quote backfill, so a converted lead attaches to its quote's
# engagement. The work is crm.backfill.backfill_lead_engagements on
# historical models; also `manage.py backfill_crm --step 6 [--dry-run]`.
# A converted lead whose quote has no engagement yet (an untouched draft)
# waits: `reconcile_engagements --fix` ensures the quote, then attaches it.

from django.db import migrations


def forwards(apps, schema_editor):
    from crm.backfill import backfill_lead_engagements

    backfill_lead_engagements(apps, log=lambda line: print(f"  crm step 6 (leads): {line}"))


def backwards(apps, schema_editor):
    from crm.backfill import reverse_lead_engagements

    reverse_lead_engagements(apps, log=lambda line: print(f"  crm step 6 (leads): {line}"))


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0011_lead_engagement"),  # after 0010, step 5's quote backfill
        ("communications", "0002_communication_schema"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
