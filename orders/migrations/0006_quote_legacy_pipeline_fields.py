"""CRM Phase 3 step 5: Quote's pipeline fields become ``legacy_*`` (spec 1.6).

State only. Each field is renamed in Python to ``legacy_<name>`` with
``db_column`` set to the old column and ``editable=False``, so nothing moves on
disk and a rollback loses nothing. Phase 3 step 12 drops the columns.
"""

from django.db import migrations, models

# (old name, field definition under the new name)
LEGACY = [
    ("sales_stage", models.CharField(
        choices=[("prospecting", "Prospecting"), ("qualification", "Qualification"),
                 ("needs_analysis", "Needs Analysis"), ("proposal_sent", "Proposal Sent"),
                 ("negotiation", "Negotiation"), ("verbal_commit", "Verbal Commit"),
                 ("closed_won", "Closed Won"), ("closed_lost", "Closed Lost")],
        db_column="sales_stage", default="prospecting", editable=False, max_length=20)),
    ("win_probability", models.IntegerField(db_column="win_probability", default=0, editable=False)),
    ("estimated_close_date", models.DateField(
        blank=True, db_column="estimated_close_date", editable=False, null=True)),
    ("weighted_value", models.DecimalField(
        db_column="weighted_value", decimal_places=2, default=0, editable=False, max_digits=12)),
    ("deal_size_category", models.CharField(
        blank=True, choices=[("small", "Small"), ("medium", "Medium"), ("large", "Large"),
                             ("enterprise", "Enterprise")],
        db_column="deal_size_category", editable=False, max_length=20)),
    ("loss_reason", models.CharField(
        blank=True, choices=[("price", "Price"), ("competitor", "Competitor"), ("timing", "Timing"),
                             ("budget", "Budget"), ("no_response", "No Response"),
                             ("scope", "Scope Change"), ("other", "Other")],
        db_column="loss_reason", editable=False, max_length=20)),
    ("loss_competitor", models.CharField(
        blank=True, db_column="loss_competitor", editable=False, max_length=255)),
    ("loss_notes", models.TextField(blank=True, db_column="loss_notes", editable=False)),
    ("lost_at", models.DateTimeField(blank=True, db_column="lost_at", editable=False, null=True)),
    ("follow_up_date", models.DateField(
        blank=True, db_column="follow_up_date", editable=False, null=True)),
    ("last_contacted_at", models.DateTimeField(
        blank=True, db_column="last_contacted_at", editable=False, null=True)),
    ("campaign", models.CharField(blank=True, db_column="campaign", editable=False, max_length=255)),
    ("lead_source", models.CharField(
        blank=True, db_column="lead_source", editable=False, max_length=255)),
]


def _state_operations():
    for old, field in LEGACY:
        yield migrations.RenameField("quote", old, f"legacy_{old}")
        yield migrations.AlterField("quote", f"legacy_{old}", field)


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0005_order_uniq_order_stripe_pi"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=list(_state_operations()),
            database_operations=[],
        ),
    ]
