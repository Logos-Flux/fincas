"""Shared test helpers for principals (identity-unification step 1).

The staff API's default permission is ``core.permissions.PrincipalPermission``:
an authenticated human must hold a staff role. API tests therefore build
their actor with :func:`make_staff_user` instead of a bare ``create_user()``,
which is now a non-staff account and gets 403 everywhere on the staff API.
"""

from __future__ import annotations

from django.contrib.auth.models import User


def make_staff_user(username, email=None, password=None, *, role="staff", **extra) -> User:
    """``User.objects.create_user`` for a back-office staff account.

    Staff access is role membership (identity step 2): the user joins
    ``role`` (default the built-in ``staff``) and ``is_staff`` is derived.
    """
    from core.roles import grant_role

    user = User.objects.create_user(username, email, password, **extra)
    grant_role(user, role)
    return user


def portal_tenant():
    """The portal's Company (Party spec 2.4).

    With exactly one Company it is the portal tenant without
    ``GOLDEN_PORTAL_COMPANY``; with none the portal refuses (503), because a
    Party cannot be tenant-less. Call it before creating portal customers.
    """
    from core.models import Company

    company = Company.objects.order_by("created_at").first()
    return company or Company.objects.create(name="Portal Tenant")


def make_portal_customer(email, *, subject=None, provider="supabase", **fields):
    """A Customer with a portal login, as the step-4 resolver leaves it.

    Creates the customer-only ``User`` (role ``customer``), links it and adds
    ``UserIdentity(provider, subject)``. Returns ``(customer, subject)``.
    """
    import uuid

    from core.identity.models import UserIdentity
    from core.identity.resolve import create_portal_user
    from customers.models import Customer

    subject = str(subject or uuid.uuid4())
    first = fields.pop("first_name", "Portal")
    last = fields.pop("last_name", "Customer")
    user = create_portal_user(email, first, last, ["customer"])
    customer = Customer.objects.create(
        first_name=first, last_name=last, email=email, user=user, **fields,
    )
    UserIdentity.objects.create(user=user, provider=provider, subject=subject, email=email)
    return customer, subject


# The one lock order (resolve.lock_login_rows) every path that moves a login takes.
LOGIN_LOCK_ORDER = ("customers_customer", "parties_party", "auth_user")


def login_lock_order(queries) -> list:
    """The login tables in the order the captured ``FOR UPDATE`` queries locked them.

    Consecutive locks on one table collapse, so a path that keeps the order
    yields :data:`LOGIN_LOCK_ORDER` or a subsequence of it; going back to an
    earlier table shows up as a repeat. Only explicit ``FOR UPDATE`` statements
    are seen, not the row locks an UPDATE takes.
    """
    order = []
    for query in queries:
        sql = query["sql"]
        if "FOR UPDATE" not in sql:
            continue
        for table in LOGIN_LOCK_ORDER:
            if f'FROM "{table}"' in sql and (not order or order[-1] != table):
                order.append(table)
    return order
