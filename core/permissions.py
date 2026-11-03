"""``PrincipalPermission`` — the default DRF permission for the staff API (D6).

``IsAuthenticated`` used to be the staff gate, because every ``auth.User`` was
staff or a service. Identity unification (docs/identity-unification.md §4)
makes customers ``auth.User`` rows too, so "authenticated" stops meaning
"staff". This class decides per principal:

- **anonymous** → denied (401 via the authenticator's challenge);
- **a service account** (``core.service_accounts.is_service_account``:
  ``svc-*`` per-key accounts, the storefront identity, members of a
  ``kind="service"`` role) → exactly its contract: the model permissions of
  its groups, per action (``service_contract_allows``). Checked *before* the
  superuser shortcut, so a service account stays inside its contract even if
  someone flags it superuser or staff. For the storefront this is the old
  ``StorefrontContractPermission`` contract unchanged;
- **superuser** → allowed. That covers the legacy ``API_KEY`` account and
  ``api-service`` bearers: the dedicated ``api-service`` superuser
  (spec §2.6), never a human;
- **any other human** → allowed only when it is active, holds a
  ``staff=True`` role (``GOLDEN_ROLES``) and carries the ``is_staff`` flag
  derived from it (:func:`is_staff_principal`);
  everyone else, including a customer-only account with a session, gets
  **403**.

It is ``REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]`` in ``golden_erp/settings/base.py`` and is
also pinned explicitly on every staff viewset (the auth-matrix test refuses
viewsets that rely on the settings default, because test settings inherit
dev's ``AllowAny``). Portal endpoints keep their own ``IsPortalCustomer``.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from .service_accounts import is_service_account, service_contract_allows


def is_staff_principal(user) -> bool:
    """True for a superuser, or an active human holding a staff role.

    The one place that answers "may this account use the back office / staff
    API?". Superusers are break-glass and always pass.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    # Superuser first. The legacy API_KEY account (``api-service``) is a
    # superuser by spec §2.6 and is refused when inactive by its resolver;
    # Django refuses sessions of inactive users on its own.
    if getattr(user, "is_superuser", False):
        return True
    if not getattr(user, "is_active", False):
        return False
    # Both the derived flag and the role (identity step 2), so the answer
    # fails closed whenever they disagree: a hand-set flag never adopted into
    # a role, or a member of a pre-existing group that merely shares a staff
    # role's name and has not been through `sync_roles` (docs/roles.md).
    from .roles import has_staff_role

    return bool(getattr(user, "is_staff", False)) and has_staff_role(user)


class PrincipalPermission(BasePermission):
    """Staff API gate: staff/superuser humans, services by contract, else 403."""

    message = "A staff account is required for this endpoint."

    def has_permission(self, request, view):
        user = request.user
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if is_service_account(user):
            return service_contract_allows(request, view)
        return is_staff_principal(user)
