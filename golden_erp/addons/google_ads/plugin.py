"""The attribution and Google Ads add-on: registers the adapter and the
``attribution`` token scope.

Enable it in the deployment's settings::

    GOLDEN_PLUGINS = ["golden_erp.addons.google_ads.plugin.GoogleAdsPlugin"]

then connect an account (``IntegrationAccount`` provider ``google_ads``),
run ``process_outbox``, and check what would upload with
``manage.py ads_conversions --account <uuid> --dry-run``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Type

from core.plugins.base import BasePlugin, TokenScope

from .adapter import ADAPTER_VERSION, GoogleAdsAdapter

#: A site or tag manager posting visits: ``POST /api/v1/crm/touchpoints/``
#: and nothing else (path-bound; the key's account also needs
#: ``crm.add_touchpoint``).
ATTRIBUTION_SCOPE = TokenScope(
    name="attribution", path_prefix="/api/v1/crm/touchpoints/",
    description="Post touchpoints (visits, ad clicks) to the CRM.",
)


class GoogleAdsPlugin(BasePlugin):
    name = "google_ads"
    verbose_name = "Attribution and Google Ads offline conversions"
    version = ADAPTER_VERSION
    author = "Fincas"
    description = "Uploads won engagements with a Google click as offline conversions."

    def get_crm_adapters(self) -> Dict[str, Type[Any]]:
        return {GoogleAdsAdapter.provider_id: GoogleAdsAdapter}

    def get_token_scopes(self) -> List[TokenScope]:
        return [ATTRIBUTION_SCOPE]
