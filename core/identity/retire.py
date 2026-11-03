"""Identity step 8: retire the legacy identity columns (spec §8 row 8).

Run by ``customers.0008_identity_retire_precheck`` before
``customers.0009`` drops ``Customer.supabase_user_id`` and ``core.0011``
drops ``UserProfile.supabase_uid``. Like ``core.identity.backfill`` it takes
an ``apps`` registry and uses historical models only, because a migration
imports it. It only makes sense against a state that still has both columns.

Forward (:func:`retire_legacy_columns`):

1. Re-run :func:`~core.identity.backfill.backfill_identities` (idempotent).
   Every legacy value the step-3 backfill can capture becomes a
   ``UserIdentity`` row now, whatever happened since step 3 ran.
2. Archive orphan uids: a real Supabase uid on a ``UserProfile`` with no user
   (a historical FK target, spec §7) is copied to
   ``UserProfile.legacy_external_id``. It cannot be a ``UserIdentity``: that
   row needs a user. Placeholders (``session:``, ``restored:``, ``local:``)
   are not ids and are not archived.
3. Verify, and refuse the drop (:class:`LegacyIdentityNotCaptured`) when a
   value would be lost:

   - a ``Customer.supabase_user_id`` with no ``UserIdentity`` of that subject
     under a real (non-reserved) provider. The step-3 backfill refuses to
     capture a customer whose user is staff or superuser (D5), or whose
     generated username ``u-<pk hex>`` is held by an unlinked user;
   - a ``UserProfile.supabase_uid`` on a profile with a user and no matching
     inert row (``legacy_supabase`` / ``local``).

   - a ``Customer.supabase_user_id`` whose subject is held only by users
     that have no customer: the column is then the only link between that
     login and a customer.

   The error lists customer and profile primary keys only, never an email or
   uid. A subject that belongs to another customer's user (the resolver
   relinked it) is reported, not refused: that login already resolves
   through the identity table, which is all the code reads since step 4, so
   the drop changes no login. Such a customer with no user of its own loses
   its ``account_holder`` (it held one only through the column).

Reverse (:func:`restore_legacy_columns`, after both columns are back): fills
``Customer.supabase_user_id`` from the customer's user's UUID-shaped subject
under a real provider, and ``UserProfile.supabase_uid`` from the archive or
the user's inert row. Best effort: a value another row already holds is
skipped, never forced.
"""

import uuid

from . import LEGACY_SUPABASE_PROVIDER, RESERVED_PROVIDER_NAMES
from .backfill import backfill_identities, format_report, profile_uid_provider


class LegacyIdentityNotCaptured(RuntimeError):
    """A legacy identity value has no ``UserIdentity`` row; the drop would lose it."""


def _models(apps):
    return (
        apps.get_model("customers", "Customer")._base_manager,
        apps.get_model("core", "UserProfile")._base_manager,
        apps.get_model("core", "UserIdentity")._base_manager,
    )


def retire_legacy_columns(apps) -> dict:
    """Capture, archive and verify. Raises :class:`LegacyIdentityNotCaptured`."""
    customers, profiles, identities = _models(apps)
    report = {
        "backfill": backfill_identities(apps),
        "archived_profiles": 0,
        "relinked_customers": [],
        "account_holders_cleared": 0,
        "uncaptured_customers": [],
        "uncaptured_profiles": [],
    }

    real = identities.exclude(provider__in=RESERVED_PROVIDER_NAMES)
    rows = customers.filter(supabase_user_id__isnull=False).order_by("pk")
    for pk, value, user_id in rows.values_list("pk", "supabase_user_id", "user_id").iterator():
        holders = set(real.filter(subject=str(value)).values_list("user_id", flat=True))
        if user_id in holders:
            continue
        # Relinked only when the subject now reaches another customer. A
        # holder with no customer means this column is the only thing tying
        # the login to a customer: dropping it would lose the link.
        if holders and customers.filter(user_id__in=holders).exists():
            report["relinked_customers"].append(pk)
        else:
            report["uncaptured_customers"].append(pk)

    legacy = profiles.filter(supabase_uid__isnull=False).order_by("pk")
    for pk, uid, user_id in legacy.values_list("pk", "supabase_uid", "user_id").iterator():
        provider = profile_uid_provider(uid)
        if provider is None:
            continue  # session:/restored: placeholders carry nothing
        if user_id is None:
            if provider == LEGACY_SUPABASE_PROVIDER:
                profiles.filter(pk=pk).update(legacy_external_id=uid)
                report["archived_profiles"] += 1
            continue  # a user-less local:<pk> is a placeholder, not an id
        if not identities.filter(provider=provider, subject=uid).exists():
            report["uncaptured_profiles"].append(pk)

    if report["uncaptured_customers"] or report["uncaptured_profiles"]:
        raise LegacyIdentityNotCaptured("\n".join(refusal_lines(report)))

    # "Account holder" means a portal login (parties.mirror.has_login). A
    # relinked customer with no user counted as one only through the legacy
    # column, so it stops being one when the column goes.
    report["account_holders_cleared"] = customers.filter(
        pk__in=report["relinked_customers"], user__isnull=True, account_holder__isnull=False,
    ).update(account_holder=None)
    return report


def refusal_lines(report) -> list[str]:
    lines = [
        "Identity step 8 refuses to drop the legacy identity columns: these values "
        "have no UserIdentity row and would be lost.",
    ]
    if report["uncaptured_customers"]:
        lines.append(
            "  Customer.supabase_user_id, customer pks: "
            + ", ".join(str(pk) for pk in report["uncaptured_customers"])
        )
        lines.append(
            "    No UserIdentity holds the value, or only one on a user with no customer. "
            "The identity backfill refuses a customer whose user is staff/superuser, "
            "or already holds another supabase subject (one subject per provider "
            "per user), or whose u-<customer pk hex> is held by an unlinked user."
        )
    if report["uncaptured_profiles"]:
        lines.append(
            "  UserProfile.supabase_uid, profile pks: "
            + ", ".join(str(pk) for pk in report["uncaptured_profiles"])
        )
    lines.append(
        "  Fix each one by hand, then migrate again: attach the login to the right "
        "user as a UserIdentity (provider, subject) and link the customer (a user "
        "keeps one subject per provider: delete the stale row first), or, if "
        "the login is dead, set the column to NULL with SQL (manage.py dbshell: the "
        "models no longer have the field). docs/identity-unification.md, "
        "'As built: step 8'."
    )
    return lines


def format_retire_report(report) -> list[str]:
    """Human-readable lines for a successful forward pass; empty when quiet."""
    lines = list(format_report(report["backfill"]))
    if report["archived_profiles"]:
        lines.append(
            "  identity step 8: user-less profile uids archived to legacy_external_id: "
            f"{report['archived_profiles']}"
        )
    if report["relinked_customers"]:
        lines.append(
            "  identity step 8: customers whose legacy uid is another user's login "
            "(relinked; dropped, no login changes): "
            + ", ".join(str(pk) for pk in report["relinked_customers"])
        )
    if report["account_holders_cleared"]:
        lines.append(
            "  identity step 8: account_holder cleared on customers with no user: "
            f"{report['account_holders_cleared']}"
        )
    if report["backfill"]["customers_linked"]:
        lines.append(
            "  identity step 8: customers linked by this run; run "
            "`manage.py backfill_parties --step 7` to set their Party.user"
        )
    return lines


def restore_legacy_columns(apps) -> dict:
    """Reverse: refill both legacy columns where the data allows it."""
    customers, profiles, identities = _models(apps)
    report = {"customers": 0, "profiles": 0}

    real = identities.exclude(provider__in=RESERVED_PROVIDER_NAMES)
    linked = customers.filter(user__isnull=False, supabase_user_id__isnull=True).order_by("pk")
    for pk, user_id in linked.values_list("pk", "user_id").iterator():
        for subject in real.filter(user_id=user_id).order_by("created_at").values_list("subject", flat=True):
            try:
                value = uuid.UUID(subject)
            except ValueError:
                continue
            if customers.filter(supabase_user_id=value).exists():
                continue
            customers.filter(pk=pk).update(supabase_user_id=value)
            report["customers"] += 1
            break

    for pk, user_id, archived in profiles.filter(supabase_uid__isnull=True).order_by("pk").values_list(
        "pk", "user_id", "legacy_external_id",
    ).iterator():
        candidates = [archived] if archived else []
        if user_id is not None:
            candidates += list(
                identities.filter(user_id=user_id, provider__in=RESERVED_PROVIDER_NAMES)
                .order_by("created_at")
                .values_list("subject", flat=True)
            )
        for uid in candidates:
            if not profiles.filter(supabase_uid=uid).exists():
                profiles.filter(pk=pk).update(supabase_uid=uid)
                report["profiles"] += 1
                break
    return report
