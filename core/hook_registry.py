"""Extension-point registry: how shared code calls into add-on apps.

Shared apps (``core``, ``products``, ``pricing``, ...) must import cleanly when
an add-on such as ``forge`` is not installed: golden-erp ships these apps
without any deployment's add-ons. Where shared code used to import an add-on
directly, it now asks this registry for a named hook and degrades when nothing
provides it. See docs/plugins.md, "Hook points".

Two ways a hook gets an implementation, in this order of precedence:

1. ``settings.ERP_HOOKS`` binds a hook name to a dotted path, or to ``None``
   to switch it off. Instance config wins, so a deployment can swap or disable
   an implementation without touching the add-on.
2. The providing app registers it from its ``AppConfig.ready()`` with
   :func:`register` (a downstream ``forge`` app does this for both
   ``products.*`` hooks).

Same config-loader discipline as ``nav_registry.py`` / ``theme_registry.py``:
``ERP_HOOKS`` is validated at import, and bad config raises
``ImproperlyConfigured`` instead of being ignored. A hook that silently does
nothing is indistinguishable from one nobody wired up. The hook names are fixed
here, in core, so a typo in settings or in a ``register()`` call fails at boot.
"""

from __future__ import annotations

from typing import Any, Callable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string

#: Every hook shared code may call, with the contract its implementation meets.
HOOKS: dict[str, str] = {
    "products.validate_structured_specs": (
        "(category_slug: str, specs: dict) -> list[str]: warnings for specs "
        "that don't fit the category's schema. Used by `set_structured_specs`."
    ),
    "products.staging_mpns_by_variant": (
        "() -> dict[variant_id, str]: candidate MPNs for published variants "
        "from a staging source, latest wins. Used by `audit_mpn_coverage`."
    ),
}

_registered: dict[str, Callable[..., Any]] = {}


def _check_name(name: Any, where: str) -> None:
    if name not in HOOKS:
        raise ImproperlyConfigured(
            f"{where}: unknown hook {name!r}; known hooks: {', '.join(sorted(HOOKS))}."
        )


def build_bindings() -> dict[str, str | None]:
    """settings.ERP_HOOKS, validated: {hook name: dotted path or None}."""
    configured = getattr(settings, "ERP_HOOKS", None) or {}
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(
            f"ERP_HOOKS must be a dict of hook name -> dotted path or None, "
            f"got {type(configured).__name__}."
        )
    for name, target in configured.items():
        _check_name(name, "ERP_HOOKS")
        if target is not None and (not isinstance(target, str) or "." not in target):
            raise ImproperlyConfigured(
                f"ERP_HOOKS[{name!r}] must be a dotted path to a callable or None, "
                f"got {target!r}."
            )
    return dict(configured)


# Mutated in place on reload (below), same discipline as theme_registry.THEMES.
BINDINGS: dict[str, str | None] = build_bindings()


def register(name: str, func: Callable[..., Any]) -> None:
    """Provide the implementation of hook ``name``. Call from ``AppConfig.ready()``.

    Registering the same callable twice is a no-op (``ready()`` can run more
    than once in tests); a second, different implementation is a conflict.
    """
    _check_name(name, "register()")
    if not callable(func):
        raise ImproperlyConfigured(f"register({name!r}): {func!r} is not callable.")
    existing = _registered.get(name)
    if existing is not None and existing is not func:
        raise ImproperlyConfigured(
            f"register({name!r}): already provided by "
            f"{existing.__module__}.{existing.__qualname__}."
        )
    _registered[name] = func


def get(name: str) -> Callable[..., Any] | None:
    """The implementation of hook ``name``, or None when nothing provides it."""
    _check_name(name, "get()")
    if name in BINDINGS:
        target = BINDINGS[name]
        if target is None:
            return None
        try:
            func = import_string(target)
        except ImportError as exc:
            raise ImproperlyConfigured(f"ERP_HOOKS[{name!r}] = {target!r}: {exc}") from exc
        if not callable(func):
            raise ImproperlyConfigured(f"ERP_HOOKS[{name!r}] = {target!r} is not callable.")
        return func
    return _registered.get(name)


@receiver(setting_changed)
def _reload_bindings(sender, setting, **kwargs) -> None:
    """Rebuild when a test overrides ERP_HOOKS."""
    if setting != "ERP_HOOKS":
        return
    rebuilt = build_bindings()
    BINDINGS.clear()
    BINDINGS.update(rebuilt)
