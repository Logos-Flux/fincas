"""Registers the ClickUp task portal adapter::

    GOLDEN_PLUGINS = ["golden_erp.addons.clickup.plugin.ClickUpPlugin"]

Then add a ClickUp account under Connected accounts (docs/crm/adapters.md,
"ClickUp"). Outbox consumers: ``clickup.task`` (push), ``clickup.inbound``
(webhooks), ``clickup.erase`` (erasure, enqueued by ``erase_party``).
"""

from __future__ import annotations

from typing import Any, Dict, Type

from core.plugins.base import BasePlugin

from .adapter import ClickUpAdapter


class ClickUpPlugin(BasePlugin):
    name = "clickup"
    verbose_name = "ClickUp task portal"
    author = "Fincas"
    description = "Pushes Fincas tasks to mapped ClickUp lists and mirrors them back; ClickUp owns the task."

    def get_crm_adapters(self) -> Dict[str, Type[Any]]:
        return {ClickUpAdapter.provider_id: ClickUpAdapter}
