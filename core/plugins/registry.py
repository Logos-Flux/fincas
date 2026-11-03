"""Plugin discovery, registration, and hook dispatch engine for Fincas."""

from __future__ import annotations

import importlib
import logging
import re
import warnings
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Type

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

from core.plugin_apps import canonical_plugin_path, is_legacy_addons_path

from .base import BasePlugin, DashboardWidget, TemplateExtension, TokenScope
from .url_auth import URL_AUTH_CHOICES, URL_AUTH_PUBLIC, require_staff

logger = logging.getLogger("golden_erp.plugins")

#: The unrestricted service scope. Tokens minted before scopes existed carry
#: no ``scope`` claim and are treated as this.
FULL_API_SCOPE = "api"

#: Longest ``max_lifetime_s`` a plugin scope may declare: 30 days. Plugin
#: scopes are path-bound by construction (``path_prefix`` is required), so a
#: long-lived one reaches only its own API — e.g. a paired browser extension
#: that has no Api-Key to refresh with. The full scope stays at 24 h (its
#: ``TokenScope`` default) and the token endpoint's default stays 1 h.
MAX_SCOPE_LIFETIME_S = 30 * 86400

#: Above this a scope must be narrow on purpose: a ``check`` and a prefix of
#: at least three segments (``/api/v1/<plugin>/``). A stateless token cannot
#: be revoked short of rotating API_KEY, so ``/api/`` for 30 days is refused.
LONG_LIVED_SCOPE_S = 86400

#: Scopes core always provides; plugins add theirs via get_token_scopes().
BUILTIN_TOKEN_SCOPES = (
    TokenScope(name=FULL_API_SCOPE, description="Full service scope (the default)."),
)

_SCOPE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_token_scope(scope: Any, owner: str) -> None:
    where = f"Token scope from {owner}"
    if not isinstance(scope, TokenScope):
        raise ImproperlyConfigured(f"{where} is {scope!r}; expected a TokenScope.")
    if not isinstance(scope.name, str) or not _SCOPE_NAME.match(scope.name):
        raise ImproperlyConfigured(
            f"{where} has name {scope.name!r}; use lowercase letters, digits, '-' or '_'."
        )
    if scope.name != FULL_API_SCOPE and scope.path_prefix is None:
        raise ImproperlyConfigured(
            f"{where} {scope.name!r} has no path_prefix. A scoped token "
            "may authenticate as the full-access api-service user, so every non-full scope "
            "must be bound to the API paths it may use."
        )
    if scope.path_prefix is not None and not (
        isinstance(scope.path_prefix, str)
        and scope.path_prefix.startswith("/")
        and scope.path_prefix.endswith("/")
        and len(scope.path_prefix) > 1
    ):
        raise ImproperlyConfigured(
            f"{where} {scope.name!r}: path_prefix {scope.path_prefix!r} must start "
            "and end with '/' and not be '/'."
        )
    if (
        not isinstance(scope.max_lifetime_s, int)
        or isinstance(scope.max_lifetime_s, bool)
        or not 60 <= scope.max_lifetime_s <= MAX_SCOPE_LIFETIME_S
    ):
        raise ImproperlyConfigured(
            f"{where} {scope.name!r}: max_lifetime_s must be an int in "
            f"60..{MAX_SCOPE_LIFETIME_S}, got {scope.max_lifetime_s!r}."
        )
    for attr in ("claims", "check"):
        value = getattr(scope, attr)
        if value is not None and not callable(value):
            raise ImproperlyConfigured(f"{where} {scope.name!r}: {attr} is not callable.")
    if scope.max_lifetime_s > LONG_LIVED_SCOPE_S:
        segments = [s for s in (scope.path_prefix or "").split("/") if s]
        if scope.check is None or len(segments) < 3:
            raise ImproperlyConfigured(
                f"{where} {scope.name!r}: a max_lifetime_s over {LONG_LIVED_SCOPE_S} "
                "needs a check and a path_prefix of at least three segments "
                f"(e.g. /api/v1/<plugin>/), got {scope.path_prefix!r}."
            )
    if scope.verifies_aud and scope.check is None:
        raise ImproperlyConfigured(
            f"{where} {scope.name!r}: verifies_aud=True needs a check to verify it."
        )


def _resolve_plugin_class(entry: Any) -> Type[BasePlugin]:
    """Resolve a plugin class from either a class or an import path string."""
    if isinstance(entry, type) and issubclass(entry, BasePlugin):
        return entry

    if isinstance(entry, str):
        if is_legacy_addons_path(entry):
            # Load the current spelling directly: the same class object the
            # new path gives, so the two spellings cannot register twice.
            message = (
                f"GOLDEN_PLUGINS entry {entry!r} uses the deprecated 'addons' "
                f"package; spell it {canonical_plugin_path(entry)!r}. The old "
                "spelling stops working in v0.6.0."
            )
            warnings.warn(message, DeprecationWarning, stacklevel=2)
            logger.warning(message)
            entry = canonical_plugin_path(entry)
        try:
            module_name, class_name = entry.rsplit(".", 1)
            mod = importlib.import_module(module_name)
            cls = getattr(mod, class_name)
            if not isinstance(cls, type) or not issubclass(cls, BasePlugin):
                raise ImproperlyConfigured(
                    f"Plugin target '{entry}' is not a subclass of BasePlugin."
                )
            return cls
        except (ValueError, ModuleNotFoundError, AttributeError) as exc:
            raise ImproperlyConfigured(
                f"Failed to load Fincas plugin '{entry}': {exc}"
            ) from exc

    raise ImproperlyConfigured(
        f"Invalid plugin entry: {entry!r}. Must be a subclass of BasePlugin or an import string."
    )


class PluginRegistry:
    """Central registry for managing loaded Fincas plugins and dispatching hooks."""

    def __init__(self) -> None:
        self._plugins: OrderedDict[str, BasePlugin] = OrderedDict()
        self._initialized: bool = False
        # Bumped on every reload. Caches derived from the loaded plugins
        # (core.outbox.consumers) key on it, never on id() of the instances:
        # a reload frees the old instances, and CPython reuses their ids for
        # the new ones, so an id()-keyed cache can match a different plugin set.
        self.generation: int = 0
        # Boot path is fail-closed: one malformed entry is logged and
        # skipped so the server still starts. Explicit reload() calls
        # stay strict (raise) so config errors surface loudly in tests
        # and the shell.
        self.reload(raise_on_error=False)

    def reload(self, raise_on_error: bool = True) -> None:
        """Discover and instantiate all configured plugins."""
        self.generation += 1
        self._plugins.clear()

        # 1. Load from settings.GOLDEN_PLUGINS
        configured_entries = getattr(settings, "GOLDEN_PLUGINS", []) or []
        loaded_from: Dict[type, Any] = {}
        for entry in configured_entries:
            try:
                cls = _resolve_plugin_class(entry)
                if cls in loaded_from:
                    raise ImproperlyConfigured(
                        f"GOLDEN_PLUGINS lists {cls.__module__}.{cls.__qualname__} "
                        f"twice ({loaded_from[cls]!r} and {entry!r})"
                        + ("; 'addons.' is the deprecated spelling of "
                           "'golden_erp.addons.'"
                           if is_legacy_addons_path(loaded_from[cls])
                           or is_legacy_addons_path(entry) else "")
                        + ". Keep one entry."
                    )
                plugin_inst = cls()
                loaded_from[cls] = entry
                if plugin_inst.name in self._plugins:
                    raise ImproperlyConfigured(
                        f"Duplicate plugin registration with name '{plugin_inst.name}'."
                    )
            except Exception as exc:
                if raise_on_error:
                    raise
                logger.error(
                    "Skipping unloadable plugin entry %r: %s", entry, exc
                )
                continue
            self._plugins[plugin_inst.name] = plugin_inst

        # 2. Discover from entry points (group: golden_erp.plugins)
        try:
            import sys
            if sys.version_info >= (3, 10):
                from importlib.metadata import entry_points
                discovered = entry_points(group="golden_erp.plugins")
            else:
                import pkg_resources
                discovered = pkg_resources.iter_entry_points("golden_erp.plugins")

            for ep in discovered:
                try:
                    plugin_cls = ep.load()
                    if issubclass(plugin_cls, BasePlugin):
                        inst = plugin_cls()
                        if inst.name not in self._plugins:
                            self._plugins[inst.name] = inst
                except Exception as exc:
                    logger.warning("Failed to load plugin entry point %s: %s", ep.name, exc)
        except Exception as exc:
            logger.debug("Entry point discovery error (non-fatal): %s", exc)

        self._initialized = True

    def initialize_apps(self) -> None:
        """Trigger on_ready hooks on all registered plugins."""
        for plugin in self._plugins.values():
            try:
                plugin.on_ready()
            except Exception as exc:
                logger.error("Error executing on_ready for plugin '%s': %s", plugin.name, exc)

    @property
    def plugins(self) -> Dict[str, BasePlugin]:
        """Return a mapping of registered plugins."""
        return dict(self._plugins)

    def get_plugin(self, name: str) -> Optional[BasePlugin]:
        """Retrieve a registered plugin by name."""
        return self._plugins.get(name)

    def get_nav_items(self) -> List[Dict[str, Any]]:
        """Collect all navigation items contributed by plugins."""
        items: List[Dict[str, Any]] = []
        for plugin in self._plugins.values():
            items.extend(plugin.get_nav_items())
        return items

    def get_dashboard_widgets(self) -> List[DashboardWidget]:
        """Collect and sort all dashboard widgets contributed by plugins."""
        widgets: List[DashboardWidget] = []
        for plugin in self._plugins.values():
            widgets.extend(plugin.get_dashboard_widgets())
        return sorted(widgets, key=lambda w: w.order)

    def get_template_extensions(self, slot: str) -> List[TemplateExtension]:
        """Collect template snippets targeting a specific UI slot."""
        extensions: List[TemplateExtension] = []
        for plugin in self._plugins.values():
            for ext in plugin.get_template_extensions():
                if ext.target_slot == slot:
                    extensions.append(ext)
        return sorted(extensions, key=lambda e: e.order)

    def get_payment_gateways(self) -> Dict[str, Type[Any]]:
        """Collect all pluggable payment gateway classes."""
        gateways: Dict[str, Type[Any]] = {}
        for plugin in self._plugins.values():
            gateways.update(plugin.get_payment_gateways())
        return gateways

    def get_payroll_providers(self) -> Dict[str, Type[Any]]:
        """Collect payroll adapters. Duplicate ids fail closed naming both
        plugins (XD-3) — silent last-wins would route payroll to the wrong
        provider."""
        providers: Dict[str, Type[Any]] = {}
        owners: Dict[str, str] = {}
        for plugin in self._plugins.values():
            for provider_id, cls in plugin.get_payroll_providers().items():
                if provider_id in providers:
                    raise ImproperlyConfigured(
                        f"Duplicate payroll provider id {provider_id!r}: "
                        f"{owners[provider_id]} and {plugin.name} both claim "
                        "it. Rename one — payroll must never silently "
                        "re-route."
                    )
                providers[provider_id] = cls
                owners[provider_id] = plugin.name
        return providers

    def get_crm_export_adapters(self) -> Dict[str, Type[Any]]:
        """Collect CRM export adapters (docs/crm/phase5-importer.md 1.4).

        Fail-closed like payroll providers: a provider id claimed by two
        plugins, a class that is not a ``BaseCrmExportAdapter``, or a key that
        differs from the class's ``provider_id`` raises ``ImproperlyConfigured``
        naming the plugin. An import must never silently run another
        provider's adapter.
        """
        from .crm_export import BaseCrmExportAdapter

        adapters: Dict[str, Type[Any]] = {}
        owners: Dict[str, str] = {}
        for plugin in self._plugins.values():
            for provider_id, cls in (plugin.get_crm_export_adapters() or {}).items():
                if not (isinstance(cls, type) and issubclass(cls, BaseCrmExportAdapter)):
                    raise ImproperlyConfigured(
                        f"Plugin {plugin.name!r} registers CRM export adapter {provider_id!r} as {cls!r}; "
                        "expected a BaseCrmExportAdapter subclass (core/plugins/crm_export.py)."
                    )
                if cls.provider_id != provider_id:
                    raise ImproperlyConfigured(
                        f"Plugin {plugin.name!r} registers {cls.__name__} under {provider_id!r}, but its "
                        f"provider_id is {cls.provider_id!r}. Register it under its own provider_id."
                    )
                if provider_id in adapters:
                    raise ImproperlyConfigured(
                        f"Duplicate CRM export adapter {provider_id!r}: {owners[provider_id]} and "
                        f"{plugin.name} both claim it. Rename one — an import must never silently "
                        "switch adapters."
                    )
                adapters[provider_id] = cls
                owners[provider_id] = plugin.name
        return adapters

    def get_crm_adapters(self) -> Dict[str, Type[Any]]:
        """Collect live CRM adapters (docs/crm/phase6-adapters.md 2.2).

        Fail-closed like the export adapters: a class that is not a
        ``BaseCrmAdapter``, a key that differs from its ``provider_id``, a
        malformed id (``[a-z][a-z0-9_]{0,31}``: it is persisted as
        ``Communication.provider`` and ``ExternalRef.source``) or an id claimed
        by two plugins raises ``ImproperlyConfigured`` naming the plugin(s).
        """
        from .crm_adapter import PROVIDER_ID, BaseCrmAdapter

        adapters: Dict[str, Type[Any]] = {}
        owners: Dict[str, str] = {}
        for plugin in self._plugins.values():
            for provider_id, cls in (plugin.get_crm_adapters() or {}).items():
                if not (isinstance(cls, type) and issubclass(cls, BaseCrmAdapter)):
                    raise ImproperlyConfigured(
                        f"Plugin {plugin.name!r} registers CRM adapter {provider_id!r} as {cls!r}; "
                        "expected a BaseCrmAdapter subclass (core/plugins/crm_adapter.py)."
                    )
                if cls.provider_id != provider_id:
                    raise ImproperlyConfigured(
                        f"Plugin {plugin.name!r} registers {cls.__name__} under {provider_id!r}, but its "
                        f"provider_id is {cls.provider_id!r}. Register it under its own provider_id."
                    )
                if not isinstance(provider_id, str) or not PROVIDER_ID.match(provider_id):
                    raise ImproperlyConfigured(
                        f"Plugin {plugin.name!r}: CRM adapter id {provider_id!r} must match "
                        "[a-z][a-z0-9_]{0,31} (it is stored as Communication.provider and ExternalRef.source)."
                    )
                if provider_id in adapters:
                    raise ImproperlyConfigured(
                        f"Duplicate CRM adapter {provider_id!r}: {owners[provider_id]} and "
                        f"{plugin.name} both claim it. Rename one — an account must never silently "
                        "switch adapters."
                    )
                adapters[provider_id] = cls
                owners[provider_id] = plugin.name
        return adapters

    def get_communication_kinds(self) -> List[tuple]:
        """``(plugin name, kind)`` for every communication kind a plugin adds.

        Two plugins claiming one name raise ``ImproperlyConfigured`` naming
        both (the payroll rule). ``communications.kinds`` validates each entry
        and refuses a plugin redefining a built-in or deployment kind.
        """
        collected: List[tuple] = []
        owners: Dict[str, str] = {}
        for plugin in self._plugins.values():
            for kind in plugin.get_communication_kinds() or []:
                name = kind.get("name") if isinstance(kind, dict) else getattr(kind, "name", None)
                if isinstance(name, str) and name in owners:
                    raise ImproperlyConfigured(
                        f"Duplicate communication kind {name!r}: plugin {owners[name]!r} and "
                        f"plugin {plugin.name!r} both define it. Rename one — a kind must "
                        "never silently change meaning."
                    )
                if isinstance(name, str):
                    owners[name] = plugin.name
                collected.append((plugin.name, kind))
        return collected

    def get_list_columns(self, list_id: str) -> List[tuple]:
        """``(plugin name, ListColumn)`` for every column a plugin adds to
        ``list_id``. ``core.list_columns.plugin_columns`` validates them."""
        collected: List[tuple] = []
        for plugin in self._plugins.values():
            for col in plugin.get_list_columns(list_id) or []:
                collected.append((plugin.name, col))
        return collected

    def get_shipping_carriers(self) -> Dict[str, Type[Any]]:
        """Collect all pluggable shipping carrier classes."""
        carriers: Dict[str, Type[Any]] = {}
        for plugin in self._plugins.values():
            carriers.update(plugin.get_shipping_carriers())
        return carriers

    def get_token_scopes(self) -> Dict[str, TokenScope]:
        """Every accepted bearer-token scope: core's built-ins plus each plugin's.

        Fail-closed like payroll providers and URLs: a malformed scope or a
        name claimed twice (by two plugins, or a plugin and core) raises
        ``ImproperlyConfigured`` naming both owners. Never last-wins: a
        re-defined scope would silently change what its tokens may reach.
        """
        scopes: Dict[str, TokenScope] = {}
        owners: Dict[str, str] = {}
        for scope in BUILTIN_TOKEN_SCOPES:
            scopes[scope.name] = scope
            owners[scope.name] = "core"
        for plugin in self._plugins.values():
            for scope in plugin.get_token_scopes() or []:
                _validate_token_scope(scope, f"plugin {plugin.name!r}")
                if scope.name in scopes:
                    raise ImproperlyConfigured(
                        f"Duplicate token scope {scope.name!r}: {owners[scope.name]} "
                        f"and {plugin.name} both define it. Rename one — a scope "
                        "must never silently change meaning."
                    )
                scopes[scope.name] = scope
                owners[scope.name] = plugin.name
        return scopes

    def get_urls(self, reserved: Optional[List[Any]] = None) -> List[Any]:
        """Collect plugin URL patterns, staff-gated unless a plugin opts out.

        Fail-closed like the ERP_THEMES / ERP_NAV / payroll registries: two
        plugins claiming the same pattern or URL name raise
        ``ImproperlyConfigured`` naming both, never first-wins.
        ``golden_erp/urls.py`` mounts the result after every core route, so a
        plugin cannot shadow a core *path*; it passes the core patterns as
        ``reserved`` so a plugin cannot take over a core URL *name* either
        (``reverse()`` prefers the last-listed pattern, i.e. the plugin's).
        """
        from django.urls import URLPattern, URLResolver

        collected: List[Any] = []
        owners: Dict[str, str] = {}
        name_owners: Dict[str, str] = {
            name: "core" for name in _url_names(reserved or [])
        }
        for plugin in self._plugins.values():
            auth = getattr(plugin, "url_auth", None)
            if auth not in URL_AUTH_CHOICES:
                raise ImproperlyConfigured(
                    f"Plugin {plugin.name!r} has url_auth={auth!r}; must be "
                    f"one of {', '.join(URL_AUTH_CHOICES)}."
                )
            patterns = list(plugin.get_urls() or [])
            for entry in patterns:
                if not isinstance(entry, (URLPattern, URLResolver)):
                    raise ImproperlyConfigured(
                        f"Plugin {plugin.name!r} get_urls() returned "
                        f"{entry!r}; expected path()/re_path() entries."
                    )
                route = str(entry.pattern)
                owner = owners.get(route)
                if owner is not None and owner != plugin.name:
                    raise ImproperlyConfigured(
                        f"Duplicate plugin URL {route!r}: {owner} and "
                        f"{plugin.name} both claim it. Rename one — plugin "
                        "routes must never silently shadow each other."
                    )
                owners[route] = plugin.name
            for name in _url_names(patterns):
                owner = name_owners.get(name)
                if owner is not None and owner != plugin.name:
                    raise ImproperlyConfigured(
                        f"Duplicate URL name {name!r}: {owner} and {plugin.name} "
                        "both define it, so reverse()/{% url %} would silently "
                        "point at one of them. Rename the plugin's route or "
                        "mount it under a namespace."
                    )
                name_owners[name] = plugin.name
            if not patterns:
                continue
            if auth == URL_AUTH_PUBLIC:
                logger.warning(
                    "Plugin %r mounts %d URL pattern(s) WITHOUT the staff gate "
                    "(url_auth='public'); its views must enforce their own auth.",
                    plugin.name, len(patterns),
                )
                collected.extend(patterns)
            else:
                collected.extend(require_staff(patterns))
        return collected


def _url_names(patterns: List[Any], prefix: str = "") -> List[str]:
    """Every reversible URL name in ``patterns`` (namespaced as ``ns:name``),
    descending through ``include()`` resolvers."""
    from django.urls import URLPattern, URLResolver

    names: List[str] = []
    for entry in patterns:
        if isinstance(entry, URLPattern):
            if entry.name:
                names.append(prefix + entry.name)
        elif isinstance(entry, URLResolver):
            inner = f"{prefix}{entry.namespace}:" if entry.namespace else prefix
            names.extend(_url_names(list(entry.url_patterns), inner))
    return names


# Singleton instance
plugin_registry = PluginRegistry()


def _app_config_paths() -> Dict[str, str]:
    """Every way an installed app can be named -> its app label."""
    from django.apps import apps

    names: Dict[str, str] = {}
    for config in apps.get_app_configs():
        names[config.name] = config.label
        cls = type(config)
        names[f"{cls.__module__}.{cls.__qualname__}"] = config.label
    return names


def _is_django_app_entry(entry: Any, app_paths: Dict[str, str]) -> bool:
    from django.apps import AppConfig
    from django.utils.module_loading import import_string

    if isinstance(entry, type):
        return issubclass(entry, AppConfig)
    if not isinstance(entry, str):
        return False
    entry = canonical_plugin_path(entry)  # never import the deprecated alias
    if entry in app_paths:
        return True
    try:
        importlib.import_module(entry)
        return True  # a module, not a class: an app package (or no plugin at all)
    except Exception:
        pass  # not a module; unloadable entries stay the registry's to log
    try:
        target = import_string(entry)
    except Exception:
        return False
    return isinstance(target, type) and issubclass(target, AppConfig)


def check_plugin_classification() -> None:
    """Startup guard for the GOLDEN_PLUGINS / GOLDEN_PLUGIN_APPS boundary.

    Raises ``ImproperlyConfigured`` when a Django app is listed in
    GOLDEN_PLUGINS, a BasePlugin class in GOLDEN_PLUGIN_APPS, or a
    GOLDEN_PLUGIN_APPS entry never reached INSTALLED_APPS (overlay forgot
    ``compose_installed_apps``). Misclassification is never silent: the
    registry's boot path otherwise just logs and skips a bad entry.
    """
    from django.utils.module_loading import import_string

    app_paths = _app_config_paths()
    for index, entry in enumerate(getattr(settings, "GOLDEN_PLUGINS", []) or []):
        if _is_django_app_entry(entry, app_paths):
            raise ImproperlyConfigured(
                f"GOLDEN_PLUGINS[{index}] = {entry!r} is a Django app/module, "
                "not a BasePlugin class. List Django apps in "
                "GOLDEN_PLUGIN_APPS; GOLDEN_PLUGINS takes BasePlugin glue "
                "classes only."
            )

    installed = set(settings.INSTALLED_APPS)
    for index, entry in enumerate(getattr(settings, "GOLDEN_PLUGIN_APPS", []) or []):
        where = f"GOLDEN_PLUGIN_APPS[{index}] = {entry!r}"
        target = entry
        if isinstance(entry, str):
            try:
                target = import_string(canonical_plugin_path(entry))
            except Exception:
                target = None  # an app package path, not module.attr
        if isinstance(target, type) and issubclass(target, BasePlugin):
            raise ImproperlyConfigured(
                f"{where} is a BasePlugin glue class, not a Django app. List "
                "it in GOLDEN_PLUGINS."
            )
        if entry not in installed:
            raise ImproperlyConfigured(
                f"{where} is not in INSTALLED_APPS. End the settings module "
                "with INSTALLED_APPS = compose_installed_apps(INSTALLED_APPS, "
                "GOLDEN_PLUGIN_APPS) (core.plugin_apps)."
            )


@receiver(setting_changed)
def _on_setting_changed(sender, setting, **kwargs) -> None:
    """Reload registry when GOLDEN_PLUGINS changes in tests or runtime.

    Fail-closed like the boot path: a bad value sent via
    override_settings must not raise out of the signal handler.
    Explicit reload() calls stay strict.
    """
    if setting == "GOLDEN_PLUGINS":
        plugin_registry.reload(raise_on_error=False)
