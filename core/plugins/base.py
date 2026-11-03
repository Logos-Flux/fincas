"""Base classes and data models for Fincas plugins and extensions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type


@dataclass(frozen=True)
class DashboardWidget:
    """Dashboard widget contributed by a plugin."""

    id: str
    title: str
    component_or_template: str
    width_cols: int = 4  # 1-12 grid column width
    permission: Optional[str] = None
    order: int = 100
    context_callback: Optional[Callable[..., Dict[str, Any]]] = None


@dataclass(frozen=True)
class TemplateExtension:
    """Markup a plugin adds to a page slot (``get_template_extensions()``).

    The page renders every extension targeting a slot with
    ``{% plugin_slot "<slot>" %}`` (``core.templatetags.erp_tags``), in
    ``order``, with the page's own context. Existing slots are listed in
    docs/plugins.md, "Template slots" (e.g. ``product_profile_panels``).
    """

    target_slot: str  # e.g. 'product_profile_panels'
    template_name: str
    order: int = 100


@dataclass(frozen=True)
class TokenScope:
    """A bearer-token scope contributed by a plugin (``get_token_scopes()``).

    ``core.authentication`` mints the name into the token's ``scope`` claim and
    refuses any bearer whose scope is not registered, so a plugin's scopes
    exist exactly as long as the plugin is installed.

    A scoped token still authenticates as its subject's service account (the
    full-access ``api-service`` for the legacy key), so every non-full scope
    is **path-bound by construction**: ``path_prefix`` is
    required, and the token authenticates nowhere outside it (checked on the
    raw and the normalized ``path_info``, so ``..`` cannot escape). Widening
    a scope means an explicit, broader prefix.

    - ``max_lifetime_s``: the longest token issued for it, 60..2592000
      (30 days; ``registry.MAX_SCOPE_LIFETIME_S``). Defaults to 24 h. A long
      cap suits a client that cannot refresh (a paired browser extension);
      the path binding is what keeps such a token narrow, so over 24 h the
      scope also needs a ``check`` and a prefix of three or more segments.
    - ``claims``: ``() -> dict`` of extra claims added at issuance, e.g. an
      ``aud`` binding the token to the issuing destination.
    - ``check``: ``(request, payload) -> bool``, run on every authentication.
      Anything but ``True`` refuses the token (raising ``AuthenticationFailed``
      also works and sets the message).
    - ``verifies_aud``: core never verifies ``aud``; a token carrying one is
      refused unless its scope sets this, promising that ``check`` does.
    """

    name: str
    description: str = ""
    path_prefix: Optional[str] = None
    max_lifetime_s: int = 86400
    claims: Optional[Callable[[], Dict[str, Any]]] = None
    check: Optional[Callable[[Any, Dict[str, Any]], bool]] = None
    verifies_aud: bool = False


@dataclass(frozen=True)
class ListColumn:
    """A column a plugin adds to a back-office list (``get_list_columns()``).

    docs/crm/phase4-fields.md 5.1: the route by which a deployment's
    one-to-one extension model (2.4) reaches a list. The plugin **names** the
    value; core reads it in the list's own query:

    - ``id``: ``"<plugin name>.<name>"`` (lower-case); the CSV header and the
      preference key. A plugin cannot claim another plugin's or core's ids.
    - ``path``: a model path from the list's model to a concrete field,
      through forward foreign keys or one-to-one relations (either side), e.g.
      ``"qualification__budget"``. Reverse foreign keys, many-to-many and the
      ``auth`` app are refused, so a column can neither multiply rows nor
      read credentials. Core annotates ``F(path)``; there is no callable, so a
      plugin column adds no query and runs no ORM of its own.
    - ``format``: ``text``, ``integer``, ``decimal``, ``money``, ``date``,
      ``datetime``, ``boolean`` or ``url``.
    - ``sortable``: allowed (the value is a real column; that is the point of
      an extension model). ``priority`` 1-3 (5.6), ``default`` (shown when the
      user has no preference), ``pii`` (exported only with
      ``parties.export_pii``), ``export`` (part of "All fields").

    A bad entry is ``ImproperlyConfigured`` naming the plugin, raised when the
    list first renders.
    """

    id: str
    label: str
    path: str
    format: str = "text"
    sortable: bool = False
    priority: int = 3
    default: bool = False
    pii: bool = False
    export: bool = True


class BasePlugin:
    """Base class that all Fincas plugins must inherit from.

    Subclasses define plugin metadata and override hook methods to extend
    core ERP behavior without modifying core code.
    """

    # --- Required Metadata ---
    name: str = ""  # Unique machine-readable identifier (e.g., 'webhook_alerts')
    verbose_name: str = ""  # Human-readable label
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    settings_key: Optional[str] = None  # Key under settings if plugin has config

    #: Auth for URLs from ``get_urls()``. ``"staff"`` (default) wraps every
    #: view in the /erp/ login gate plus an is_staff check; ``"public"`` mounts
    #: them as-is, so the views must enforce their own auth. Any other value
    #: fails startup.
    url_auth: str = "staff"

    def __init__(self) -> None:
        if not self.name:
            raise ValueError(f"Plugin {self.__class__.__name__} must define a non-empty 'name'.")
        if not self.verbose_name:
            self.verbose_name = self.name.replace("_", " ").title()

    def on_ready(self) -> None:
        """Called once when Django is fully initialized (AppConfig.ready)."""
        pass

    def get_nav_items(self) -> List[Dict[str, Any]]:
        """Return a list of sidebar navigation item dictionaries or NavItem instances.

        Example:
            return [{
                "type": "link",
                "label": "Custom Analytics",
                "section": "analytics",
                "url_name": "erp-analytics-dashboard",
                "icon": "ti ti-chart-dots",
                "color": "text-primary",
                "staff_only": False,
            }]
        """
        return []

    def get_dashboard_widgets(self) -> List[DashboardWidget]:
        """Return a list of DashboardWidget instances to display on the main dashboard."""
        return []

    def get_template_extensions(self) -> List[TemplateExtension]:
        """Return template snippets to inject into defined extension slots."""
        return []

    def get_payment_gateways(self) -> Dict[str, Type[Any]]:
        """Return a mapping of {gateway_id: GatewayClass}."""
        return {}

    def get_payroll_providers(self) -> Dict[str, Type[Any]]:
        """Return a mapping of {provider_id: BasePayrollProvider subclass}.

        Payroll is a sync, not a module (Step 5): adapters pull provider
        runs, the ERP books the cost lines. See core/plugins/payroll.py
        for the contract.
        """
        return {}

    def get_crm_export_adapters(self) -> Dict[str, Type[Any]]:
        """Return a mapping of {provider_id: BaseCrmExportAdapter subclass}.

        A CRM export adapter turns a provider's files (a file export or a
        recorded API crawl) into the canonical import format; the importer
        reads only that. ``manage.py crm_export_adapt --provider <id>`` runs
        it. A provider id claimed twice raises ``ImproperlyConfigured`` naming
        both plugins when the adapters are collected (``crm_export_adapt``).
        See core/plugins/crm_export.py and docs/plugins.md, "CRM export adapters".
        """
        return {}

    def get_crm_adapters(self) -> Dict[str, Type[Any]]:
        """Return a mapping of {provider_id: BaseCrmAdapter subclass}.

        A live CRM adapter (docs/crm/phase6-adapters.md section 2): per-company
        accounts, encrypted credentials, inbound by poll or webhook through
        one ``ingest``, outbound through outbox consumers. A provider id
        claimed twice raises ``ImproperlyConfigured`` naming both plugins.
        See core/plugins/crm_adapter.py and docs/plugins.md, "CRM adapters".
        """
        return {}

    def get_communication_kinds(self) -> List[Any]:
        """Return ``communications.kinds.CommunicationKind`` entries this plugin adds.

        Phase 6 adapters (calls, meetings, mail) register their kinds here;
        they merge into the registry after ``GOLDEN_COMMUNICATION_KINDS``. A
        name claimed twice (by two plugins, or redefining a built-in or a
        deployment kind) fails startup naming both. See docs/plugins.md,
        "Communication kinds".
        """
        return []

    def get_outbox_consumers(self) -> List[Any]:
        """Return ``core.outbox.OutboxConsumer`` entries this plugin owns.

        A consumer is durable work fed by lifecycle signals (its ``build``
        runs in the emitting transaction) or by ``core.outbox.enqueue``, and
        handled by ``manage.py process_outbox`` with no transaction open.
        Anything that does I/O belongs here, never in a signal receiver. A
        name claimed twice fails naming both owners. See docs/plugins.md,
        "Outbox consumers".

        The default returns the consumers of this plugin's CRM adapters
        (``BaseCrmAdapter.outbox_consumers()``, docs/crm/phase6-adapters.md
        2.2); a plugin that overrides it and also ships adapters extends
        ``super().get_outbox_consumers()``.
        """
        consumers: List[Any] = []
        for cls in (self.get_crm_adapters() or {}).values():
            consumers.extend(cls.outbox_consumers())
        return consumers

    def get_shipping_carriers(self) -> Dict[str, Type[Any]]:
        """Return a mapping of {carrier_id: CarrierClass}."""
        return {}

    def get_token_scopes(self) -> List[TokenScope]:
        """Return bearer-token scopes this plugin's API accepts.

        Names must be unique across core and every plugin; a duplicate fails
        startup. See docs/plugins.md, "API token scopes".
        """
        return []

    def get_list_columns(self, list_id: str) -> List[ListColumn]:
        """Return ``ListColumn`` entries for the list ``list_id``
        (``parties.party``, ``crm.engagement``, ``crm.task``, ``crm.lead`` …).

        See docs/plugins.md, "List columns".
        """
        return []

    def get_urls(self) -> List[Any]:
        """Return Django URL patterns contributed by this plugin.

        Mounted at the site root after every core route (a plugin can never
        shadow a core URL), so give full paths, e.g. ``path("erp/ebay/", ...)``.
        Two plugins returning the same pattern fail startup. Views are
        staff-gated unless ``url_auth = "public"``.
        """
        return []
