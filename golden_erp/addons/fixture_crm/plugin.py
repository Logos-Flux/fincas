"""Registers the fixture adapters. Test settings only::

    GOLDEN_PLUGINS = ["golden_erp.addons.fixture_crm.plugin.FixtureCrmPlugin"]
"""

from __future__ import annotations

from typing import Any, Dict, Type

from core.plugins.base import BasePlugin

from .adapter import FixtureCrmAdapter, FixtureOAuthAdapter


class FixtureCrmPlugin(BasePlugin):
    name = "fixture_crm"
    verbose_name = "Fixture CRM adapters (tests only)"
    author = "Fincas"
    description = "Invented providers that exercise the live CRM adapter contract. Never install in production."

    def get_crm_adapters(self) -> Dict[str, Type[Any]]:
        return {cls.provider_id: cls for cls in (FixtureCrmAdapter, FixtureOAuthAdapter)}
