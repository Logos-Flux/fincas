"""Fincas — URL patterns (shipped in the package).

A project mounts these at its root with ``path("", include("golden_erp.urls"))``
(or uses ``ROOT_URLCONF = "golden_erp.urls"``, the packaged default). The
patterns assume root mounting: the back office redirects and LOGIN_URL use
absolute ``/erp/`` paths. golden's own ``config/urls.py`` re-exports them.
"""

from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from core.integrations.webhook import webhook_view as integration_webhook
from core.plugins import plugin_registry

# Styled error pages (core.error_views): HTML in the back office, JSON under
# /api/. Django reads these names from ROOT_URLCONF; a project that mounts
# these patterns from its own urls.py must re-export them (config/urls.py does).
handler403 = "core.error_views.permission_denied"
handler404 = "core.error_views.page_not_found"
handler500 = "core.error_views.server_error"

admin.site.site_header = "Fincas"
admin.site.site_title = "Fincas Admin"
admin.site.index_title = "Administration"

urlpatterns = [
    path("", RedirectView.as_view(url="/erp/", permanent=False), name="root-redirect"),
    path("admin/", admin.site.urls),
    # CRM Phase 2 step 6: ahead of core.erp_urls so no /erp/parties/<…>/
    # pattern there can shadow it.
    path("erp/parties/merge-review/", include("parties.merge_urls")),
    path("erp/", include("core.erp_urls")),

    # OpenAPI 3.0 Documentation & Schemas
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),

    # REST APIs (v1)
    path("api/v1/core/", include("core.urls")),
    path("api/v1/products/", include("products.urls")),
    path("api/v1/customers/", include("customers.urls")),
    path("api/v1/orders/", include("orders.urls")),
    path("api/v1/payments/", include("payments.urls")),
    path("api/v1/purchasing/", include("purchasing.urls")),
    path("api/v1/fulfillment/", include("fulfillment.urls")),
    path("api/v1/inventory/", include("inventory.urls")),
    path("api/v1/pricing/", include("pricing.urls")),
    path("api/v1/documents/", include("documents.urls")),
    path("api/v1/content/", include("content.urls")),
    # CRM reports as JSON (CRM Phase 6 step 7); before crm.urls, which owns api/v1/crm/.
    path("api/v1/crm/reports/", include("reporting.api_urls")),
    path("api/v1/crm/", include("crm.urls")),
    path("api/v1/communications/", include("communications.urls")),
    path("api/v1/parties/", include("parties.urls")),
    path("api/v1/reporting/", include("reporting.urls")),
    path("api/v1/discounts/", include("discounts.urls")),
    path("api/v1/accounting/", include("accounting.urls")),
    path("api/v1/webhooks/", include("webhooks.urls")),
    # Inbound provider webhooks for Connected accounts (docs/crm/phase6-adapters.md 2.8).
    path("api/v1/integrations/<str:provider>/<uuid:account_id>/webhook/", integration_webhook,
         name="integration-webhook"),
    path("api/v1/portal/", include("core.portal_urls")),
    path("api/v1/portal/customer/", include("portal.urls")),
    # Erasure (parties step 12); the rest of /api/v1/parties/ is parties.urls (step 8).
    path("api/v1/parties/", include("parties.erasure_urls")),

    # REST APIs (unversioned aliases)
    path("api/core/", include("core.urls")),
    path("api/products/", include("products.urls")),
    path("api/customers/", include("customers.urls")),
    path("api/orders/", include("orders.urls")),
    path("api/payments/", include("payments.urls")),
    path("api/purchasing/", include("purchasing.urls")),
    path("api/fulfillment/", include("fulfillment.urls")),
    path("api/inventory/", include("inventory.urls")),
    path("api/pricing/", include("pricing.urls")),
    path("api/documents/", include("documents.urls")),
    path("api/content/", include("content.urls")),
    path("api/crm/reports/", include("reporting.api_urls")),
    path("api/crm/", include("crm.urls")),
    path("api/communications/", include("communications.urls")),
    path("api/parties/", include("parties.urls")),
    path("api/reporting/", include("reporting.urls")),
    path("api/discounts/", include("discounts.urls")),
    path("api/accounting/", include("accounting.urls")),
    path("api/webhooks/", include("webhooks.urls")),
    path("api/portal/", include("core.portal_urls")),
    path("api/portal/customer/", include("portal.urls")),
    path("api/parties/", include("parties.erasure_urls")),
]

# Plugin URLs (BasePlugin.get_urls) mount LAST: Django resolves first-match,
# so a plugin can never shadow a core route; reserved= also refuses a
# plugin URL name that a core route already uses. Staff-gated unless the plugin
# declares url_auth = "public"; a path claimed by two plugins fails startup.
urlpatterns += plugin_registry.get_urls(reserved=urlpatterns)
