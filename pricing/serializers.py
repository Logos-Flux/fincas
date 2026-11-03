from rest_framework import serializers

from .models import (
    PriceList,
    PriceListItem,
    TaxRate,
)


class PriceListSerializer(serializers.ModelSerializer):
    class Meta:
        model = PriceList
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class PriceListItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PriceListItem
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class TaxRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaxRate
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]
