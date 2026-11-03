"""Identity backfill (identity-unification §3.3, step 3).

Run by the data migrations ``customers.0004_backfill_user_identity``,
``customers.0006_rerun_identity_backfill`` and (through
``core.identity.retire``) ``customers.0008_identity_retire_precheck``, and by
their tests. It reads the legacy columns that identity step 8 dropped, so it
only works on a historical registry from before ``customers.0009``. Every function takes an ``apps`` registry and only uses what a
historical model has: no custom model methods, managers or signals. Keep it
that way, because migrations import this module.

Forward pass, idempotent (a second run creates nothing):

1. Each ``Customer`` with ``supabase_user_id`` (it already has a portal
   login: it signed in or was invited) gets an ``auth.User`` named
   ``u-<customer pk hex>``, with an unusable password and
   ``is_active = customer.is_active``. The customer is linked to it
   (``Customer.user``), the user joins the ``customer`` role and gets
   ``UserIdentity(provider="supabase", subject=<supabase_user_id>)``.
   Customers without a portal login get no ``User``: those are created
   lazily, at first portal login or at invite time (step 4).
   ``Customer.supabase_user_id`` is left in place (step 8 drops it later).
2. Each ``UserProfile.supabase_uid`` on a profile linked to a user:

   - ``session:<pk>`` (the old login signal's placeholder) or
     ``restored:<pk>`` (written when ``core.0006`` is reversed): no row;
   - ``local:<pk>`` (``backfill_tenancy``'s placeholder): an inert
     ``provider="local"`` row;
   - anything else: an inert ``provider="legacy_supabase"`` row (the
     historical staff Supabase login).

   Neither provider name is ever configured (``core.identity``), so these
   rows record history and cannot log anyone in. A profile with no user is
   left alone and counted: it stays an FK target for historical records.
   ``backfill_tenancy --placeholder-uid-prefix`` other than ``local:`` lands
   as ``legacy_supabase``, which is equally inert.
3. Customers whose emails differ only by case are reported, never merged
   (§3.5).

A customer is refused (reported, not linked) when its linked user is staff
or superuser, or already holds another ``supabase`` subject, or when
``u-<customer pk hex>`` already exists unlinked: the forward pass never adopts
a user it did not create, and never gives a user a second subject of one
provider (``core.0012``'s constraint). A profile whose user already holds
another subject of the inert provider is reported the same way.

Reverse: deletes the reserved-provider rows, each customer's ``supabase``
row(s), and the ``u-<customer pk hex>`` users this pass created, unless such
a user has since become staff or gained another identity. Legacy columns were
never cleared, so nothing else needs restoring.
"""

from django.contrib.auth.hashers import make_password
from django.db.models import Count
from django.db.models.functions import Lower

from . import (
    GENERATED_USERNAME_PREFIX,
    LEGACY_SUPABASE_PROVIDER,
    LOCAL_PROVIDER,
    RESERVED_PROVIDER_NAMES,
    SUPABASE_PROVIDER,
)

CUSTOMER_ROLE = "customer"
SESSION_PLACEHOLDER_PREFIX = "session:"
# Written by reversing core.0006 for a NULL uid on a profile with no user.
RESTORED_PLACEHOLDER_PREFIX = "restored:"
LOCAL_PLACEHOLDER_PREFIX = "local:"


def generated_username(customer_pk) -> str:
    """``u-<32 hex>``: derived from the Customer pk, so a re-run finds it."""
    return f"{GENERATED_USERNAME_PREFIX}{customer_pk.hex}"


def profile_uid_provider(uid):
    """Provider name for a ``UserProfile.supabase_uid`` value, or None to skip."""
    if not uid or uid.startswith((SESSION_PLACEHOLDER_PREFIX, RESTORED_PLACEHOLDER_PREFIX)):
        return None
    if uid.startswith(LOCAL_PLACEHOLDER_PREFIX):
        return LOCAL_PROVIDER
    return LEGACY_SUPABASE_PROVIDER


def _models(apps):
    return (
        apps.get_model("customers", "Customer")._base_manager,
        apps.get_model("core", "UserProfile")._base_manager,
        apps.get_model("core", "UserIdentity")._base_manager,
        apps.get_model("auth", "User")._base_manager,
        apps.get_model("auth", "Group")._base_manager,
    )


def backfill_identities(apps) -> dict:
    """Forward pass. Returns a report of counts (see :func:`format_report`)."""
    customers, profiles, identities, users, groups = _models(apps)
    report = {
        "users_created": 0,
        "customers_linked": 0,
        SUPABASE_PROVIDER: 0,
        LEGACY_SUPABASE_PROVIDER: 0,
        LOCAL_PROVIDER: 0,
        "unlinked_profiles": 0,
        "conflicts": [],
        "profile_conflicts": [],
        "email_case_duplicates": [],
    }

    customer_group = None
    portal_customers = customers.filter(supabase_user_id__isnull=False).order_by("created_at", "pk")
    for customer in portal_customers.iterator():
        subject = str(customer.supabase_user_id)
        existing = identities.filter(provider=SUPABASE_PROVIDER, subject=subject).first()
        if existing is not None:
            if existing.user_id != customer.user_id:
                report["conflicts"].append(customer.pk)
            continue

        if customer.user_id is not None:
            user = users.get(pk=customer.user_id)
            # Never attach a self-signup login to a staff account (D5).
            refuse = user.is_staff or user.is_superuser
        else:
            user = None
            # The generated name already exists, but no customer links to it
            # (a re-run would have found the identity above): somebody made
            # it by hand. Never adopt it, so reverse only ever deletes users
            # this pass created.
            refuse = users.filter(username=generated_username(customer.pk)).exists()
        # One subject per provider per user (core.0012's constraint): a login
        # that already holds another supabase subject is reported, never
        # given a second one.
        if refuse or (user is not None and identities.filter(
                user=user, provider=SUPABASE_PROVIDER).exists()):
            report["conflicts"].append(customer.pk)
            continue
        if user is None:
            user = users.create(
                username=generated_username(customer.pk),
                email=customer.email.lower(),
                first_name=customer.first_name[:150],
                last_name=customer.last_name[:150],
                is_active=customer.is_active,
                is_staff=False,
                is_superuser=False,
                password=make_password(None),
                last_login=None,
            )
            report["users_created"] += 1
            customers.filter(pk=customer.pk).update(user=user)
            report["customers_linked"] += 1

        identities.create(
            user=user,
            provider=SUPABASE_PROVIDER,
            subject=subject,
            email=customer.email.lower(),
        )
        report[SUPABASE_PROVIDER] += 1
        if customer_group is None:
            # Not core.roles.ensure_role: migrations must use historical
            # models. That helper's only extra behaviour is adopting staff
            # when it creates "staff"; "customer" is not a staff role.
            customer_group, _ = groups.get_or_create(name=CUSTOMER_ROLE)
        user.groups.add(customer_group)

    for profile in profiles.filter(supabase_uid__isnull=False).order_by("pk").iterator():
        uid = profile.supabase_uid
        provider = profile_uid_provider(uid)
        if provider is None:
            continue
        if profile.user_id is None:
            report["unlinked_profiles"] += 1
            continue
        row = identities.filter(provider=provider, subject=uid).first()
        if row is None and identities.filter(user_id=profile.user_id, provider=provider).exists():
            # The user already holds another subject of this provider (one per
            # provider per user, core.0012): reported, never a second row.
            report["profile_conflicts"].append(profile.pk)
            continue
        if row is None:
            identities.create(provider=provider, subject=uid, user_id=profile.user_id)
            report[provider] += 1
        elif row.user_id != profile.user_id:
            report["profile_conflicts"].append(profile.pk)

    duplicate_emails = (
        customers.annotate(email_lower=Lower("email"))
        .values("email_lower")
        .annotate(n=Count("pk"))
        .filter(n__gt=1)
        .values_list("email_lower", flat=True)
    )
    for email in duplicate_emails:
        report["email_case_duplicates"].append(
            sorted(str(pk) for pk in customers.filter(email__iexact=email).values_list("pk", flat=True))
        )
    return report


def reverse_backfill(apps) -> dict:
    """Undo :func:`backfill_identities`. Returns counts of what it removed."""
    customers, _profiles, identities, users, _groups = _models(apps)
    report = {"identities_deleted": 0, "users_deleted": 0, "users_kept": []}

    deleted, _ = identities.filter(provider__in=RESERVED_PROVIDER_NAMES).delete()
    report["identities_deleted"] += deleted

    linked = customers.filter(user__isnull=False).order_by("pk")
    for customer in linked.iterator():
        user = users.get(pk=customer.user_id)
        generated = user.username == generated_username(customer.pk)
        if generated:
            # Every supabase row of a user this pass created, including one
            # left behind when the portal relinked the customer to a new uid.
            rows = identities.filter(provider=SUPABASE_PROVIDER, user_id=user.pk)
        elif customer.supabase_user_id is not None:
            rows = identities.filter(
                provider=SUPABASE_PROVIDER,
                subject=str(customer.supabase_user_id),
                user_id=user.pk,
            )
        else:
            continue
        deleted, _ = rows.delete()
        report["identities_deleted"] += deleted
        if not generated:
            continue  # not created by the forward pass
        if user.is_staff or user.is_superuser or identities.filter(user_id=user.pk).exists():
            report["users_kept"].append(user.username)
            continue
        customers.filter(pk=customer.pk).update(user=None)
        user.delete()
        report["users_deleted"] += 1
    return report


def format_report(report) -> list[str]:
    """Human-readable lines for a forward report; empty when nothing happened."""
    lines = []
    counted = (
        ("users_created", "customer users created"),
        ("customers_linked", "customers linked to a user"),
        (SUPABASE_PROVIDER, "supabase identities"),
        (LEGACY_SUPABASE_PROVIDER, "legacy_supabase identities (inert)"),
        (LOCAL_PROVIDER, "local placeholder identities (inert)"),
        ("unlinked_profiles", "profiles with a uid but no user (left in place)"),
    )
    for key, label in counted:
        if report[key]:
            lines.append(f"  identity backfill: {label}: {report[key]}")
    if report["conflicts"]:
        lines.append(
            "  identity backfill: customers NOT linked (username or link conflict): "
            + ", ".join(str(pk) for pk in report["conflicts"])
        )
    if report["profile_conflicts"]:
        lines.append(
            "  identity backfill: profiles whose uid is already another user's identity: "
            + ", ".join(str(pk) for pk in report["profile_conflicts"])
        )
    if report["email_case_duplicates"]:
        lines.append(
            "  identity backfill: customer emails that differ only by case "
            f"(not merged): {len(report['email_case_duplicates'])} group(s)"
        )
        for group in report["email_case_duplicates"]:
            lines.append(f"    - {', '.join(group)}")
    return lines
