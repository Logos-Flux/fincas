"""DRF permission for the customer portal (identity-unification §3.4, §4)."""

from rest_framework.permissions import BasePermission


class IsPortalCustomer(BasePermission):
    """An active ``User`` holding a portal role, with an active linked Customer.

    Requires the request to have been authenticated by the portal token class
    (``core.portal_auth.PortalTokenAuthentication``), so a staff session or a
    golden-issued bearer never satisfies it even if the account also has a
    customer record. On success sets ``request.customer``.
    """

    def has_permission(self, request, view):
        from core.identity.resolve import customer_for_user
        from core.portal_auth import PortalTokenAuthentication
        from core.role_registry import portal_role_names

        user = request.user
        if not getattr(user, "is_authenticated", False) or not user.is_active:
            return False
        if not isinstance(request.successful_authenticator, PortalTokenAuthentication):
            return False
        if not user.groups.filter(name__in=portal_role_names()).exists():
            return False
        customer = customer_for_user(user)
        if customer is None or not customer.is_active:
            return False
        request.customer = customer
        return True
