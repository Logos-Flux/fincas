"""Staff REST API serializers for communications (docs/crm/phase3-engagement.md 5.2).

``company`` is read-only; every reference is scoped to the requester's
tenant. Creation goes through ``communications.ingest.record`` (kind and
direction validated against the registry, participants resolved, never a
party created). The raw provider payload is never in these serializers: it
has its own endpoint behind ``communications.view_raw_communication``.
"""

from __future__ import annotations

from rest_framework import serializers

from core.sidecar_api import MetadataCompatMixin
from crm.api_serializers import ScopedEngagement, ScopedParty

from .models import Communication, CommunicationParticipant, CommunicationRaw

#: Editable on a manual communication (``is_editable``); everything else is a record.
EDITABLE_FIELDS = ("subject", "body_text", "summary", "occurred_at", "ended_at", "duration_seconds",
                   "status", "engagement", "party", "metadata")


class ParticipantSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommunicationParticipant
        fields = ["id", "role", "party", "party_email", "party_phone", "address_raw",
                  "address_normalized", "display_name"]
        read_only_fields = fields


class ParticipantInputSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=CommunicationParticipant.Role.choices)
    address = serializers.CharField(required=False, allow_blank=True, max_length=254, default="")
    display_name = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")
    party = ScopedParty(required=False, allow_null=True, default=None)


class CommunicationSerializer(MetadataCompatMixin, serializers.ModelSerializer):
    engagement = ScopedEngagement(required=False, allow_null=True)
    party = ScopedParty(required=False, allow_null=True)
    participants = ParticipantSerializer(many=True, read_only=True)
    participant_input = ParticipantInputSerializer(many=True, write_only=True, required=False,
                                                   source="participant_specs")
    has_raw = serializers.SerializerMethodField()

    class Meta:
        model = Communication
        fields = [
            "id", "company", "kind", "direction", "status", "subject", "body_text", "summary",
            "occurred_at", "ended_at", "duration_seconds", "provider", "thread_key", "engagement",
            "party", "performed_by", "email_log", "is_editable", "metadata", "participants",
            "participant_input", "has_raw", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company", "provider", "performed_by", "email_log",
                            "is_editable", "created_at", "updated_at"]

    def get_has_raw(self, obj) -> bool:
        annotated = getattr(obj, "has_raw_payload", None)  # CommunicationViewSet annotates it
        if annotated is not None:
            return bool(annotated)
        return CommunicationRaw._base_manager.filter(communication_id=obj.pk).exists()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # The input key is write-only; the read side is ``participants``.
        data.pop("participant_input", None)
        return data


class CommunicationUpdateSerializer(CommunicationSerializer):
    """PATCH: only ``EDITABLE_FIELDS``; kind and direction are fixed at logging."""

    class Meta(CommunicationSerializer.Meta):
        read_only_fields = [f for f in CommunicationSerializer.Meta.fields if f not in EDITABLE_FIELDS]


class CommunicationRawSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommunicationRaw
        fields = ["communication", "provider", "schema_version", "payload", "content_hash",
                  "fetched_at"]
        read_only_fields = fields

