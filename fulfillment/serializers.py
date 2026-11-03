from rest_framework import serializers

from .models import PickTicket, PickTicketLine


class PickTicketLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = PickTicketLine
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class PickTicketSerializer(serializers.ModelSerializer):
    lines = PickTicketLineSerializer(many=True, read_only=True)

    class Meta:
        model = PickTicket
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]
