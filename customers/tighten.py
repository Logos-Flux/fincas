"""CRM Phase 2 step 11 (D13) pre-check: what would violate the tightening.

``docs/crm/phase2-party.md`` section 4 (D13) makes ``Customer.company``,
``Customer.party``, ``CustomerContact.party`` and ``ActivityLog.company``
NOT NULL, and replaces ``Customer.email unique=True`` with
``uniq_customer_email_ci_company`` (``Lower("email")`` + ``company``). This
module finds every row that would break one of those, so the tightening
migrations can refuse with a list instead of failing half-way on an
``IntegrityError``. ``manage.py check_tighten`` runs the same check.

The report names rows by id. Emails are masked (``j…@example.com``): the
list goes to migration output and CI logs, which must not carry full PII.

Works on the migration state's models (``apps``) and on the live ones.

Both ``0007_tighten`` migrations import this module, so treat it as part of
them: a later change here changes what those migrations check on replay.
"""

from django.db.models import Count
from django.db.models.functions import Lower

CHECKS = (
    # (report key, label, operator fix)
    ("customer_company_null", "customers with no company",
     "set GOLDEN_PORTAL_COMPANY (or keep exactly one Company) and run `manage.py reconcile_parties --fix`; "
     "several tenants: `manage.py backfill_tenancy --company <id> --also customers.Customer`"),
    ("customer_party_null", "customers with no party",
     "`manage.py reconcile_parties --fix` (or `backfill_parties --step 3`)"),
    ("contact_party_null", "customer contacts with no party",
     "`manage.py reconcile_parties --fix` (or `backfill_parties --step 4`)"),
    ("activity_company_null", "activity log rows with no company",
     "`manage.py backfill_tenancy --company <id> --also crm.ActivityLog`"),
    ("customer_email_case_duplicates", "customer emails that differ only by case within a tenant",
     "resolve each group (docs/crm/phase2-party.md 4.2): merge the people in the merge-review queue, "
     "then give the duplicate commerce account its own address or retire it"),
)


def mask_email(value: str) -> str:
    """``j…@example.com``: the first character and the domain only."""
    local, at, domain = (value or "").rpartition("@")
    if not at:
        return "…"
    return f"{local[:1]}…@{domain}"


def _models(apps):
    if apps is None:
        from django.apps import apps as live

        apps = live
    return (
        apps.get_model("customers", "Customer"),
        apps.get_model("customers", "CustomerContact"),
        apps.get_model("crm", "ActivityLog"),
    )


def _ids(queryset) -> list[str]:
    return sorted(str(pk) for pk in queryset.values_list("pk", flat=True))


def find_violations(apps=None) -> dict:
    """Every row the step-11 constraints would reject, keyed as in ``CHECKS``."""
    customer, contact, activity = _models(apps)
    customers = customer._base_manager.all()
    report = {
        "customer_company_null": _ids(customers.filter(company__isnull=True)),
        "customer_party_null": _ids(customers.filter(party__isnull=True)),
        "contact_party_null": _ids(contact._base_manager.filter(party__isnull=True)),
        "activity_company_null": _ids(activity._base_manager.filter(company__isnull=True)),
        "customer_email_case_duplicates": [],
    }
    # NULL-company rows are reported above; the constraint only sees them once
    # they have a tenant, so they are grouped with their eventual one there.
    groups = (
        customers.filter(company__isnull=False)
        .annotate(email_ci=Lower("email"))
        .values("company_id", "email_ci")
        .annotate(n=Count("pk"))
        .filter(n__gt=1)
        .order_by("company_id", "email_ci")
    )
    for group in groups:
        rows = customers.annotate(email_ci=Lower("email")).filter(
            company_id=group["company_id"], email_ci=group["email_ci"])
        report["customer_email_case_duplicates"].append({
            "company": str(group["company_id"]),
            "email": mask_email(group["email_ci"]),
            "customers": _ids(rows),
        })
    return report


def has_violations(report: dict) -> bool:
    return any(report[key] for key, _label, _fix in CHECKS)


def format_violations(report: dict) -> list[str]:
    """Human-readable lines: one header per failing check, then its rows."""
    lines = []
    for key, label, fix in CHECKS:
        rows = report[key]
        if not rows:
            continue
        lines.append(f"{label}: {len(rows)}  (fix: {fix})")
        for row in rows:
            if isinstance(row, dict):
                lines.append(f"  - company {row['company']} {row['email']}: {', '.join(row['customers'])}")
            else:
                lines.append(f"  - {row}")
    return lines


class TightenRefused(RuntimeError):
    """Raised by the tightening migrations while any row would violate D13."""


def refuse_if_violations(apps, schema_editor=None) -> None:
    """``RunPython`` guard: abort the migration, listing the offending rows."""
    report = find_violations(apps)
    if has_violations(report):
        raise TightenRefused(
            "CRM Phase 2 step 11 (D13) refused: resolve these rows first, then re-run migrate. "
            "`manage.py check_tighten` lists the same.\n" + "\n".join(format_violations(report))
        )
