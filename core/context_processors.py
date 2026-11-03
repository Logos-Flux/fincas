"""Template context processors for the core app."""

from __future__ import annotations

from django.conf import settings
from django.templatetags.static import static

from . import nav_registry, theme_registry
from .labels import LabelNamespace, get_label, labels_for_request
from .nav_registry import NavItem
from .theme_registry import ThemeConfig


def _serialize(theme: ThemeConfig) -> dict:
    """One registry entry as the plain dict the templates + picker JS consume.

    The static *path* in the registry becomes a resolved *URL* here
    (``css_url``), so the JSON blob embedded by base.html works under any
    STATIC_URL / hashed-storage configuration.
    """
    return {
        "name": theme.name,
        "label": theme.label,
        "mode": theme.mode,
        "bg": theme.bg,
        "css_url": static(theme.css) if theme.css else None,
    }


def erp_themes(request):
    """Adds the theme registry to every template context.

    - ``erp_themes``: serialized entries, in registry order (navbar dropdown
      + the ``json_script`` blob in base.html).
    - ``erp_default_theme``: the default theme's name.
    - ``erp_default_theme_entry``: the serialized default entry — base.html
      renders the server-side ``<link id="theme-css">`` from it.
    """
    # Read through the MODULE, never `from … import DEFAULT_THEME`: the
    # registry rebinds that string when settings change (tests, reload), and
    # a from-import would freeze the value at first import while THEMES —
    # mutated in place — moved on. That skew makes the lookup below raise.
    default = theme_registry.DEFAULT_THEME
    themes = [_serialize(t) for t in theme_registry.THEMES.values()]
    return {
        "erp_themes": themes,
        "erp_default_theme": default,
        "erp_default_theme_entry": next(t for t in themes if t["name"] == default),
    }


def _serialize_nav_item(item: NavItem, namespace: LabelNamespace) -> dict:
    # label_key wins when set (resolved for the request's company); label is
    # the fallback and what the item reads with no label layer.
    text = item.label
    if item.label_key:
        text = get_label(item.label_key, company=namespace.company_id).plural
    return {
        "type": item.type,
        "label": text,
        "section": item.section,
        "url_name": item.url_name,
        "icon": item.icon,
        "color": item.color,
    }


def erp_nav(request):
    """Adds the sidebar nav registry, and where links leave the back office,
    to every template context.

    - ``erp_nav``: ``staff_only`` items (and the headers above them) are
      filtered here, not in the template — ``sidebar.html`` renders exactly
      what it's given, matching the pre-registry ``{% if user.is_staff %}``
      block's behaviour.
    - ``storefront_url``: ``settings.STOREFRONT_URL`` (no trailing slash,
      ``""`` when unset). Templates build storefront links from it ("View on
      site", the document form's product search) and hide them when empty.
    """
    is_staff = getattr(getattr(request, "user", None), "is_staff", False)
    items = [item for item in nav_registry.NAV if not item.staff_only or is_staff]
    namespace = labels_for_request(request)
    return {
        "erp_nav": [_serialize_nav_item(item, namespace) for item in items],
        "storefront_url": (getattr(settings, "STOREFRONT_URL", "") or "").rstrip("/"),
    }


def labels(request):
    """Adds ``labels``: the staff-UI noun labels for the user's company.

    ``{{ labels.crm.engagement }}`` / ``{{ labels.crm.engagement.plural }}``.
    Lazy: the company is resolved on first lookup, and only when a tenant
    layer is configured (``core.labels.company_id_for_request``). A user with
    no profile sees deployment labels.
    """
    return {"labels": labels_for_request(request)}
