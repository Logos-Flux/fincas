"""ERP theme registry — the settings-driven loader behind the navbar picker.

Same config-loader shape as ``chat/llm/registry.py`` (golden §3.7 pattern,
applied here per golden §5.4 step 5): the module ships **zero** themes; the
instance supplies its own through ``settings.ERP_THEMES`` plus
``settings.ERP_DEFAULT_THEME``, and adding a theme is a settings change (drop
a CSS file in ``golden_erp/static/assets/css/themes/`` + append an entry), not an edit
to this module or to any template.

Consumers:

- ``core.context_processors.erp_themes`` serializes the registry (with the
  ``css`` static path resolved to a URL) into every template context; the
  navbar dropdown in ``golden_erp/templates/includes/navigation.html`` iterates it, and
  ``golden_erp/templates/base.html`` embeds it as JSON via ``json_script`` for the
  pre-paint snippet + the picker JS in ``golden_erp/templates/includes/scripts.html``.
- ``localStorage['erp-theme']`` stores ``ThemeConfig.name``. Names are
  therefore persisted client-side forever — treat them like the chat
  registry's model keys: retire by keeping the old name resolvable (the JS
  falls back to the default for unknown names, so removal is safe but loses
  the user's choice).

Bad config raises ``ImproperlyConfigured`` at import rather than dropping the
entry: a theme that silently vanishes from the picker looks identical to one
that was never configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

MODES = ("light", "dark")


@dataclass(frozen=True)
class ThemeConfig:
    name: str          # Stable id persisted in localStorage['erp-theme']
    label: str         # Picker display text
    mode: str          # Value for <html data-bs-theme> — "light" | "dark"
    # Static path for <link id="theme-css"> (e.g. "assets/css/themes/eva-01.css").
    # None = no theme stylesheet: the framework's stock look. The picker JS
    # handles None by setting link.disabled = true.
    css: str | None = None
    # Pre-paint <html> background colour ('' = none) — the no-flash guard
    # applied before first paint so a dark theme never flashes white.
    bg: str = ""


def _theme_from_config(entry: Any, index: int) -> ThemeConfig:
    """Turn one ERP_THEMES dict into a validated ThemeConfig."""
    where = f"ERP_THEMES[{index}]"
    if isinstance(entry, ThemeConfig):
        theme = entry
    elif isinstance(entry, dict):
        try:
            theme = ThemeConfig(**entry)
        except TypeError as exc:
            raise ImproperlyConfigured(f"{where} is not a valid ThemeConfig: {exc}") from exc
    else:
        raise ImproperlyConfigured(
            f"{where} must be a dict of ThemeConfig kwargs, got {type(entry).__name__}."
        )
    for field in ("name", "label"):
        if not getattr(theme, field):
            raise ImproperlyConfigured(f"{where} is missing a non-empty {field}.")
    if theme.mode not in MODES:
        raise ImproperlyConfigured(
            f"{where} has mode={theme.mode!r}; must be one of {', '.join(MODES)}."
        )
    if theme.css is not None and not theme.css:
        raise ImproperlyConfigured(f"{where} has an empty css path; use None for no stylesheet.")
    return theme


def build_themes() -> dict[str, ThemeConfig]:
    """settings.ERP_THEMES as a validated dict keyed by name, in order."""
    configured = getattr(settings, "ERP_THEMES", []) or []
    if not isinstance(configured, (list, tuple)):
        raise ImproperlyConfigured(
            f"ERP_THEMES must be a list of ThemeConfig kwargs dicts, got {type(configured).__name__}."
        )
    themes: dict[str, ThemeConfig] = {}
    for i, entry in enumerate(configured):
        theme = _theme_from_config(entry, i)
        if theme.name in themes:
            raise ImproperlyConfigured(f"ERP_THEMES reuses the name {theme.name!r}.")
        themes[theme.name] = theme
    if not themes:
        raise ImproperlyConfigured("ERP_THEMES is empty — the picker needs at least one theme.")
    return themes


def build_default(themes: dict[str, ThemeConfig]) -> str:
    """settings.ERP_DEFAULT_THEME, validated against the registry."""
    default = getattr(settings, "ERP_DEFAULT_THEME", None)
    if not default:
        raise ImproperlyConfigured("ERP_DEFAULT_THEME is not set.")
    if default not in themes:
        raise ImproperlyConfigured(
            f"ERP_DEFAULT_THEME={default!r} is not a registered theme name "
            f"(have: {', '.join(themes)})."
        )
    return default


# Mutated in place on reload (below) so that `from … import THEMES` holds.
THEMES: dict[str, ThemeConfig] = build_themes()
DEFAULT_THEME: str = build_default(THEMES)


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    """Rebuild when a test overrides the instance settings (chat-registry style)."""
    global DEFAULT_THEME
    if setting not in ("ERP_THEMES", "ERP_DEFAULT_THEME"):
        return
    rebuilt = build_themes()
    rebuilt_default = build_default(rebuilt)
    THEMES.clear()
    THEMES.update(rebuilt)
    DEFAULT_THEME = rebuilt_default


def default_theme() -> ThemeConfig:
    return THEMES[DEFAULT_THEME]
