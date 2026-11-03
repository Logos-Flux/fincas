"""The sidecar on the staff REST API (phase4-fields.md 3.3, section 9).

Two serializer mixins:

- ``MetadataCompatMixin`` keeps ``metadata`` writable for one release, as a
  merge (never a replacement):

  - ``custom.*`` keys go through ``core.sidecar.set_fields``: validated and
    coerced against their definition; an undefined one is a 400
    ``metadata_key_undefined``.
  - setting, changing or removing a registered system key
    (``core.provenance.is_system_metadata_key``) is a 400
    ``metadata_key_protected`` (since #64);
  - any other key (a provider key, an unregistered un-dotted key, a key that
    breaks the key rule) is **ignored** and named in a ``Warning`` response
    header (``CompanyScopedViewSet.finalize_response``);
  - echoing a key back unchanged is always accepted, so a GET-edit-PATCH of
    the whole object works.

- ``CustomFieldsMixin`` adds ``custom_fields``: an object of the defined
  ``custom.*`` keys with their canonical JSON values, readable and writable.
  Only ``custom.*`` keys are accepted there; ``null`` removes one.

Both write after the row is saved, inside the same transaction as the
create or update, so a refused field rolls the whole write back.
"""

from __future__ import annotations

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail, PermissionDenied

from . import sidecar
from .provenance import is_system_metadata_key

API_SOURCE = "api"
# Columns a promoted key may route to that need a permission to name directly
# (phase4-fields.md 2.1: owner_team is stamped from the owner).
ROUTED_PERMISSIONS = {"owner_team": "crm.reassign_engagement"}
WARNING_ATTR = "_sidecar_ignored_keys"


def warning_header(keys) -> str:
    """RFC 7234 ``Warning`` (299: miscellaneous persistent warning)."""
    names = ", ".join(sorted(set(keys)))[:500]
    return (f'299 - "metadata keys ignored: {names}. Only custom.* keys are written through '
            'metadata; use custom_fields."')


def _note_ignored(serializer, keys) -> None:
    request = serializer.context.get("request")
    if request is not None and keys:
        setattr(request, WARNING_ATTR, sorted(set(getattr(request, WARNING_ATTR, ())) | set(keys)))


def _unchanged(current: dict, key, item) -> bool:
    return (key in current and current[key] == item) or (key not in current and item is None)


@extend_schema_field(OpenApiTypes.OBJECT)
class CustomFieldsField(serializers.Field):
    """``custom_fields``: the object's defined ``custom.*`` keys."""

    def __init__(self, **kwargs):
        kwargs.setdefault("source", "*")
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def to_representation(self, obj):
        return sidecar.custom_values(obj)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("custom_fields must be a JSON object.", code="invalid")
        wrong = sorted(str(k) for k in data if sidecar.namespace(k) != sidecar.CUSTOM)
        if wrong:
            raise serializers.ValidationError(
                f"custom_fields takes custom.* keys only: {', '.join(wrong)}.", code=sidecar.KEY_INVALID,
            )
        return {"custom_fields": dict(data)}


class MetadataCompatMixin:
    """``metadata`` writes merge through the sidecar contract (see module doc)."""

    PROTECTED_CODE = sidecar.KEY_PROTECTED
    sidecar_source = API_SOURCE

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("metadata must be a JSON object.", code="invalid")
        current = getattr(self.instance, "metadata", None)
        current = current if isinstance(current, dict) else {}
        definable = sidecar.object_name_for(self.Meta.model) in sidecar.DEFINABLE_OBJECTS
        patch, refused, undefined, ignored = {}, [], [], []
        for key, item in value.items():
            if _unchanged(current, key, item):
                continue
            ns = sidecar.namespace(key)
            if ns == sidecar.CUSTOM:
                if definable:
                    patch[key] = item  # validated by set_fields, against the row's company
                else:
                    undefined.append(key)
            elif is_system_metadata_key(key):
                refused.append(str(key))
            else:
                ignored.append(str(key))
        if refused:
            raise serializers.ValidationError(
                f"System keys are written by golden only: {', '.join(sorted(refused))}.",
                code=self.PROTECTED_CODE,
            )
        if undefined:
            raise serializers.ValidationError(
                f"{', '.join(sorted(undefined))}: field definitions apply to "
                f"{', '.join(sidecar.DEFINABLE_OBJECTS)} only.",
                code=sidecar.KEY_UNDEFINED,
            )
        _note_ignored(self, ignored)
        return patch

    def _sidecar_profile(self):
        profile = self.context.get("profile")
        if profile is not None:
            return profile
        request = self.context.get("request")
        return getattr(getattr(request, "user", None), "userprofile", None) if request else None

    def _check_routed_permissions(self, instance, patch) -> None:
        """A promoted key routed to a guarded column needs that column's
        permission, exactly as writing the column directly does."""
        object_name = sidecar.object_name_for(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        for key, value in patch.items():
            column = sidecar.PROMOTED_KEYS.get(object_name, {}).get(key)
            perm = ROUTED_PERMISSIONS.get(column)
            if perm is None:
                continue
            current = getattr(instance, f"{column}_id", getattr(instance, column, None))
            if str(value) != str(current) and not (value is None and current is None) \
                    and (user is None or not user.has_perm(perm)):
                raise PermissionDenied(f"Setting {column} (via {key}) needs {perm}.")

    def save(self, **kwargs):
        data = self._validated_data
        origin = {}
        patch = {}
        for name in ("metadata", "custom_fields"):
            part = data.pop(name, None) or {}
            for key, value in part.items():
                patch[key] = value
                origin[key] = name
        with transaction.atomic():
            if self.instance is not None and self.instance.pk is not None:
                # The model save below writes the whole row, metadata included:
                # take the stored sidecar under a lock first, so a sync's write
                # since this request loaded the row is not overwritten.
                model = type(self.instance)
                self.instance.metadata = model._base_manager.select_for_update().only(
                    "pk", "metadata").get(pk=self.instance.pk).metadata
            instance = super().save(**kwargs)
            self._check_routed_permissions(instance, patch)
            if patch:
                try:
                    sidecar.set_fields(instance, patch, source=self.sidecar_source,
                                       by=self._sidecar_profile())
                except sidecar.SidecarError as exc:
                    raise serializers.ValidationError(_errors(exc, origin)) from exc
        return instance


def _errors(exc: sidecar.SidecarError, origin: dict) -> dict:
    out: dict[str, list] = {}
    for key, (code, message) in exc.errors.items():
        name = origin.get(key) or ("custom_fields" if "custom_fields" in origin.values() else "metadata")
        text = message if key == "__all__" or key in message else f"{key}: {message}"
        out.setdefault(name, []).append(ErrorDetail(text, code=code))
    return out


class CustomFieldsMixin(MetadataCompatMixin, serializers.Serializer):
    """``MetadataCompatMixin`` plus the ``custom_fields`` read/write surface.

    For Party, Engagement and Task (the objects FieldDefinitions apply to).
    List ``"custom_fields"`` in the serializer's ``Meta.fields``.
    """

    custom_fields = CustomFieldsField()
