"""ERP sidebar nav registry — the settings-driven loader behind the sidebar.

Same config-loader shape as ``theme_registry.py`` / ``chat/llm/registry.py``
(golden §3.7 pattern): the sidebar's item list is *config*, not markup.
``settings.ERP_NAV`` is an ordered list of item dicts; the instance supplies
its own (which apps it runs, in what order), and ``golden_erp/templates/includes/
sidebar.html`` renders whatever it's given rather than hardcoding
``{% url %}`` calls per app. Adding, removing, or reordering a nav item is a
settings change, not a template edit.

Two item shapes, discriminated by ``type``:

- ``header`` — a section label (e.g. "Sales"). Just ``label`` (+ optional
  ``staff_only``).
- ``link`` — a sidebar link. ``section`` is matched against each view's
  ``current_section`` (``core/views/mixins.py``) to drive the active state;
  ``url_name`` is passed straight to ``{% url %}``; ``icon`` is the full
  icon class string (``ti ti-*`` or a still-loaded ``fas fa-*``/``fab fa-*``
  FontAwesome class — the icon swap in golden Phase 0 only touched Nucleo,
  FontAwesome stays); ``color`` is a Bootstrap text-* utility class.

Bad config raises ``ImproperlyConfigured`` at import rather than dropping the
entry — a vanished sidebar row is indistinguishable from one never
configured (same reasoning as the theme and chat-model registries).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

TYPES = ("header", "link")


@dataclass(frozen=True)
class NavItem:
    type: str
    label: str
    section: str = ""       # link only
    url_name: str = ""      # link only
    icon: str = ""          # link only
    color: str = ""         # link only
    staff_only: bool = False
    # Optional core.label_registry key: the sidebar shows that noun's resolved
    # plural (tenant -> deployment -> default) and ``label`` is the fallback.
    label_key: str = ""


def _nav_item_from_config(entry: Any, index: int) -> NavItem:
    """Turn one ERP_NAV dict into a validated NavItem."""
    where = f"ERP_NAV[{index}]"
    if isinstance(entry, NavItem):
        item = entry
    elif isinstance(entry, dict):
        try:
            item = NavItem(**entry)
        except TypeError as exc:
            raise ImproperlyConfigured(f"{where} is not a valid NavItem: {exc}") from exc
    else:
        raise ImproperlyConfigured(
            f"{where} must be a dict of NavItem kwargs, got {type(entry).__name__}."
        )
    if item.type not in TYPES:
        raise ImproperlyConfigured(
            f"{where} has type={item.type!r}; must be one of {', '.join(TYPES)}."
        )
    if not item.label:
        raise ImproperlyConfigured(f"{where} is missing a non-empty label.")
    if item.label_key:
        # Checked against the label registry as it stands now; a nav item
        # keyed to a deployment party role (party_role.<custom>) needs that
        # role declared in the same settings.
        from . import label_registry

        if not label_registry.is_label_key(item.label_key):
            raise ImproperlyConfigured(
                f"{where} has label_key={item.label_key!r}, which is not a registered label key."
            )
    if item.type == "link":
        for field in ("section", "url_name"):
            if not getattr(item, field):
                raise ImproperlyConfigured(f"{where} is a link but is missing {field}.")
    return item


def build_nav() -> list[NavItem]:
    """settings.ERP_NAV + registered plugins as a validated list, in configured order."""
    configured = list(getattr(settings, "ERP_NAV", []) or [])
    if not isinstance(configured, list):
        raise ImproperlyConfigured(
            f"ERP_NAV must be a list of NavItem kwargs dicts, got {type(configured).__name__}."
        )
    try:
        from core.plugins import plugin_registry
        plugin_nav = plugin_registry.get_nav_items()
        configured.extend(plugin_nav)
    except Exception:
        pass

    return [_nav_item_from_config(entry, i) for i, entry in enumerate(configured)]


# Mutated in place on reload (below) so that `from … import NAV` still sees
# updates — same discipline as theme_registry.THEMES.
NAV: list[NavItem] = build_nav()


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    """Rebuild when a test overrides ERP_NAV or GOLDEN_PLUGINS."""
    if setting not in ("ERP_NAV", "GOLDEN_PLUGINS"):
        return
    rebuilt = build_nav()
    NAV[:] = rebuilt
