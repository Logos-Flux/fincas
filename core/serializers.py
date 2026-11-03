from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .managers import scope_to_company
from .models import AuditLog, Company, Team, UserProfile


class GuardedDocumentSerializer(serializers.ModelSerializer):
    """Enforce guarded state machines and server-authoritative totals.

    Two rules, applied to any document serializer subclassing this mixin:

    - ``status`` is rejected on updates. Legal moves are defined solely by
      the model's ``VALID_TRANSITIONS`` map and applied only through the
      DRF ``transition`` action; a direct PATCH of ``status`` (even to a
      map-legal value) returns a 400 pointing at that endpoint. Creation
      may still set the initial status. Derived writers (sub-order roll-up,
      payment reconcile) use the ORM and never pass through serializers,
      so they are unaffected.
    - ``SERVER_TOTAL_FIELDS`` (subtotal/tax/grand_total/balance style
      aggregates) are rejected on both create and update: totals are
      computed by ``recalculate_totals()`` (and the payment ledger), not
      caller-dictated. Adjustment inputs such as discount_percent,
      shipping_cost, or handling_fee stay writable.

    Subclasses set ``SERVER_TOTAL_FIELDS``; both default to empty.
    """

    SERVER_TOTAL_FIELDS = frozenset()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        errors = {}
        if self.instance is not None and "status" in attrs:
            errors["status"] = (
                "Status cannot be set directly. Use the transition endpoint "
                "POST .../transition/ with body {'status': <target>} — legal "
                "moves are defined by the document's transition map."
            )
        totals = sorted(set(attrs) & set(self.SERVER_TOTAL_FIELDS))
        for field in totals:
            errors[field] = (
                f"{field} is server-computed. Update the line items and call "
                "POST .../recalculate/ instead of writing it directly."
            )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        team = attrs.get("team", getattr(self.instance, "team", None))
        company = attrs.get("company", getattr(self.instance, "company", None))
        if "team" in attrs and team is not None and team != getattr(self.instance, "team", None):
            # Setting someone's team decides how their future records are
            # stamped (phase4-fields.md 2.1): core.manage_teams, own tenant,
            # active teams only.
            request = self.context.get("request")
            user = getattr(request, "user", None)
            if user is None or not user.has_perm("core.manage_teams"):
                raise serializers.ValidationError({"team": "Setting a team needs core.manage_teams."})
            from .views import request_company

            mine = request_company(request)
            if mine is not None and team.company_id != mine.pk:
                raise serializers.ValidationError({"team": "The team belongs to another company."})
            if not team.is_active:
                raise serializers.ValidationError({"team": "The team is archived."})
        if team is not None and team.company_id != getattr(company, "pk", None):
            raise serializers.ValidationError({"team": "The team belongs to another company."})
        return attrs


@extend_schema_field(OpenApiTypes.UUID)
class ScopedTeamField(serializers.PrimaryKeyRelatedField):
    """A team of the requester's tenant (phase4-fields.md 2.1): another
    tenant's team id is a 400 "does not exist", never a cross-tenant link."""

    def get_queryset(self):
        request = self.context.get("request")
        if request is None:
            return Team.objects.none()
        return scope_to_company(Team.objects.all(), request)

    def to_internal_value(self, data):
        team = super().to_internal_value(data)
        # An archived team stays on the records that have it, but is never
        # newly given to one.
        current = getattr(getattr(self.parent, "instance", None), self.source, None)
        if not team.is_active and team != current:
            self.fail("archived")
        return team

    default_error_messages = {
        **serializers.PrimaryKeyRelatedField.default_error_messages,
        "archived": "The team is archived.",
    }


class TeamSerializer(serializers.ModelSerializer):
    """``teams/``: ``key`` and ``company`` are set on create, immutable after."""

    class Meta:
        model = Team
        fields = ["id", "company", "key", "name", "is_active", "sort_order", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {"company": {"required": False}}
        # (company, key) uniqueness is checked in validate(), with a message.
        validators = []

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        if instance is not None:
            for name in ("key", "company"):
                if name in attrs and attrs[name] != getattr(instance, name):
                    raise serializers.ValidationError(
                        {name: f"A team's {name} cannot change (imports map by the key)."}
                    )
            return attrs
        from .views import request_company, sole_company

        company = attrs.get("company") or request_company(self.context.get("request")) or sole_company()
        key = attrs.get("key")
        if company is not None and key and Team.objects.filter(company=company, key=key).exists():
            raise serializers.ValidationError({"key": f"The company already has a team {key!r}."})
        return attrs


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = "__all__"
        read_only_fields = ["id", "created_at"]
