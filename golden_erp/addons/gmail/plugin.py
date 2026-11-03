"""Registers the Gmail adapter::

    GOLDEN_PLUGINS = [..., "golden_erp.addons.gmail.plugin.GmailPlugin"]
"""

from __future__ import annotations

from typing import Any, Dict, Type

from core.plugins.base import BasePlugin

from .adapter import GmailAdapter


class GmailPlugin(BasePlugin):
    name = "gmail"
    verbose_name = "Gmail timeline"
    author = "Fincas"
    description = ("Logs staff mailboxes' mail with known contacts as email communications "
                   "(per-user read-only grant).")

    def get_crm_adapters(self) -> Dict[str, Type[Any]]:
        return {GmailAdapter.provider_id: GmailAdapter}
