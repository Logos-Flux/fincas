from rest_framework import serializers

from .models import DiscountCode, DiscountCodeUsage


class DiscountCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiscountCode
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at", "times_used"]


class DiscountCodeUsageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiscountCodeUsage
        fields = "__all__"


class ValidateDiscountRequestSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=50)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2)
    items = serializers.ListField(
        child=serializers.DictField(), required=False, default=list
    )


class RedeemDiscountRequestSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=50)
    order_number = serializers.CharField(max_length=50)
    customer_email = serializers.CharField(max_length=255, required=False, default="")
    discount_applied = serializers.DecimalField(max_digits=10, decimal_places=2)
