from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import PrincipalPermission
from core.throttling import CouponValidateThrottle

from .models import DiscountCode, DiscountCodeUsage
from .serializers import RedeemDiscountRequestSerializer, ValidateDiscountRequestSerializer


class ValidateDiscountView(APIView):
    """
    POST /api/v1/discounts/validate/

    Validates a discount code against a cart. Does NOT mark the code as used.
    """

    # Explicit auth (never the settings default, F-003) + the storefront
    # guest gate: the low-privilege storefront identity holds no discounts
    # permissions, so a leaked storefront token cannot probe or redeem codes.
    permission_classes = [PrincipalPermission]
    throttle_classes = [CouponValidateThrottle]

    def post(self, request):
        serializer = ValidateDiscountRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        code_str = serializer.validated_data["code"].strip()
        subtotal = Decimal(str(serializer.validated_data["subtotal"]))
        items = serializer.validated_data.get("items", [])

        # Look up code (case-insensitive)
        try:
            code = DiscountCode.objects.get(code__iexact=code_str)
        except DiscountCode.DoesNotExist:
            return Response({
                "valid": False,
                "message": "Invalid discount code.",
            })

        # Check active
        if not code.is_active:
            return Response({
                "valid": False,
                "message": "This code is no longer active.",
            })

        # Check date range
        now = timezone.now()
        if code.valid_from and now < code.valid_from:
            return Response({
                "valid": False,
                "message": "This code is not yet valid.",
            })
        if code.valid_to and now > code.valid_to:
            return Response({
                "valid": False,
                "message": "This code has expired.",
            })

        # Check usage limit
        if code.times_used >= code.max_uses:
            return Response({
                "valid": False,
                "message": "This code has already been used.",
            })

        # Calculate qualifying subtotal
        if code.applies_to == DiscountCode.AppliesTo.SPECIFIC_SKUS and items:
            allowed = set(s.lower() for s in code.allowed_skus)
            qualifying_subtotal = Decimal("0")
            for item in items:
                sku = str(item.get("sku", "")).lower()
                if sku in allowed:
                    qty = int(item.get("quantity", 1))
                    price = Decimal(str(item.get("price", 0)))
                    qualifying_subtotal += price * qty

            if qualifying_subtotal == 0:
                return Response({
                    "valid": False,
                    "message": "None of the items in your cart qualify for this code.",
                })
        else:
            qualifying_subtotal = subtotal

        # Check minimum order
        if code.minimum_order and qualifying_subtotal < code.minimum_order:
            return Response({
                "valid": False,
                "message": f"Minimum order of ${code.minimum_order} required for this code.",
            })

        # Calculate discount amount
        if code.discount_type == DiscountCode.DiscountType.PERCENT:
            discount_amount = (qualifying_subtotal * code.value / Decimal("100")).quantize(Decimal("0.01"))
        else:
            discount_amount = min(code.value, qualifying_subtotal)

        return Response({
            "valid": True,
            "discount_type": code.discount_type,
            "discount_value": float(code.value),
            "discount_amount": float(discount_amount),
            "message": (
                f"{float(code.value):g}% off applied!"
                if code.discount_type == DiscountCode.DiscountType.PERCENT
                else f"${float(code.value):g} off applied!"
            ),
        })


class RedeemDiscountView(APIView):
    """
    POST /api/v1/discounts/redeem/

    Called after order creation to mark a code as used.
    """

    # Same explicit auth + storefront gate as ValidateDiscountView.
    permission_classes = [PrincipalPermission]

    def post(self, request):
        serializer = RedeemDiscountRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        code_str = serializer.validated_data["code"].strip()
        order_number = serializer.validated_data["order_number"]
        customer_email = serializer.validated_data.get("customer_email", "")
        discount_applied = serializer.validated_data["discount_applied"]

        with transaction.atomic():
            try:
                code = DiscountCode.objects.select_for_update().get(code__iexact=code_str)
            except DiscountCode.DoesNotExist:
                return Response(
                    {"success": False, "message": "Invalid code."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if code.times_used >= code.max_uses:
                return Response(
                    {"success": False, "message": "Code already fully redeemed."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Atomic increment
            DiscountCode.objects.filter(pk=code.pk).update(
                times_used=F("times_used") + 1
            )

            # Record usage
            DiscountCodeUsage.objects.create(
                discount_code=code,
                order_number=order_number,
                customer_email=customer_email,
                discount_applied=discount_applied,
            )

        return Response({"success": True, "message": "Code redeemed."})
