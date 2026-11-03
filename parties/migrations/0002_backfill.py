"""Party backfill, data steps D2-D10 (docs/crm/phase2-party.md section 4).

One reversible ``RunPython`` per step, each calling the same function as
``manage.py backfill_parties --step <n>`` (``parties.backfill``). Idempotent:
rows already linked are skipped. Writes with ``bulk_create`` / ``update``
only, so no signals fire. Prints one summary line per step that did
something; the downstream rehearsal records them verbatim.

Long on a big deployment? ``migrate parties 0002 --fake`` and run
``backfill_parties --step all`` (or step by step) in the cutover window.
"""

from django.db import migrations

from parties.backfill import STEPS, format_line, run_step


_printed = []


def _forward(step):
    def forwards(apps, schema_editor):
        report = run_step(apps, step)
        if any(v not in (0, "") for v in report.values()):
            print(f"\n  {format_line(step, report)}", end="")
            _printed.append(step)
        if step == max(STEPS) and _printed:
            print()  # Django's " OK" goes on its own line
            _printed.clear()

    forwards.__name__ = f"d{step}_forwards"
    return forwards


def _backward(step):
    def backwards(apps, schema_editor):
        run_step(apps, step, reverse=True)

    backwards.__name__ = f"d{step}_backwards"
    return backwards


class Migration(migrations.Migration):

    dependencies = [
        ("parties", "0001_initial"),
        ("customers", "0005_party_links"),
        ("crm", "0006_party_links"),
        ("core", "0006_identity_schema"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(_forward(n), _backward(n)) for n in sorted(STEPS)]
