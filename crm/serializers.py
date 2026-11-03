from rest_framework import serializers
from rest_framework.exceptions import APIException

from core.sidecar_api import MetadataCompatMixin

from .models import Lead

#: Where clients read about the move.
ENGAGEMENT_DOCS = "docs/crm/phase3-engagement.md"


class LeadTriaged(APIException):
    """400 ``lead_triaged``: a workflow field of a lead already on an engagement."""

    status_code = 400
    default_code = "lead_triaged"

    def __init__(self, fields, lead):
        from orders.serializers import _engagement_url

        super().__init__()
        self.detail = {
            "detail": (
                f"{', '.join(fields)}: the lead is on an engagement (CRM Phase 3); "
                "work the deal there."
            ),
            "code": self.default_code,
            "fields": list(fields),
            "engagement": str(lead.engagement_id),
            "engagement_url": _engagement_url(lead.engagement_id),
            "docs": ENGAGEMENT_DOCS,
        }


class _LeadCompatMixin(serializers.Serializer):
    """``status`` derived from the engagement once triaged (one release, spec 1.7);
    a write changing a frozen workflow field is refused with 400 ``lead_triaged``."""

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "status" in data:
            data["status"] = instance.compat_status()
        return data

    def to_internal_value(self, data):
        validated = super().to_internal_value(data)
        lead = self.instance
        if lead is not None and getattr(lead, "engagement_id", None):
            # Refused only when the value changes, so a GET-then-PUT of the
            # same payload (which carries the derived status) still saves.
            changed = [k for k in Lead.FROZEN_WHEN_TRIAGED
                       if k in validated and validated[k] != _current(lead, k)]
            if changed:
                raise LeadTriaged(changed, lead)
            for k in Lead.FROZEN_WHEN_TRIAGED:
                validated.pop(k, None)
        return validated


def _current(lead, name):
    if name == "status":
        return lead.compat_status()
    return getattr(lead, name)


class LeadListSerializer(_LeadCompatMixin, serializers.ModelSerializer):
    class Meta:
        model = Lead
        fields = [
            "id", "lead_number", "first_name", "last_name", "email",
            "status", "source", "estimated_value", "engagement", "created_at",
        ]


class LeadDetailSerializer(MetadataCompatMixin, _LeadCompatMixin, serializers.ModelSerializer):
    # Served from Communication + Task since ActivityLog froze (spec 2.4).
    activities = serializers.SerializerMethodField()
    # Attribution captured at intake (CRM Phase 6 3.5): stored in
    # ``metadata`` under registered keys and turned into the lead's
    # Touchpoint by ``crm.intake``. Create only; write only.
    landing_url = serializers.URLField(max_length=2048, required=False, allow_blank=True, write_only=True)
    click_id_kind = serializers.ChoiceField(choices=("gclid", "gbraid", "wbraid", "fbclid", "msclkid", "other"),
                                            required=False, allow_blank=True, write_only=True)
    click_id = serializers.CharField(max_length=255, required=False, allow_blank=True, write_only=True)
    utm_term = serializers.CharField(max_length=255, required=False, allow_blank=True, write_only=True)
    utm_content = serializers.CharField(max_length=255, required=False, allow_blank=True, write_only=True)
    visitor_id = serializers.CharField(max_length=255, required=False, allow_blank=True, write_only=True)

    class Meta:
        model = Lead
        fields = "__all__"
        # party: parties.sync only; engagement: crm.intake only.
        read_only_fields = ["id", "created_at", "updated_at", "party", "engagement"]

    def validate(self, attrs):
        from .intake import LEAD_ATTRIBUTION_KEYS

        attrs = super().validate(attrs)
        given = {k: attrs.pop(k) for k in LEAD_ATTRIBUTION_KEYS if k in attrs}
        if self.instance is not None and any(given.values()):
            raise serializers.ValidationError(
                {k: "Attribution is captured when the lead is created." for k, v in given.items() if v})
        if bool(given.get("click_id")) != bool(given.get("click_id_kind")):
            raise serializers.ValidationError({"click_id": "click_id and click_id_kind go together."})
        self._attribution = {k: v.strip() for k, v in given.items() if v and v.strip()}
        return attrs

    def create(self, validated_data):
        attribution = getattr(self, "_attribution", None)
        if attribution:
            # Before the save: Lead.save() runs intake, which reads them.
            validated_data["metadata"] = {**(validated_data.get("metadata") or {}), **attribution}
        return super().create(validated_data)

    def get_activities(self, obj):
        from core.managers import scope_to_company

        from .activity_compat import lead_activities

        request = self.context.get("request")
        if request is None:
            return lead_activities(obj, scope=lambda qs: qs.filter(company_id=obj.company_id))
        return lead_activities(obj, scope=lambda qs: scope_to_company(qs, request))
