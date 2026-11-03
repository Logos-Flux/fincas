"""Storefront service identity — least-privilege principal for guest flows (F-037).

The Qwik storefront in `storefront/` talks to this API as a *channel*, not a
user. Historically every storefront visitor shared the `api-service` superuser
JWT (docs/decisions-pending.md §2.2), which made a leaked storefront token a
full ERP compromise. This module defines the dedicated low-privilege identity
that replaces it:

- ``storefront-api`` user: active, NOT staff, NOT superuser, unusable password.
- ``storefront`` group: only the model permissions the storefront contract
  needs —

    products.view_product / view_productvariant / view_brand / view_category
        catalog reads (the storefront catalog GETs);
    pricing.view_pricelist / view_pricelistitem
        public price/tier reads;
    orders.add_order
        guest-checkout order creation (guest orders carry ``guest_email``
        and no Customer FK link).

  Nothing else. The identity cannot read orders/customers/suppliers/invoices,
  cannot write anything (the only unsafe permission in the group is
  ``orders.add_order``, and ``PrincipalPermission`` only honours it
  on the OrderViewSet ``create`` action), and is not staff, so Django admin,
  IsAdminUser viewsets, and /erp/ HTML views are all out of reach.

Enforcement is real, not cosmetic: ``core.permissions.PrincipalPermission`` is
pinned on every authenticated viewset (see the auth-matrix test), and it denies any
request from the storefront identity that is not in the contract above. A
leaked storefront token is near-powerless.

Since identity step 5 the storefront is the first instance of the generic
service-account pattern (``core/service_accounts.py``, spec §2.6): role
``storefront`` (``kind="service"``), the shared ``service_contract_allows``
check, and bearer resolution through ``resolve_token_subject``. Its token
issuance (public, ``sub="storefront"``) and its contract are unchanged.

Provisioning is idempotent — ``ensure_storefront_identity()`` always
converges to the same state (it re-adds any missing group permissions and
re-clears staff/superuser flags). It is called from:

- the data migration ``core.0002_storefront_identity`` (fresh installs);
- a ``post_migrate`` receiver (:func:`ensure_storefront_identity_after_migrate`),
  so every ``migrate`` repairs a missing or tampered identity;
- ``manage.py ensure_storefront_identity``, for operators and runbooks;
- token validation (``core.authentication``), lazily.

It is deliberately NOT called from ``CoreConfig.ready()``: app start must not
query or write the database (Django warns about it, and a ``manage.py`` run
against an unmigrated production restore used to create these rows early).
"""

from django.contrib.auth.hashers import is_password_usable, make_password
from django.contrib.auth.models import Group, Permission, User
from django.db import DEFAULT_DB_ALIAS
from django.utils import timezone

STOREFRONT_GROUP = "storefront"
STOREFRONT_USERNAME = "storefront-api"
STOREFRONT_EMAIL = "storefront@example.com"

# (app_label, codename) — the entire positive capability of the identity.
STOREFRONT_PERMISSIONS = (
    ("products", "view_product"),
    ("products", "view_productvariant"),
    ("products", "view_brand"),
    ("products", "view_category"),
    ("pricing", "view_pricelist"),
    ("pricing", "view_pricelistitem"),
    ("orders", "add_order"),
)


def ensure_storefront_identity(apps=None) -> User:
    """Create (or repair) the storefront group + user. Idempotent.

    Missing permissions are re-added and privilege-escalation flags
    (is_staff/is_superuser) are re-cleared on every call, so the function
    converges even if someone tampers with the account later. Permission
    rows for not-yet-migrated apps are skipped on this call and picked up
    by a later one (see module docstring).

    ``apps``: the data migration (``core.0002``) passes its historical
    registry, so it touches no live model (data migrations use historical
    models only; issue #88). Historical models have no model methods, so the
    password is handled with the hasher functions directly, and no live
    ``User`` signal fires (none would change this non-staff account).
    """
    if apps is not None:
        Group_ = apps.get_model("auth", "Group")
        Permission_ = apps.get_model("auth", "Permission")
        User_ = apps.get_model("auth", "User")
    else:
        Group_, Permission_, User_ = Group, Permission, User
    group, _ = Group_.objects.get_or_create(name=STOREFRONT_GROUP)
    for app_label, codename in STOREFRONT_PERMISSIONS:
        permission = Permission_.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if permission is not None:
            group.permissions.add(permission)

    # last_login is explicit (not a model default): the frozen
    # auth.0001 migration declares it NOT NULL, so a fresh migrate fails
    # without it. Found 2026-10-04 smoke-testing a scratch database.
    user, _ = User_.objects.get_or_create(
        username=STOREFRONT_USERNAME,
        defaults={
            "email": STOREFRONT_EMAIL,
            "is_active": True,
            "last_login": timezone.now(),
        },
    )
    if user.is_staff or user.is_superuser or not user.is_active:
        user.is_staff = False
        user.is_superuser = False
        user.is_active = True
    if is_password_usable(user.password):
        user.password = make_password(None)  # what set_unusable_password() stores
    user.save()
    user.groups.add(group)
    return user


def ensure_storefront_identity_after_migrate(sender, using=DEFAULT_DB_ALIAS, **kwargs):
    """``post_migrate`` receiver, run for every app's emission.

    Idempotent and cheap, so it runs each time rather than trying to spot
    "the last app" (Django emits ``post_migrate`` only for apps with a models
    module, so a model-less app appended last would silence that trigger; see
    ``core.roles.ensure_roles_after_migrate``). Permission rows for apps
    emitted later are picked up on their own emission.
    """
    if using != DEFAULT_DB_ALIAS:
        return
    ensure_storefront_identity()


def is_storefront_user(user) -> bool:
    """True when the authenticated principal is the storefront identity."""
    return (
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "username", None) == STOREFRONT_USERNAME
    )


def _contract_allows(request, view) -> bool:
    """Back-compat name: the storefront contract is now the generic service
    contract (``core.service_accounts.service_contract_allows``), which for
    this group (view perms + ``orders.add_order``) allows exactly what this
    function allowed before."""
    from .service_accounts import service_contract_allows

    return service_contract_allows(request, view)


def __getattr__(name):
    """Back-compat: ``StorefrontContractPermission`` is now ``PrincipalPermission``.

    The old class let every authenticated non-storefront user through. Its
    successor (core/permissions.py, identity-unification D6) keeps the
    storefront contract and additionally 403s non-staff humans. Imported
    lazily because core.permissions imports this module.
    """
    if name == "StorefrontContractPermission":
        from .permissions import PrincipalPermission

        return PrincipalPermission
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
