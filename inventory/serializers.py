from rest_framework import serializers

from .models import Warehouse, WarehouseStock


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class WarehouseStockSerializer(serializers.ModelSerializer):
    available_qty = serializers.DecimalField(max_digits=10, decimal_places=3, read_only=True)

    class Meta:
        model = WarehouseStock
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]
