"""Deprecated alias: the addons moved to ``golden_erp.addons`` (#120).

REMOVE IN v0.6.0. This package exists through the 0.5.x releases so that settings and
code written against v0.4 (``GOLDEN_PLUGINS = ["addons.gmail.plugin.GmailPlugin"]``,
``from addons.klaviyo import values``) keep working while they are updated.

``import addons`` emits one ``DeprecationWarning`` (the module is cached, so it
runs once per process) and installs :class:`_AliasFinder` at the front of
``sys.meta_path``. Every ``addons.<x>[.<y>…]`` import is then answered with the
module object already registered as ``golden_erp.addons.<x>[.<y>…]`` (imported
on demand), and both names point at that one object in ``sys.modules``.

It must alias, never re-import: extending ``__path__`` to the new directory
would load each file a second time under the old name, giving two plugin
classes, two adapter registrations and two outbox consumers for one addon.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import warnings

OLD = "addons"
NEW = "golden_erp.addons"

warnings.warn(
    "The top-level 'addons' package is deprecated and will be removed in "
    "v0.6.0; import 'golden_erp.addons' instead (and spell GOLDEN_PLUGINS "
    "entries 'golden_erp.addons.<x>.plugin.<Class>').",
    DeprecationWarning,
    stacklevel=2,
)


def canonical(dotted: str) -> str:
    """``addons.x.y`` -> ``golden_erp.addons.x.y``; anything else unchanged."""
    if dotted == OLD or dotted.startswith(OLD + "."):
        return NEW + dotted[len(OLD):]
    return dotted


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self.target = target

    def create_module(self, spec):
        # The real module, imported (or reused) under its canonical name.
        # importlib then stores it in sys.modules under the alias too; it only
        # fills module attributes that are missing, so the real __name__,
        # __spec__ and __path__ stay as they are.
        return importlib.import_module(self.target)

    def exec_module(self, module) -> None:
        pass  # already executed under its canonical name


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(OLD + "."):
            return None
        new_name = canonical(fullname)
        try:
            real = importlib.util.find_spec(new_name)
        except (ImportError, ValueError):
            return None
        if real is None:
            return None  # ModuleNotFoundError names the old spelling
        return importlib.machinery.ModuleSpec(
            fullname,
            _AliasLoader(new_name),
            is_package=real.submodule_search_locations is not None,
        )


def _install() -> None:
    if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
        # First: PathFinder would otherwise find addons.x.y through the real
        # package's __path__ and execute the file again under the old name.
        sys.meta_path.insert(0, _AliasFinder())


_install()
