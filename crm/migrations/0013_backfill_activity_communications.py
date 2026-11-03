# CRM Phase 3 step 6 (docs/crm/phase3-engagement.md 2.4, E8): every
# ActivityLog row becomes, with the same id, a Communication (call / email /
# meeting / note) or a done Task (task), linked through the lead's engagement
# (so after the lead step) or the customer's party. ActivityLog is frozen from
# here; /api/v1/crm/activities/ serves the copies (crm.activity_compat). The
# work is crm.backfill.backfill_activity_communications; also
# `manage.py backfill_crm --step 6`.

from django.db import migrations


def forwards(apps, schema_editor):
    from crm.backfill import backfill_activity_communications

    backfill_activity_communications(apps, log=lambda line: print(f"  crm step 6 (activities): {line}"))


def backwards(apps, schema_editor):
    from crm.backfill import reverse_activity_communications

    reverse_activity_communications(apps, log=lambda line: print(f"  crm step 6 (activities): {line}"))


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0012_backfill_lead_engagements"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
