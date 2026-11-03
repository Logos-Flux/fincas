# Bug batch 2026-10-15: the default sales pipeline ends with Closed/Signed
# after Verbal commit. New companies get it from the seed (crm.0008 and the
# post_save receiver); this appends it to existing ``sales`` pipelines.
# Skipped when GOLDEN_DEFAULT_PIPELINES replaces golden's pipeline: the
# deployment owns its stages then.

from django.conf import settings
from django.db import migrations


def add_stage(apps, schema_editor):
    if getattr(settings, "GOLDEN_DEFAULT_PIPELINES", None) is not None:
        return
    from crm.pipelines import append_stage
    from crm.registries import CLOSED_SIGNED_STAGE, GOLDEN_SALES_PIPELINE

    append_stage(
        GOLDEN_SALES_PIPELINE.key, CLOSED_SIGNED_STAGE,
        pipeline_model=apps.get_model("crm", "Pipeline"),
        stage_model=apps.get_model("crm", "Stage"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0018_lead_source_ai_chat"),
    ]

    # Reverse leaves the stage: deals may already sit in it (Stage is PROTECT).
    operations = [migrations.RunPython(add_stage, migrations.RunPython.noop)]
