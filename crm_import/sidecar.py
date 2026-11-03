"""The import sidecar (docs/crm/phase5-importer.md 2.12; phase4-fields.md 3).

Phase 4's sidecar contract (``core/sidecar.py``) stores a provider's values
FLAT in ``metadata``, one key per source property, ``"<provider>.<name>"``
(for example ``"hubspot.lead_type"``), written only by imports and plugin
ingest through ``core.sidecar.set_fields``. Spec 2.12's nested "import" /
provider / "properties" path is superseded by it. ``<name>`` is the
property's original name lower-cased with every character outside
``[a-z0-9_]`` mapped to ``_`` (phase4-fields.md 3.1; the contract's key rule
refuses anything else): HubSpot's internal names (``hs_lead_status``,
``lead_type``) are already in that form.

``put`` is the only writer (the appliers, step 4); ``properties`` is the
re-import compare's reader. Values the importer derives itself rather than
reads from a source property (extra domains, unparseable addresses, the job
title) use the same namespace with an ``import_`` prefix, and ``properties``
leaves them out.
"""

from __future__ import annotations

import re
from decimal import Decimal

#: Keys the importer derives itself (not source properties).
DERIVED_PREFIX = "import_"
_KEY_CHARS = re.compile(r"[^a-z0-9_]")


class SidecarRefused(ValueError):
    """A value the sidecar contract cannot store (phase4-fields.md 3.2)."""


def key_name(name) -> str:
    """The ``<name>`` part of a provider key (may be empty or too long: see ``key_for``)."""
    return _KEY_CHARS.sub("_", str(name).lower()).lstrip("_")


def key_for(provider: str, name) -> str:
    """``<provider>.<name>`` for a source property. Raises ``SidecarRefused``
    for a name that maps to nothing usable (empty, or longer than 64)."""
    from core.sidecar import DOTTED_KEY_RE

    key = f"{provider}.{key_name(name)}"
    if not DOTTED_KEY_RE.fullmatch(key):
        raise SidecarRefused(f"Property {name!r} has no usable sidecar key ({key!r}).")
    return key


def properties(obj, provider: str) -> dict | None:
    """The provider's sidecar values on ``obj`` (``{<name>: value}``), or
    None when ``obj`` holds no key of ``provider`` (no import wrote it:
    nothing to compare)."""
    metadata = getattr(obj, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    prefix = f"{provider}."
    mine = {k[len(prefix):]: v for k, v in metadata.items() if isinstance(k, str) and k.startswith(prefix)}
    if not mine:
        return None
    return {k: v for k, v in mine.items() if not k.startswith(DERIVED_PREFIX)}


def _storable(value):
    """A source value as a sidecar value: floats as decimal strings; nested
    objects are left for the contract to refuse (3.2)."""
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, (list, tuple)):
        return [_storable(v) for v in value]
    return value


def put(obj, provider: str, values: dict, *, derived: dict | None = None, session=None) -> dict:
    """Write source properties into ``obj``'s sidecar (spec 2.12).

    ``values`` maps original property names to values; ``None`` (absent) is
    skipped, never stored. ``derived`` holds the importer's own values
    (names already ``import_*``); a source name in that namespace is refused. Two names that map to one key, or a value the
    contract refuses (a nested object, over a limit), raise
    ``SidecarRefused``: the applier fails the row ``invalid_value``, never
    truncates. With ``session`` (a ``writelog.WriteSession``) the row is
    snapshotted first, so undo restores the sidecar too. Returns the keys set.
    """
    from core.sidecar import SidecarError, set_fields

    out: dict = {}
    names: dict[str, str] = {}
    for name in values:
        if key_name(name).startswith(DERIVED_PREFIX):
            raise SidecarRefused(f"Property {name!r} falls in the importer's own {DERIVED_PREFIX}* keys; "
                                 "map it to another name or drop it.")
    for name, value in [*values.items(), *((derived or {}).items())]:
        if value is None:
            continue
        key = key_for(provider, name)
        if key in names and names[key] != name:
            raise SidecarRefused(f"Properties {names[key]!r} and {name!r} both map to {key!r}.")
        names[key] = name
        out[key] = _storable(value)
    if not out:
        return {}
    if session is not None:
        session.touch(type(obj), obj.pk)
    try:
        plan = set_fields(obj, out, source=f"import:{provider}")
    except SidecarError as exc:
        details = "; ".join(f"{k}: {msg}" for k, (_code, msg) in sorted(exc.errors.items()))
        raise SidecarRefused(f"The sidecar refused {details}") from exc
    return dict(plan.set)
