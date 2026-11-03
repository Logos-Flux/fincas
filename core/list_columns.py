"""The list column registry (docs/crm/phase4-fields.md 5.1, 5.3, 5.6).

Every ``ERPListView`` describes its columns as :class:`Column` entries. A
column has a canonical, stable ``id`` (the CSV header and the preference
key), a ``source`` (``field``, ``definition`` or ``plugin``), ``sortable``,
``priority`` (1-3, the responsive rule) and ``default`` (shown when the user
has no preference).

Three kinds of column:

- **field**: a model field path (``owner__display_name``) or a value the view
  computes (``roles``). Declared by the view, in code.
- **definition**: one per ``FieldDefinition`` with ``list=True`` for the
  list's object (``core.field_registry``), restricted to the viewer's tenant. The
  value is read in SQL with ``KeyTextTransform`` while the list ``defer``\\ s
  ``metadata`` (3.4), so a 32 KB sidecar costs nothing on a 50-row page.
  Never sortable (F8).
- **plugin**: from ``BasePlugin.get_list_columns(list_id)``. A plugin column
  names a model path (typically a one-to-one extension model, 2.4) and a
  ``format``; core annotates the path. A plugin never gets the queryset or a
  row, so it cannot add queries or run ORM of its own.

The view's ``list_id`` (``parties.party``, ``crm.engagement``) keys the
user's preferences (``ERPUserSettings.list_preferences``), saved filters and
the plugin hook.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured

SOURCES = ("field", "definition", "plugin")
PRIORITIES = (1, 2, 3)
#: At most this many columns are shown (5.3).
MAX_COLUMNS = 12
#: Column ids: registry keys (``display_name``), sidecar keys
#: (``custom.package``) or plugin-namespaced ids (``myplugin.score``).
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(\.[a-z0-9][a-z0-9_]{0,63})?$")
#: Value formats a definition or plugin column may render with.
FORMATS = ("text", "integer", "decimal", "money", "date", "datetime", "boolean", "url")

# Tabler (Bootstrap) breakpoints: priority 1 always, 2 from md, 3 from lg (5.6).
PRIORITY_CLASSES = {1: "", 2: "d-none d-md-table-cell", 3: "d-none d-lg-table-cell"}


@dataclass(frozen=True)
class Column:
    """One list column. Rendering hints (``badge``, ``money`` …) are the
    legacy ``list_columns`` keys, kept so ``erp/list.html`` renders every
    commerce list unchanged."""

    id: str
    label: str
    source: str = "field"
    #: ORM path (field, plugin) or sidecar key (definition).
    path: str = ""
    sortable: bool = False
    sort_paths: tuple = ()
    priority: int = 1
    default: bool = True
    #: Chooser group: "Default", "Fields" (definitions) or "Other".
    group: str = ""
    #: Personal data: exported only with ``parties.export_pii`` (5.5).
    pii: bool = False
    #: Included in "All fields" exports.
    export: bool = True
    #: Exported only, never offered on screen (an id column, a raw key).
    export_only: bool = False
    format: str = "text"
    #: ``(value, label)`` pairs for a choice definition.
    choices: tuple = ()
    owner: str = ""  # the plugin that contributed it
    # Legacy rendering hints for erp/list.html.
    link: bool = False
    link_url_name: str = ""
    badge: bool = False
    bool_badge: bool = False
    date_only: bool = False
    money: bool = False
    align: str = ""
    #: The link (and the row click) opens a new tab: documents such as an
    #: invoice open beside the list, never in place of it.
    new_tab: bool = False
    #: Path to the pk the link opens (``engagement_id``) when it is not the
    #: row's own. Such a link never becomes the row click.
    link_pk: str = ""

    @property
    def field(self) -> str:
        """``erp/list.html`` and older templates read ``col.field``."""
        return self.path or self.id

    @property
    def priority_class(self) -> str:
        return PRIORITY_CLASSES.get(self.priority, "")

    @property
    def annotation(self) -> str:
        """The queryset alias a definition or plugin column is read from."""
        return "_col_" + re.sub(r"[^a-z0-9_]", "_", self.id)


_LEGACY_KEYS = ("link", "link_url_name", "badge", "bool_badge", "date_only", "money", "align",
                "new_tab", "link_pk")


def column(id: str, label: str, **kwargs: Any) -> Column:
    """A validated field column (``ImproperlyConfigured`` on a bad entry)."""
    col = Column(id=id, label=label, **kwargs)
    validate(col)
    return col


def validate(col: Column, where: str = "") -> None:
    where = where or f"column {col.id!r}"
    if not isinstance(col.id, str) or not ID_RE.match(col.id):
        raise ImproperlyConfigured(f"{where}: id must match {ID_RE.pattern}.")
    if col.source not in SOURCES:
        raise ImproperlyConfigured(f"{where}: source must be one of {', '.join(SOURCES)}.")
    if col.priority not in PRIORITIES:
        raise ImproperlyConfigured(f"{where}: priority must be 1, 2 or 3.")
    if col.format not in FORMATS:
        raise ImproperlyConfigured(f"{where}: format must be one of {', '.join(FORMATS)}.")
    if col.source == "definition" and col.sortable:
        raise ImproperlyConfigured(f"{where}: sidecar columns are never sortable (F8).")


def from_legacy(entries: Iterable[dict], sortable: dict) -> list[Column]:
    """Columns from a pre-registry ``list_columns`` list of dicts.

    ``id`` defaults to the field path, so a commerce list keeps its
    preference keys and sort ids. Commerce lists keep every column as a
    default (5.1: "no new defaults")."""
    columns = []
    for entry in entries:
        if isinstance(entry, Column):
            columns.append(entry)
            continue
        path = entry["field"]
        col_id = entry.get("id") or path
        kwargs = {k: entry[k] for k in _LEGACY_KEYS if k in entry}
        sort_paths = tuple(sortable.get(col_id, ()))
        col = Column(
            id=col_id if ID_RE.match(col_id) else col_id.lower(),
            label=entry.get("label", path),
            source="field",
            path=path,
            sortable=bool(sort_paths),
            sort_paths=sort_paths,
            priority=entry.get("priority", 1),
            default=entry.get("default", True),
            pii=entry.get("pii", False),
            export=entry.get("export", True),
            export_only=entry.get("export_only", False),
            **kwargs,
        )
        columns.append(col)
    return columns


# --- definition columns ---------------------------------------------------------


def definition_columns(object_name: str, company_id) -> list[Column]:
    """One column per ``FieldDefinition`` of ``object_name`` the tenant has.

    ``list=True`` definitions are offered in the chooser (``default`` =
    ``list_default``); the rest with ``export=True`` are export-only, so
    "All fields" carries every exportable definition (5.5)."""
    from core.field_registry import definitions_for

    columns = []
    for definition in definitions_for(object_name, company_id):
        if not (definition.list or definition.export):
            continue
        fmt = _DEFINITION_FORMATS.get(definition.type, "text")
        columns.append(Column(
            id=definition.key,
            label=definition.label,
            source="definition",
            path=definition.key,
            sortable=False,
            priority=3,
            default=bool(definition.list and definition.list_default),
            group="Fields",
            pii=bool(definition.pii),
            export=bool(definition.export),
            export_only=not definition.list,
            format=fmt,
            choices=tuple((str(v), str(lbl)) for v, lbl in (definition.choices or ())),
        ))
    return columns


_DEFINITION_FORMATS = {
    "string": "text", "text": "text", "choice": "text", "multi_choice": "text",
    "integer": "integer", "decimal": "decimal", "date": "date", "datetime": "datetime",
    "boolean": "boolean", "url": "url",
}


def raw_value(col: Column, obj) -> Any:
    """The stored value of a definition or plugin column on a list row."""
    return getattr(obj, col.annotation, None)


def sidecar_value(col: Column, raw) -> Any:
    """A ``KeyTextTransform`` result back to its JSON value.

    ``->>`` yields text: strings as-is, numbers and booleans as their JSON
    text (``1250``, ``true``), lists as a JSON array. ``None`` = absent."""
    if raw is None:
        return None
    if col.format == "boolean":
        return raw in (True, 1, "1", "true", "True")
    if isinstance(raw, str) and raw[:1] == "[":
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(parsed, list):
            return parsed
    return raw


def display_value(col: Column, raw) -> str:
    """Screen text for a definition column: choice labels, "Yes"/"No"; "" = unknown."""
    value = sidecar_value(col, raw)
    if value is None or value == "":
        return ""
    labels = dict(col.choices)
    if isinstance(value, list):
        return ", ".join(labels.get(str(v), str(v)) for v in value)
    if col.format == "boolean":
        return "Yes" if value else "No"
    return labels.get(str(value), str(value))


def export_value(col: Column, raw) -> str:
    """Canonical CSV text for a definition column (5.5): choice values, not
    labels; ``true``/``false``; list items joined with ``;``; empty for unknown."""
    value = sidecar_value(col, raw)
    if value is None:
        return ""
    if isinstance(value, list):
        return ";".join(_scalar(v) for v in value)
    return _scalar(value)


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --- plugin columns -------------------------------------------------------------


def plugin_columns(list_id: str, model) -> list[Column]:
    """Columns from ``BasePlugin.get_list_columns(list_id)``, validated.

    A plugin returns ``core.plugins.base.ListColumn`` entries. Each id must
    be namespaced with the plugin's name (``<plugin>.<name>``) so two
    plugins, or a plugin and core, cannot collide; ``path`` must walk forward
    (or reverse one-to-one) relations from ``model`` to a concrete field, so
    it never multiplies rows, and may not cross into credential models
    (``auth``, sessions, admin, content types, API keys, login identities). Anything else
    is ``ImproperlyConfigured`` naming the plugin."""
    from core.plugins.registry import plugin_registry

    columns = []
    seen = set()
    for plugin_name, entry in plugin_registry.get_list_columns(list_id):
        where = f"plugin {plugin_name!r} list column {getattr(entry, 'id', entry)!r} on {list_id!r}"
        col_id = getattr(entry, "id", None)
        if not isinstance(col_id, str) or not col_id.startswith(f"{plugin_name}."):
            raise ImproperlyConfigured(f"{where}: id must start with '{plugin_name}.'.")
        if col_id in seen:
            raise ImproperlyConfigured(f"{where}: the id is used twice.")
        seen.add(col_id)
        path = getattr(entry, "path", "")
        if not isinstance(path, str) or not _plugin_path_ok(model, path):
            raise ImproperlyConfigured(
                f"{where}: path {path!r} must walk forward or one-to-one relations from "
                f"{model._meta.label} to a concrete field, outside credential models "
                "(auth, sessions, admin, contenttypes, core.ApiKey, core.UserIdentity, "
                "core.IntegrationAccount, core.IntegrationCredential).",
            )
        sortable = bool(getattr(entry, "sortable", False))
        col = Column(
            id=col_id,
            label=str(getattr(entry, "label", col_id)),
            source="plugin",
            path=path,
            sortable=sortable,
            sort_paths=(path,) if sortable else (),
            priority=getattr(entry, "priority", 3),
            default=bool(getattr(entry, "default", False)),
            group="Other",
            pii=bool(getattr(entry, "pii", False)),
            export=bool(getattr(entry, "export", True)),
            format=getattr(entry, "format", "text"),
            owner=plugin_name,
        )
        validate(col, where)
        columns.append(col)
    return columns


#: Models a plugin column may never read through: credentials, sessions and
#: login identities.
_DENIED_APPS = frozenset({"auth", "sessions", "admin", "contenttypes"})
_DENIED_MODELS = frozenset({"core.apikey", "core.useridentity", "core.integrationcredential",
                            "core.integrationaccount"})


def _denied(model) -> bool:
    return model._meta.app_label in _DENIED_APPS or model._meta.label_lower in _DENIED_MODELS


def _plugin_path_ok(model, path: str) -> bool:
    if not path or len(path) > 200:
        return False
    parts = path.split("__")
    for i, part in enumerate(parts):
        if _denied(model):
            return False
        try:
            fld = model._meta.get_field(part)
        except FieldDoesNotExist:
            return False
        if fld.many_to_many or fld.one_to_many:
            return False
        last = i == len(parts) - 1
        if fld.is_relation:
            if fld.related_model is None:
                return False
            if not fld.concrete and not fld.one_to_one:
                return False
            if last:
                return False  # a relation, not a value
            model = fld.related_model
        elif not fld.concrete or not last:
            return False
    return not _denied(model)


# --- preferences ----------------------------------------------------------------


def visible_ids(columns: list[Column], stored: Optional[list], *, cap: bool = True) -> list[str]:
    """The ids to show: the user's stored choice (ids that no longer exist
    dropped), else the defaults; registry order, at most ``MAX_COLUMNS``
    unless ``cap=False`` (the export's "visible columns")."""
    offered = [c for c in columns if not c.export_only]
    known = {c.id for c in offered}
    limit = MAX_COLUMNS if cap else None
    if isinstance(stored, list):
        chosen = {i for i in stored if isinstance(i, str) and i in known}
        if chosen:
            return [c.id for c in offered if c.id in chosen][:limit]
    return [c.id for c in offered if c.default][:limit]

