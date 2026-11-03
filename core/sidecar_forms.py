"""Field definitions on back-office screens (docs/crm/phase4-fields.md 3.3, 5.2).

Forms render ``core.sidecar.fields_for`` as one fieldset per definition
``group`` with a widget per type, and validate through the same
``core.sidecar`` validator as the API; detail pages show the same groups as
definition lists, plus an "Other data" panel for provider and undefined keys.

**Personal data.** A ``pii`` definition's value is shown, and its field is
offered in a form, only to a user with ``parties.export_pii`` (granted to the
``staff`` role by default; a deployment can revoke it). Everyone else sees
"Hidden: personal data" and the form leaves the stored value alone. "Other
data" holds provider and undefined keys, whose content no definition
describes, so it is behind the same permission.
"""

from __future__ import annotations

import datetime
from collections import OrderedDict

from django import forms
from django.utils import dateformat, timezone

from . import sidecar

#: The permission that reveals personal-data fields on screen (5.5's export gate).
PII_PERMISSION = "parties.export_pii"
#: ``set_fields`` source for back-office writes.
ERP_SOURCE = "erp"
#: "Other data" truncates each value at this many characters (5.2).
OTHER_DATA_LIMIT = 200
FIELD_PREFIX = "sc__"

DATE_WIDGET = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
DATETIME_WIDGET = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
BOOLEAN_CHOICES = (("", "Unknown"), ("true", "Yes"), ("false", "No"))


def can_view_pii(user) -> bool:
    return bool(user is not None and user.is_authenticated and user.has_perm(PII_PERMISSION))


def field_name(key: str) -> str:
    """The form field name for a sidecar key (``custom.package`` -> ``sc__custom__package``)."""
    return FIELD_PREFIX + key.replace(".", "__")


def _form_field(definition, *, required: bool):
    common = {"label": definition.label, "required": required, "help_text": definition.help}
    kind = definition.type
    if kind == "string":
        return forms.CharField(max_length=sidecar.MAX_STRING, **common)
    if kind == "text":
        return forms.CharField(max_length=sidecar.MAX_STRING, widget=forms.Textarea(attrs={"rows": 3}),
                               **common)
    if kind == "url":
        return forms.URLField(max_length=sidecar.MAX_STRING, assume_scheme="https", **common)
    if kind == "integer":
        return forms.IntegerField(min_value=-sidecar.MAX_INT, max_value=sidecar.MAX_INT, **common)
    if kind == "decimal":
        return forms.DecimalField(**common)
    if kind == "date":
        return forms.DateField(widget=DATE_WIDGET, **common)
    if kind == "datetime":
        return forms.DateTimeField(widget=DATETIME_WIDGET, **common)
    if kind == "boolean":
        return forms.TypedChoiceField(choices=BOOLEAN_CHOICES, coerce=lambda v: v == "true",
                                      empty_value=None, **common)
    if kind == "choice":
        return forms.ChoiceField(choices=[("", "—"), *definition.choices], **common)
    if kind == "multi_choice":
        return forms.MultipleChoiceField(choices=list(definition.choices),
                                         widget=forms.CheckboxSelectMultiple, **common)
    raise ValueError(f"unknown definition type {kind!r}")  # pragma: no cover - registry refuses it


def _initial(definition, value):
    if value is None:
        return None
    if definition.type == "boolean" and isinstance(value, bool):
        return "true" if value else "false"
    if definition.type == "datetime" and isinstance(value, datetime.datetime):
        return timezone.localtime(value).replace(tzinfo=None)
    return value


def _to_sidecar(definition, value):
    """A cleaned form value as ``set_fields`` input (None: remove the key)."""
    if value in (None, "", []):
        return None
    if definition.type == "datetime" and isinstance(value, datetime.datetime) and timezone.is_naive(value):
        return timezone.make_aware(value)
    return value


class DefinitionFieldsMixin:
    """Adds the definition fields of ``definition_object`` to a Django form.

    The form's ``__init__`` calls :meth:`add_definition_fields` with the
    instance being edited (or a new unsaved one) and the requesting user.
    ``clean()`` validates through ``core.sidecar.validate_fields``; after the
    row is saved the view calls :meth:`save_definition_fields`.
    """

    definition_object = ""

    def add_definition_fields(self, instance, *, user, company_id=None):
        self.sidecar_instance = instance
        self.sidecar_user = user
        if company_id is not None and getattr(instance, "company_id", None) is None:
            instance.company_id = company_id
        self.definition_fields = OrderedDict()  # field name -> definition
        self.definition_groups = OrderedDict()  # group -> [field names]
        self.hidden_definitions = []
        if instance is None:
            return
        show_pii = can_view_pii(user)
        for definition, value in sidecar.fields_for(instance):
            if definition.pii and not show_pii:
                self.hidden_definitions.append(definition)
                continue
            name = field_name(definition.key)
            field = _form_field(definition, required=definition.required and not definition.read_only)
            if definition.read_only:
                field.disabled = True
                field.help_text = (field.help_text + " " if field.help_text else "") + \
                    "Imported; read-only here."
            field.initial = _initial(definition, value)
            if field.help_text:
                # 5.7: help is announced with the control (the partial renders it
                # under this id; auto_id is Django's default "id_%s").
                field.widget.attrs["aria-describedby"] = f"id_{name}_help"
            self.fields[name] = field
            self.definition_fields[name] = definition
            self.definition_groups.setdefault(definition.group, []).append(name)

    @property
    def definition_sections(self):
        """``[{"title": group, "fields": [names]}]`` for ``erp/form.html``."""
        return [{"title": group, "fields": names} for group, names in self.definition_groups.items()]

    def definition_values(self) -> dict:
        """``{key: value}`` of the editable definition fields (None: remove)."""
        values = {}
        for name, definition in self.definition_fields.items():
            if definition.read_only or name not in self.cleaned_data:
                continue
            # An absent key written as None is a no-op in set_fields (no audit row).
            values[definition.key] = _to_sidecar(definition, self.cleaned_data[name])
        return values

    def clean_definition_fields(self):
        instance = getattr(self, "sidecar_instance", None)
        if instance is None or not self.definition_fields:
            return
        values = self.definition_values()
        if not values:
            return
        try:
            sidecar.validate_fields(instance, values, source=ERP_SOURCE)
        except sidecar.SidecarError as exc:
            by_key = {d.key: name for name, d in self.definition_fields.items()}
            for key, (_code, message) in exc.errors.items():
                target = by_key.get(key)
                if target is not None and target in self.fields:
                    self.add_error(target, message)
                else:
                    self.add_error(None, message)

    def save_definition_fields(self, obj, *, by=None):
        """Write the definition values onto the saved ``obj`` (one audit row)."""
        if not getattr(self, "definition_fields", None):
            return None
        values = self.definition_values()
        if not values:
            return None
        return sidecar.set_fields(obj, values, source=ERP_SOURCE, by=by)


# --- detail pages ------------------------------------------------------------------


def display_value(definition, value) -> str:
    """A typed sidecar value as staff read it ("" when absent)."""
    if value is None or value == "" or value == []:
        return ""
    kind = definition.type
    if kind == "boolean":
        return "Yes" if value is True else "No" if value is False else str(value)
    if kind == "choice":
        return definition.choice_label(value)
    if kind == "multi_choice" and isinstance(value, list):
        return ", ".join(definition.choice_label(v) for v in value)
    if kind == "date" and isinstance(value, datetime.date):
        return dateformat.format(value, "M j, Y")
    if kind == "datetime" and isinstance(value, datetime.datetime):
        return dateformat.format(timezone.localtime(value), "M j, Y H:i")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def definition_groups(obj, user) -> list[dict]:
    """``[{"title", "rows": [{"label", "value", "hidden", "url", "multiline"}]}]``
    in definition order. Empty values are listed (``value`` ""), so staff see
    what is missing (5.2)."""
    show_pii = can_view_pii(user)
    groups: OrderedDict[str, list] = OrderedDict()
    for definition, value in sidecar.fields_for(obj):
        hidden = definition.pii and not show_pii
        text = "" if hidden else display_value(definition, value)
        groups.setdefault(definition.group, []).append({
            "key": definition.key, "label": definition.label, "value": text, "hidden": hidden,
            "url": text if definition.type == "url" and text else "",
            "multiline": definition.type == "text",
        })
    return [{"title": title, "rows": rows} for title, rows in groups.items()]


def other_data(obj, user) -> dict:
    """Provider and undefined keys for the collapsed "Other data" panel:
    ``{"rows": [(key, text)], "count": n, "hidden": bool}``. Values are
    truncated at 200 characters; templates escape them (5.2)."""
    metadata = obj.metadata if isinstance(getattr(obj, "metadata", None), dict) else {}
    defined = {d.key for d, _v in sidecar.fields_for(obj)}
    keys = sorted(
        k for k in metadata
        if k not in defined and sidecar.namespace(k) in (sidecar.CUSTOM, sidecar.PROVIDER)
    )
    if not keys:
        return {"rows": [], "count": 0, "hidden": False}
    if not can_view_pii(user):
        return {"rows": [], "count": len(keys), "hidden": True}
    rows = []
    for key in keys:
        value = metadata[key]
        text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        if isinstance(value, bool):
            text = "true" if value else "false"
        if len(text) > OTHER_DATA_LIMIT:
            text = text[:OTHER_DATA_LIMIT] + "…"
        rows.append((key, text))
    return {"rows": rows, "count": len(rows), "hidden": False}
