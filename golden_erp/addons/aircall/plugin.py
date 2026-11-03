"""Registers the Aircall adapter::

    GOLDEN_PLUGINS = [..., "golden_erp.addons.aircall.plugin.AircallPlugin"]

``call`` is a built-in communication kind (``communications.kinds``), so the
plugin adds no kind; the adapter passes ``known_max_version`` instead
(docs/crm/phase6-adapters.md problem 13).
"""

from __future__ import annotations

from typing import Any, Dict, Type

from core.plugins.base import BasePlugin

from .adapter import AircallAdapter


class AircallPlugin(BasePlugin):
    name = "aircall"
    verbose_name = "Aircall calls"
    author = "Fincas"
    description = "Aircall calls as call communications; recordings stay at Aircall."

    def get_crm_adapters(self) -> Dict[str, Type[Any]]:
        return {AircallAdapter.provider_id: AircallAdapter}
