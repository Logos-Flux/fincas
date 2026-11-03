"""The HubSpot export add-on: registers the adapter with the plugin registry.

Enable it in the deployment's settings::

    GOLDEN_PLUGINS = ["golden_erp.addons.hubspot_export.plugin.HubSpotExportPlugin"]

then ``manage.py crm_export_adapt --provider hubspot --input <dir> --output <dir>``.
"""

from __future__ import annotations

from typing import Any, Dict, Type

from core.plugins.base import BasePlugin

from .adapter import ADAPTER_VERSION, HubSpotExportAdapter


class HubSpotExportPlugin(BasePlugin):
    name = "hubspot_export"
    verbose_name = "HubSpot export adapter"
    version = ADAPTER_VERSION
    author = "Fincas"
    description = "Turns a HubSpot file export or recorded API crawl into the canonical CRM import format."

    def get_crm_export_adapters(self) -> Dict[str, Type[Any]]:
        return {HubSpotExportAdapter.provider_id: HubSpotExportAdapter}
