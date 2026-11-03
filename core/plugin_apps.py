"""Settings-time composition of ``GOLDEN_PLUGIN_APPS`` into ``INSTALLED_APPS``.

Registration boundary (enforced, never silent):

- ``GOLDEN_PLUGINS``      — ``BasePlugin`` glue classes (nav, widgets,
  gateways, signals, URLs). Loaded by ``core.plugins.registry``.
- ``GOLDEN_PLUGIN_APPS``  — real Django apps (models, migrations, admin,
  templates). Appended to ``INSTALLED_APPS`` by :func:`compose_installed_apps`.

A deployment's overlay settings module ends with::

    from golden_erp.settings.prod import *  # noqa: F401,F403
    from core.plugin_apps import compose_installed_apps

    GOLDEN_PLUGIN_APPS = ["acme_ebay"]
    INSTALLED_APPS = compose_installed_apps(INSTALLED_APPS, GOLDEN_PLUGIN_APPS)

This module lives outside ``core.plugins`` on purpose: importing that package
builds the plugin registry, which reads ``django.conf.settings`` — and doing
that while the settings module is still executing would freeze a half-built
settings object. Nothing here imports project code; a ``BasePlugin`` class is
recognised by reading its module's source, not by executing it.

``CoreConfig.ready()`` re-checks both lists once apps are loaded
(``core.plugins.registry.check_plugin_classification``), so a misplaced entry
or a forgotten ``compose_installed_apps`` call fails at startup either way.
"""

from __future__ import annotations

import ast
import sys
from importlib.machinery import PathFinder
from typing import Iterable, List, Optional

from django.core.exceptions import ImproperlyConfigured

_BASE_PLUGIN = "core.plugins.base.BasePlugin"

#: The addons moved from top-level ``addons`` to ``golden_erp.addons`` (#120).
#: REMOVE IN v0.6.0, with the ``addons`` shim package.
LEGACY_ADDONS_PREFIX = "addons"
ADDONS_PREFIX = "golden_erp.addons"


def canonical_plugin_path(entry: str) -> str:
    """The current spelling of a dotted path: ``addons.x`` -> ``golden_erp.addons.x``.

    Paths outside the old ``addons`` package come back unchanged. Plugin and
    app checks compare and look up canonical paths, so the deprecated and the
    current spelling of one plugin are recognised as the same entry.
    """
    if entry == LEGACY_ADDONS_PREFIX or entry.startswith(LEGACY_ADDONS_PREFIX + "."):
        return ADDONS_PREFIX + entry[len(LEGACY_ADDONS_PREFIX):]
    return entry


def is_legacy_addons_path(entry) -> bool:
    return isinstance(entry, str) and canonical_plugin_path(entry) != entry


def _static_spec(dotted: str):
    """Find a module spec without executing any module (parents included)."""
    path = None
    spec = None
    parts = dotted.split(".")
    for i in range(len(parts)):
        if i > 0 and path is None:
            return None  # parent is a plain module; it cannot hold submodules
        try:
            spec = PathFinder.find_spec(".".join(parts[: i + 1]), path)
        except (ImportError, ValueError):
            return None
        if spec is None:
            return None
        path = spec.submodule_search_locations
    return spec


def _class_is_plugin_in_source(source_file: str, class_name: str) -> bool:
    """True when ``class_name`` in that file derives from ``BasePlugin``
    (directly, or through another class defined in the same file)."""
    try:
        with open(source_file, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=source_file)
    except (OSError, SyntaxError, ValueError):
        return False
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    seen: set = set()

    def derives(name: str) -> bool:
        if name in seen or name not in classes:
            return False
        seen.add(name)
        for base in classes[name].bases:
            base_name = (
                base.id if isinstance(base, ast.Name)
                else base.attr if isinstance(base, ast.Attribute)
                else None
            )
            if base_name == "BasePlugin" or (base_name and derives(base_name)):
                return True
        return False

    return derives(class_name)


def _mro_names(cls: type) -> List[str]:
    return [f"{c.__module__}.{c.__qualname__}" for c in getattr(cls, "__mro__", ())]


def is_base_plugin_path(entry: str) -> bool:
    """Best-effort, execution-free: does this dotted path name a BasePlugin class?

    Returns False when it cannot tell (Django's own app loading then rejects a
    non-AppConfig class path with ``ImproperlyConfigured`` anyway).
    """
    entry = canonical_plugin_path(entry)
    module_path, _, attr = entry.rpartition(".")
    if not module_path:
        return False
    loaded = sys.modules.get(module_path)
    if loaded is not None:
        target = getattr(loaded, attr, None)
        return isinstance(target, type) and _BASE_PLUGIN in _mro_names(target)
    if _static_spec(entry) is not None:
        return False  # the full path is a module, i.e. an app package
    spec = _static_spec(module_path)
    origin: Optional[str] = getattr(spec, "origin", None) if spec else None
    if not origin or not origin.endswith(".py"):
        return False
    return _class_is_plugin_in_source(origin, attr)


def _misplaced_plugin_error(where: str, entry: str) -> ImproperlyConfigured:
    return ImproperlyConfigured(
        f"{where} = {entry!r} is a BasePlugin glue class, not a Django app. "
        "List it in GOLDEN_PLUGINS; GOLDEN_PLUGIN_APPS takes Django apps "
        "(models/migrations/admin/templates) only."
    )


def compose_installed_apps(
    installed_apps: Iterable[str], plugin_apps: Iterable[str]
) -> List[str]:
    """Return ``installed_apps`` with each ``plugin_apps`` entry appended.

    Idempotent (an entry already installed is not added twice), so stacked
    overlays may each call it. Raises ``ImproperlyConfigured`` for a
    non-string entry, a duplicate, or a ``BasePlugin`` class path.
    """
    if isinstance(plugin_apps, (str, bytes)) or not isinstance(plugin_apps, (list, tuple)):
        raise ImproperlyConfigured(
            f"GOLDEN_PLUGIN_APPS must be a list of Django app paths, got "
            f"{type(plugin_apps).__name__}."
        )
    composed = list(installed_apps)
    seen: set = set()
    for index, entry in enumerate(plugin_apps):
        where = f"GOLDEN_PLUGIN_APPS[{index}]"
        if isinstance(entry, type):
            label = f"{entry.__module__}.{entry.__qualname__}"
            if _BASE_PLUGIN in _mro_names(entry):
                raise _misplaced_plugin_error(where, label)
            raise ImproperlyConfigured(
                f"{where} = {label} is a class object; list Django apps by "
                "dotted path string."
            )
        if not isinstance(entry, str) or not entry:
            raise ImproperlyConfigured(
                f"{where} must be a non-empty dotted path string, got {entry!r}."
            )
        if canonical_plugin_path(entry) in seen:
            raise ImproperlyConfigured(f"{where} repeats {entry!r}.")
        seen.add(canonical_plugin_path(entry))
        if is_base_plugin_path(entry):
            raise _misplaced_plugin_error(where, entry)
        if entry not in composed:
            composed.append(entry)
    return composed
