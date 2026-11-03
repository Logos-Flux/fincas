# CRM Phase 3 step 6 (docs/crm/phase3-engagement.md 2.5, E9): every
# historical EmailLog without a Communication gets one through
# communications.email_link (purchase orders skipped), oldest first. After
# step 5, so quote and order emails land on their engagement. Runs on this
# migration's historical models only (`apps` is passed all the way down; see
# communications/backfill.py and issue #88). Also `manage.py backfill_crm --step 6`.

from django.db import migrations


def forwards(apps, schema_editor):
    from communications.backfill import backfill_email_communications

    backfill_email_communications(apps, log=lambda line: print(f"  crm step 6 (emails): {line}"))


def backwards(apps, schema_editor):
    from communications.backfill import reverse_email_communications

    reverse_email_communications(apps, log=lambda line: print(f"  crm step 6 (emails): {line}"))


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0002_communication_schema"),
        ("crm", "0013_backfill_activity_communications"),
        ("orders", "0007_quote_order_engagement"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
