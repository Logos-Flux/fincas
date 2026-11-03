"""Plugin glue for the Klaviyo adapter::

    GOLDEN_PLUGINS = [..., "golden_erp.addons.klaviyo.plugin.KlaviyoPlugin"]
"""

from __future__ import annotations

from typing import Any, Dict, List, Type

from core.plugins.base import BasePlugin

from .adapter import KIND, KlaviyoAdapter
from .values import SCHEMA_VERSION


class KlaviyoPlugin(BasePlugin):
    name = "klaviyo"
    verbose_name = "Klaviyo marketing email"
    author = "Fincas"
    description = ("Pushes marketing consent and profiles to Klaviyo and pulls sends, unsubscribes, "
                   "bounces and complaints back. Fincas owns consent.")

    def get_crm_adapters(self) -> Dict[str, Type[Any]]:
        return {KlaviyoAdapter.provider_id: KlaviyoAdapter}

    def get_communication_kinds(self) -> List[Any]:
        from communications.kinds import OUTBOUND, CommunicationKind

        # A campaign send: never resets a deal's stale clock, never threads an engagement.
        return [CommunicationKind(KIND, "Marketing email", "ti ti-mail-fast", directions=(OUTBOUND,),
                                  counts_as_activity=False, raw_schema_version=SCHEMA_VERSION)]
