"""``POST /api/v1/parties/parties/{id}/erase/`` (docs/crm/phase2-party.md 5.2, 7.4).

Body ``{"confirm": "<display_name>", "reason": "..."}``. Needs
``parties.erase_party`` (superusers hold it; the built-in ``staff`` role does
not). The storefront service identity and customer-only accounts get 403,
another tenant's party is a 404. The rest of the 5.2 staff API arrives with
step 8 (``parties.views``); this route is mounted on its own so it does not
depend on that PR.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import PrincipalPermission
from core.storefront import is_storefront_user

from .erasure import ErasureError, erase_party
from .models import Party


class CanErasePermission(BasePermission):
    """``parties.erase_party`` (superusers hold every permission). Never a service."""

    message = "Erasing a party needs the parties.erase_party permission."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and not is_storefront_user(user)
            and user.has_perm("parties.erase_party")
        )


class ErasureRequestSerializer(serializers.Serializer):
    confirm = serializers.CharField(help_text="The party's display name, typed again.")
    reason = serializers.CharField(
        max_length=1000, help_text="Why; kept in the audit log. Must not repeat the person's data.")


class PartyEraseView(APIView):
    permission_classes = [PrincipalPermission, CanErasePermission]

    @extend_schema(
        tags=["parties"], request=ErasureRequestSerializer, responses={200: OpenApiTypes.OBJECT},
        description="Erase this person's personal data (7.4). Not reversible. The response lists "
                    "what was kept (orders, invoices, referenced addresses) and why.",
    )
    def post(self, request, pk):
        party = get_object_or_404(Party.objects.for_request(request), pk=pk)
        body = ErasureRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        if body.validated_data["confirm"].strip() != party.display_name.strip():
            raise ValidationError({"confirm": "Type the party's display name exactly to confirm the erasure."})
        try:
            result = erase_party(party, request.user, body.validated_data["reason"], source="api")
        except ErasureError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(result.as_dict(), status=status.HTTP_200_OK)
