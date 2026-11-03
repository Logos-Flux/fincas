"""Accounting API serializers (Step 1b)."""

from rest_framework import serializers

from .models import FiscalPeriod


class FiscalPeriodSerializer(serializers.ModelSerializer):
    draft_entry_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = FiscalPeriod
        fields = [
            "id", "company", "name", "start_date", "end_date", "status",
            "closed_at", "draft_entry_count",
        ]
        read_only_fields = ["status", "closed_at", "draft_entry_count"]
