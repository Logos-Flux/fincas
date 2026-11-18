from rest_framework import serializers

from .models import PickTicket, PickTicketLine


class PickTicketLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = PickTicketLine
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class PickTicketSerializer(serializers.ModelSerializer):
    lines = PickTicketLineSerializer(many=True, read_only=True)
    # PickTicket.save() assigns the next PT-nnnnn when this is blank; without
    # the override the unique, non-blank column is required of every caller.
    ticket_number = serializers.CharField(required=False, allow_blank=True, max_length=20)

    class Meta:
        model = PickTicket
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]
