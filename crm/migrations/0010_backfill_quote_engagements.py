# CRM Phase 3 step 5 (docs/crm/phase3-engagement.md 1.6, section 7): one
# Engagement per quote revision chain that carries pipeline data, with
# history rows (source=backfill), linking the chain's quotes and orders. The
# work is crm.backfill.backfill_quote_engagements on historical models; also
# `manage.py backfill_crm --step 5 [--dry-run] [--accept-unmapped]`.
#
# Unmapped Quote.sales_stage strings stop the migration, listing them. To
# proceed anyway (open chains start at the first stage, closed ones keep
# stage NULL, each with metadata.unmapped_stage), add them to
# crm.backfill.STAGE_ALIASES or set GOLDEN_CRM_BACKFILL_ACCEPT_UNMAPPED=1 (env)
# or GOLDEN_CRM_BACKFILL_ACCEPT_UNMAPPED = True (settings). Run
# `backfill_crm --step 5 --dry-run` first to see the list.

from django.db import migrations


def forwards(apps, schema_editor):
    from crm.backfill import accept_unmapped_default, backfill_quote_engagements

    backfill_quote_engagements(
        apps, accept_unmapped=accept_unmapped_default(),
        log=lambda line: print(f"  crm step 5: {line}"),
    )


def backwards(apps, schema_editor):
    from crm.backfill import reverse_quote_engagements

    reverse_quote_engagements(apps, log=lambda line: print(f"  crm step 5: {line}"))


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0009_task_communication"),
        ("customers", "0006_rerun_identity_backfill"),
        ("orders", "0007_quote_order_engagement"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
