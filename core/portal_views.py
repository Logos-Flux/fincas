"""
Portal API views — admin-facing endpoints for managing portal customers.

Invites go through the configured portal provider's ``invite`` capability
(``core.identity.providers``; only ``SupabaseJWT`` implements it). The
customer's login is a ``UserIdentity`` on the customer's own ``auth.User``,
created here if the customer has none (identity-unification §3.4).
Responses keep the ``supabase_user_id`` key for API compatibility: it is the
provider subject (also returned as ``subject``). The ``Customer`` column of that
name was dropped by identity step 8; nothing is dual-written any more.
"""

import logging

from django.db import IntegrityError, transaction
from rest_framework import serializers, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from customers.models import Customer

from .identity.models import UserIdentity
from .identity.providers import InviteFailed, invite_provider
from .throttling import PortalInviteResendThrottle

logger = logging.getLogger(__name__)


class PortalInviteSerializer(serializers.Serializer):
    # Customer PKs are UUIDs (BaseModel) — an IntegerField here 400s every
    # real customer, so both invite endpoints take the UUID.
    customer_id = serializers.UUIDField(help_text="Customer PK to invite")


def _error(message, code=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({"error": message, **extra}, status=code)


def _load(request):
    """Validate the body and fetch the Customer. Returns ``(customer, None)`` or ``(None, Response)``."""
    ser = PortalInviteSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    customer_id = ser.validated_data["customer_id"]
    customer = Customer.objects.select_related("user").filter(pk=customer_id).first()
    if customer is None:
        return None, _error(f"Customer {customer_id} not found", status.HTTP_404_NOT_FOUND)
    if not customer.email:
        return None, _error("Customer has no email address")
    return customer, None


def _provider_or_error():
    provider = invite_provider()
    if provider is None:
        return None, _error(
            "No portal provider able to send invites is configured",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return provider, None


def _portal_identity(customer, provider):
    if customer.user_id is None:
        return None
    return UserIdentity.objects.filter(user_id=customer.user_id, provider=provider.config.name).first()


def _send(provider, customer):
    """Call the provider. Returns ``(subject, None)`` or ``(None, Response)``."""
    try:
        subject = provider.invite(
            customer.email,
            {"customer_id": str(customer.pk), "full_name": customer.full_name},
        )
    except InviteFailed as exc:
        logger.error("Portal invite failed for customer %s: %s %s", customer.pk, exc, exc.detail)
        extra = {"detail": exc.detail} if exc.detail else {}
        return None, _error(str(exc), exc.status, **extra)
    return subject, None


class _StaffHeld(Exception):
    pass


def _staff_held():
    return _error(
        "This customer is linked to a staff account; it cannot get a portal login from "
        "this provider", status.HTTP_409_CONFLICT,
    )


class PortalInviteView(APIView):
    """Create the customer's portal login and send an invite email.

    POST /api/v1/portal/invite/
    Auth: API key or session (admin/staff only)

    Body: {"customer_id": "<uuid>"}

    1. 409 if the customer's user already holds an identity for the provider.
    2. The provider creates its account and sends the email (Supabase Admin).
    3. The customer's ``User`` is created if missing (customer-only, unusable
       password, the provider's ``roles_on_create``) and gets
       ``UserIdentity(provider, subject)``.
    """

    permission_classes = [IsAdminUser]

    def post(self, request):
        customer, error = _load(request)
        if error is not None:
            return error
        provider, error = _provider_or_error()
        if error is not None:
            return error

        existing = _portal_identity(customer, provider)
        if existing is not None:
            return _error(
                "Customer already has a portal account", status.HTTP_409_CONFLICT,
                supabase_user_id=existing.subject, subject=existing.subject,
            )
        if customer.user is not None:
            from .identity.resolve import is_privileged

            if is_privileged(customer.user) and not provider.config.trusted:
                # The resolver refuses untrusted logins for staff accounts
                # (D5), so such an invite could never be used. Re-checked
                # under the row lock below.
                return _staff_held()

        subject, error = _send(provider, customer)
        if error is not None:
            return error

        from .identity.resolve import (
            Claims,
            HeldIdentity,
            attach_identity,
            create_portal_user,
            is_privileged,
            lock_login_rows,
        )

        claims = Claims(subject=subject, email=customer.email, email_verified=False, raw={})
        try:
            with transaction.atomic():
                # Customer → Party → User, the order login and erasure take
                # (resolve.lock_login_rows): saving the user below runs the
                # party sync, which writes the customer's parties.
                lock_login_rows(customers=[customer.pk],
                                parties=[customer.party_id, customer.account_holder_id])
                locked = Customer.objects.get(pk=customer.pk)
                if {locked.party_id, locked.account_holder_id} - {
                        customer.party_id, customer.account_holder_id}:
                    # Relinked before the lock; the Customer lock still
                    # serialises us against every other writer of it.
                    lock_login_rows(parties=[locked.party_id, locked.account_holder_id])
                taken = UserIdentity.objects.filter(
                    provider=provider.config.name, subject=subject,
                ).exclude(user_id=locked.user_id).exists()
                if taken:
                    raise IntegrityError("subject belongs to another user")
                user = locked.user
                if user is not None and is_privileged(user) and not provider.config.trusted:
                    raise _StaffHeld()
                if user is None:
                    user = create_portal_user(
                        locked.email, locked.first_name, locked.last_name,
                        provider.config.roles_on_create,
                    )
                    locked.user = user
                    locked.save(update_fields=["user", "updated_at"])
                else:
                    from .roles import grant_role

                    for role in provider.config.roles_on_create:
                        grant_role(user, role)
                attach_identity(provider, user, claims, login=False)
        except _StaffHeld:
            return _staff_held()
        except HeldIdentity:
            # A concurrent invite or login attached a subject since the 409
            # check above; never replace it from here.
            return _error("Customer already has a portal account", status.HTTP_409_CONFLICT)
        except IntegrityError:
            logger.error(
                "Portal invite: %s subject %s for customer %s already belongs to another user",
                provider.config.name, subject, customer.pk,
            )
            return _error(
                "The provider returned a login that belongs to another account",
                status.HTTP_409_CONFLICT,
            )

        logger.info(
            "Portal invite sent: customer=%s user=%s provider=%s subject=%s",
            customer.pk, user.pk, provider.config.name, subject,
        )
        return Response(
            {
                "customer_id": customer.pk,
                "email": customer.email,
                "user_id": user.pk,
                "provider": provider.config.name,
                "subject": subject,
                "supabase_user_id": subject,
            },
            status=status.HTTP_201_CREATED,
        )


class PortalInviteResendView(APIView):
    """Re-send the invite email for an expired portal invite.

    POST /api/v1/portal/invite/resend/
    Auth: API key or session (admin/staff only). Rate-limited per admin
    (email-sending endpoint — must not become a spam cannon).

    Body: {"customer_id": "<uuid>"}

    The customer must already hold a portal login (a ``UserIdentity`` for
    the provider) — first-time invites go through POST /api/v1/portal/invite/.
    """

    permission_classes = [IsAdminUser]
    throttle_classes = [PortalInviteResendThrottle]

    def post(self, request):
        customer, error = _load(request)
        if error is not None:
            return error
        provider, error = _provider_or_error()
        if error is not None:
            return error

        identity = _portal_identity(customer, provider)
        if identity is None:
            return _error(
                "Customer has no portal account yet; use POST /api/v1/portal/invite/ first",
                status.HTTP_409_CONFLICT,
            )

        subject, error = _send(provider, customer)
        if error is not None:
            return error
        if subject != identity.subject:
            logger.warning(
                "Portal invite resend: provider returned subject %s for customer %s, "
                "which holds %s; link left unchanged", subject, customer.pk, identity.subject,
            )

        logger.info(
            "Portal invite resent: customer=%s provider=%s subject=%s",
            customer.pk, provider.config.name, identity.subject,
        )
        return Response(
            {
                "customer_id": customer.pk,
                "email": customer.email,
                "user_id": customer.user_id,
                "provider": provider.config.name,
                "subject": identity.subject,
                "supabase_user_id": identity.subject,
                "resent": True,
            },
            status=status.HTTP_200_OK,
        )
