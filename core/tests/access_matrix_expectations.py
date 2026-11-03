"""Expectations for the access matrix (``core/tests/test_access_matrix.py``).

This file is the *independent* source of truth the matrix checks the code
against (docs/release-hardening-plan.md section 6.1): nothing here is read
from a permission class, a mixin or a middleware. A mismatch is a finding
against one of three things, each cited in the failure message:

1. :data:`ROLES_MD` -- ``docs/roles.md`` "Built-in roles" and "Adding roles in
   a deployment", encoded once. A role's ``staff`` / ``portal`` / ``kind`` /
   ``permissions`` keys are what that page says they mean.
2. :data:`MODEL_OVERRIDES` -- one entry per model behind an API viewset or an
   ``/erp/`` model view. ``{}`` means the roles.md default; an entry refines
   it per action where roles.md is too coarse, says which seeded row to use,
   or marks a skip or a known leak.
3. :data:`ROUTE_CLASSES` -- one entry per route that is neither of those
   (function views, model-less class views, the auth, portal and token
   routes, plugin routes). An unlisted route fails the test.

How to read a spec string
-------------------------

``"<class>[;leak:<principal>:<F-id>][;skip:<reason>]"`` where ``<class>`` is:

``public``
    nobody is refused for who they are (login, logout, password reset, the
    storefront catalog reads, token-gated links).
``staff``
    back-office access: a superuser, or a member of any ``staff=True`` role.
    On the API a service account is held to its role's declared model
    permissions instead (``view_`` / ``add_`` / ``change_`` / ``delete_``;
    custom actions and model-less views are never in a contract; writes need
    a pinned company, except the storefront identity, which may only ``add``):
    docs/identity-unification.md section 2.6 "As built: step 5".
``superuser``
    break-glass only (docs/roles.md "``is_superuser`` is independent").
``perm:<app_label.codename>``
    a superuser, or a member of a role that *declares* that permission in
    ``GOLDEN_ROLES`` (docs/roles.md: "``permissions`` ... added to the group").
``portal``
    a portal customer (a ``portal=True`` role) and nobody else, staff and
    superusers included: the portal has its own authentication.

``human`` refuses every machine principal (service accounts and the legacy
key) whatever the class says: the action attributes a change to a person
(``crm.api`` reopen: "a service principal cannot reopen").

``leak:<principal>:<F-id>`` records a known gap from ``docs/findings-log.md``:
the principal is *observed* to pass although the class refuses it. The matrix
then expects the leak, so a gap that stops leaking fails too, until the entry
is removed. ``skip:<reason>`` leaves the row out and is counted as a skip.

A ``MODEL_OVERRIDES`` entry may hold, besides ``api:<action>`` and
``erp:<action>`` specs, ``"api"`` / ``"erp"`` (a default for every action on
that surface) and ``"row"`` (``"seed"`` when ``test_access_matrix.seed``
creates one, else ``"skip:<reason>"``; detail actions of a model without a
row are skipped). ``ROUTE_CLASSES`` values are a spec string, or a dict
``{"get": spec, "post": spec}`` when the methods differ. ``ROUTE_OPTIONS``
tunes a route: ``methods`` (restrict, e.g. to keep a POST that would call a
provider off the matrix), ``params`` (fixed URL parameter values), ``row``
(``{"pk": "app.Model"}``: a seeded row for a parameter, when a placeholder
would 404 or raise before the gate that the class names; other parameters get
a placeholder of the converter's type) and ``fresh_client`` (the
route ends the session, so the principal's client is rebuilt afterwards).

Cross-company isolation is deliberately not in this matrix (decision 2):
one instance per company is the design and F-026/027/028 are S2 limitations,
not release criteria.
"""

from __future__ import annotations

# --- 1. docs/roles.md, encoded once -------------------------------------------

#: docs/roles.md "Built-in roles" (the Role / Kind / Meaning table) and
#: "Adding roles in a deployment" (what ``staff``, ``portal`` and ``kind`` mean).
#: The matrix derives every principal's reach from these three meanings; the
#: built-in rows are listed so a change to that table is a change here.
ROLES_MD = {
    "meanings": {
        # key in GOLDEN_ROLES -> surface it grants (docs/roles.md "Adding roles")
        "staff": "back office and the staff API",
        "portal": "the customer portal API",
        "service": "the staff API, exactly the role's declared model permissions",
    },
    "builtin": {
        "staff": {"staff": True, "permissions": ("parties.export_pii",)},
        "customer": {"portal": True},
        "storefront": {"kind": "service"},
    },
    "superuser": "independent break-glass: passes every staff gate",
}

#: Viewset action -> model-permission verb for a service account's contract
#: (docs/identity-unification.md 2.6). Anything else is outside any contract.
CONTRACT_VERBS = {
    "list": "view", "retrieve": "view", "export": "view",
    "create": "add", "update": "change", "partial_update": "change", "destroy": "delete",
}

# --- 2. per-model overrides ----------------------------------------------------

#: One entry per model behind an API viewset or an ``/erp/`` model view.
MODEL_OVERRIDES = {
    # core
    "core.Company": {"row": "seed"},
    "core.UserProfile": {"row": "seed"},
    # core.TeamViewSet: edits need core.manage_teams (docs/crm/phase4-fields.md 2.1)
    "core.Team": {
        "row": "seed", "api:create": "perm:core.manage_teams",
        "api:update": "perm:core.manage_teams", "api:partial_update": "perm:core.manage_teams",
        "api:destroy": "perm:core.manage_teams",
    },
    "core.AuditLog": {"row": "seed"},
    # products: the storefront catalog reads are public by design
    # (docs/testing-plan.md section 4, pinned decision; core/tests/test_auth_matrix.py).
    "products.Product": {"row": "seed", "api:list": "public", "api:retrieve": "public"},
    "products.ProductVariant": {"row": "seed"},
    # Brand/Category reads are public too: the storefront's brand and category
    # pages (docs/storefront-seo.md). Non-staff readers get visible rows only,
    # through the public serializer; writes keep the staff/contract default.
    "products.Brand": {"row": "seed", "api:list": "public", "api:retrieve": "public"},
    "products.Category": {"row": "seed", "api:list": "public", "api:retrieve": "public"},
    "products.SupplierProduct": {"row": "seed"},
    "products.ProductOption": {"row": "seed"},
    "products.ProductOptionValue": {"row": "seed"},
    "products.VariantOptionValue": {"row": "seed"},
    # customers / parties
    "customers.Customer": {"row": "seed"},
    "customers.CustomerAddress": {"row": "seed"},
    "parties.Party": {"row": "seed"},
    "parties.PartyEmail": {"row": "seed"},
    "parties.PartyPhone": {"row": "seed"},
    "parties.PartyAddress": {"row": "seed"},
    "parties.PartyRole": {"row": "seed"},
    "parties.PartyRelationship": {"row": "seed"},
    "parties.ConsentRecord": {"row": "seed"},
    # orders / payments
    "orders.Quote": {"row": "seed"},
    "orders.QuoteLineItem": {"row": "seed"},
    "orders.Order": {"row": "seed"},
    "orders.OrderLineItem": {"row": "seed"},
    "orders.SubOrder": {"row": "seed"},
    "orders.SalesInvoice": {"row": "seed"},
    "payments.Payment": {"row": "seed"},
    # purchasing / fulfillment / inventory / pricing
    "purchasing.Supplier": {"row": "seed"},
    "purchasing.PurchaseOrder": {"row": "seed"},
    "purchasing.POLineItem": {"row": "seed"},
    "purchasing.POReceipt": {"row": "seed"},
    "fulfillment.PickTicket": {"row": "seed"},
    "fulfillment.PickTicketLine": {"row": "seed"},
    "inventory.Warehouse": {"row": "seed"},
    "inventory.WarehouseStock": {"row": "seed"},
    "pricing.PriceList": {"row": "seed"},
    "pricing.PriceListItem": {"row": "seed"},
    "pricing.TaxRate": {"row": "seed"},
    # documents / content / discounts / reporting / accounting / webhooks
    "documents.ProductDocument": {"row": "seed"},
    "content.Article": {"row": "seed"},
    "discounts.DiscountCode": {"row": "seed"},
    "reporting.ReportDefinition": {"row": "seed"},
    "accounting.FiscalPeriod": {"row": "seed"},
    "webhooks.WebhookSubscription": {
        "row": "seed",
        "api:ping": "staff;skip:ping delivers synchronously to the target URL (no network in "
                    "the suite); webhooks/tests cover it",
    },
    "webhooks.WebhookDeliveryLog": {"row": "seed"},
    # crm / communications
    "crm.Lead": {"row": "seed"},
    # docs/crm/phase3-engagement.md: pipeline admin needs crm.manage_pipelines,
    # reopening a deal crm.reopen_engagement.
    "crm.Pipeline": {
        "row": "seed", "api:create": "perm:crm.manage_pipelines",
        "api:update": "perm:crm.manage_pipelines", "api:partial_update": "perm:crm.manage_pipelines",
        "api:destroy": "perm:crm.manage_pipelines",
    },
    "crm.Stage": {
        "row": "seed", "api:create": "perm:crm.manage_pipelines",
        "api:update": "perm:crm.manage_pipelines", "api:partial_update": "perm:crm.manage_pipelines",
        "api:destroy": "perm:crm.manage_pipelines",
    },
    "crm.Engagement": {"row": "seed", "api:reopen": "perm:crm.reopen_engagement;human"},
    "crm.EngagementContact": {"row": "seed"},
    "crm.EngagementStageChange": {"row": "seed"},
    "crm.Task": {"row": "seed"},
    # docs/crm/phase6-adapters.md: recording a visit is a declared grant (a tag
    # manager's key), raw provider payloads need communications.view_raw_communication.
    "crm.Touchpoint": {"row": "seed", "api:create": "perm:crm.add_touchpoint"},
    "communications.Communication": {
        "row": "seed", "api:raw": "perm:communications.view_raw_communication"},
    "crm_import.ImportJob": {"row": "seed"},
}

# --- 3. every other route --------------------------------------------------------

_PUBLIC_AUTH = "public"  # docs/roles.md has no row for these: anyone may sign in or out

ROUTE_CLASSES = {
    # root + API plumbing
    "root-redirect": "public",
    "schema": "public",  # OpenAPI document (drf-spectacular default SERVE_PERMISSIONS)
    "swagger-ui": "public",
    "redoc": "public",
    "api-root": (
        "staff;skip:DRF binds DEFAULT_PERMISSION_CLASSES at import, and the dev/test "
        "settings keep AllowAny (F-003); the packaged default is PrincipalPermission, "
        "pinned by test_auth_matrix.test_settings_default_permission_is_principal"
    ),
    "api-token-obtain": "public;skip:credential exchange, covered by test_auth_matrix and test_token_scopes",
    "api-storefront-token": "public;skip:credential exchange, covered by test_auth_matrix and test_storefront_identity",
    # token-gated payment link: the token is the credential (orders.views)
    "sales-invoice-by-token": "public",
    "sales-invoice-reconcile": "staff",
    # inbound provider webhooks verify their own signature (docs/crm/phase6-adapters.md 2.8)
    "integration-webhook": "public",
    # staff API views without a viewset model
    "api-crm-reports": "staff",
    "api-crm-report-funnel": "staff",
    "api-crm-report-stage-duration": "staff",
    "api-crm-report-follow-ups": "staff",
    "api-crm-report-attribution": "staff",
    "activitylog-list": "staff",
    "activitylog-detail": "staff",
    "party-match-list": "staff",
    "api-discount-validate": "staff",
    "api-discount-redeem": "staff",
    "party-erase": "perm:parties.erase_party",
    "portal-invite": "staff",
    "portal-invite-resend": "staff",
    # customer portal (docs/roles.md: ``customer`` "may use the customer portal API")
    "portal-profile": "portal",
    "portal-address-list": "portal",
    "portal-address-detail": "portal",
    "portal-contact-list": "portal",
    "portal-order-list": "portal",
    "portal-order-detail": "portal",
    "portal-quote-list": "portal",
    "portal-quote-detail": "portal",
    "portal-invoice-list": "portal",
    "portal-document-list": "portal",
    "portal-activity": "portal",
    "portal-me": "portal",
    "portal-me-consents": "portal",
    "portal-module-counts": "portal",
    # /erp/ auth routes (StaffOnlyERPMiddleware docstring: open so anyone can sign out)
    "erp-login": _PUBLIC_AUTH,
    "erp-logout": _PUBLIC_AUTH,
    "erp-password-reset": _PUBLIC_AUTH,
    "erp-password-reset-done": _PUBLIC_AUTH,
    "erp-password-reset-confirm": _PUBLIC_AUTH,
    "erp-password-reset-complete": _PUBLIC_AUTH,
    "erp-oidc-start": _PUBLIC_AUTH,
    "erp-oidc-callback": _PUBLIC_AUTH,
    "erp-verify-email": "public",
    "erp-sso-storefront": "staff",
    # /erp/ core
    "erp-dashboard": "staff",
    "erp-dashboard-quick-actions": "staff",
    "erp-account": "staff",
    "erp-account-edit": "staff",
    "erp-account-change-password": "staff",
    "erp-user-list": "staff",
    "erp-user-create": "staff",
    "erp-user-toggle-active": "staff",
    "erp-settings-teams": "staff",
    "erp-settings-team-create": "perm:core.manage_teams",
    "erp-settings-team-edit": "perm:core.manage_teams",
    "erp-integrations": "staff",
    "erp-integration-account-create": "perm:core.change_integrationaccount",
    "erp-integration-account-update": "perm:core.change_integrationaccount",
    "erp-integration-account-credential": "perm:core.change_integrationaccount",
    "erp-integration-account-test": "perm:core.change_integrationaccount",
    "erp-integration-oauth-connect": "staff",
    "erp-integration-oauth-callback": "staff",
    # disconnect: your own grant, or anyone's with the change permission; the
    # adapter lookup (404 without one installed) comes first, so the matrix
    # can only see the staff gate.
    "erp-integration-oauth-disconnect": "staff",
    "erp-integration-test": "staff",
    # /erp/ orders
    "erp-order-send-email": "staff",
    "erp-suborder-send-shipping-email": "staff",
    "erp-suborder-update-tracking": "staff",
    "erp-shipping-rates": "staff",
    "erp-quote-detail": "staff",
    "erp-quote-pdf": "staff",
    "erp-quote-send": "staff",
    # /erp/ products
    "erp-product-create": "staff",
    "erp-product-detail": "staff",
    "erp-variant-lookup": "staff",
    "erp-variant-search": "staff",
    "erp-supplier-cost-create": "staff",
    "erp-supplier-cost-detail": "staff",
    "erp-supplier-cost-update": "staff",
    "erp-supplier-cost-delete": "staff",
    # /erp/ customers
    "erp-customer-address-delete": "staff",
    "erp-address-create-ajax": "staff",
    "erp-address-detail-ajax": "staff",
    "erp-address-update-ajax": "staff",
    "erp-customer-create-ajax": "staff",
    "erp-customer-search-ajax": "staff",
    "erp-address-search-ajax": "staff",
    "erp-customer-defaults-ajax": "staff",
    "erp-contact-search-ajax": "staff",
    "erp-contact-create-ajax": "staff",
    "erp-contact-detail-ajax": "staff",
    "erp-contact-update-ajax": "staff",
    # /erp/ parties
    "erp-party-search": "staff",
    "erp-party-erase": "perm:parties.erase_party",
    "erp-party-merge-review": {"get": "staff", "post": "perm:parties.merge_party"},
    # /erp/ crm
    "erp-crm-my-day": "staff",
    "erp-crm-engagement-create": "staff",
    "erp-crm-engagement-bulk": "staff",
    "erp-crm-engagement-edit": "staff",
    "erp-crm-engagement-timeline": "staff",
    "erp-crm-engagement-move": "staff",
    "erp-crm-engagement-win": "staff",
    "erp-crm-engagement-lose": "staff",
    "erp-crm-engagement-reopen": "staff",
    "erp-crm-engagement-close-remaining": "staff",
    "erp-crm-engagement-task-create": "staff",
    "erp-crm-engagement-contact-create": "staff",
    "erp-crm-engagement-log": "staff",
    "erp-crm-contact-remove": "staff",
    "erp-crm-task-create": "staff",
    "erp-crm-task-complete": "staff",
    "erp-crm-task-reopen": "staff",
    "erp-crm-task-cancel": "staff",
    "erp-crm-task-reschedule": "staff",
    "erp-crm-task-reassign": "staff",
    "erp-crm-lead-create": "staff",
    "erp-crm-pipeline-list": "staff",
    "erp-crm-pipeline-create": "perm:crm.manage_pipelines",
    "erp-crm-pipeline-edit": "perm:crm.manage_pipelines",
    "erp-crm-stage-create": "perm:crm.manage_pipelines",
    "erp-crm-stage-edit": "perm:crm.manage_pipelines",
    "erp-crm-stage-reorder": "perm:crm.manage_pipelines",
    # /erp/ purchasing, fulfillment, inventory, payments
    "erp-supplier-search-ajax": "staff",
    "erp-supplier-ship-from": "staff",
    "erp-pickticket-create-ajax": "staff",
    "erp-warehouse-search-ajax": "staff",
    "erp-invoice-bulk-pdf": "staff",
    "erp-invoice-detail": "staff",
    "erp-invoice-pdf": "staff",
    "erp-invoice-send": "staff",
    # /erp/ reports, discounts, content, accounting
    "erp-report-list": "staff",
    "erp-report-crm-funnel": "staff",
    "erp-report-crm-stages": "staff",
    "erp-report-crm-follow-ups": "staff",
    "erp-report-crm-attribution": "staff",
    "erp-discount-generate": "staff",
    "erp-article-delete": "staff",
    "erp-accounting-dashboard": "staff",
    "erp-accounting-trial-balance": "staff",
    "erp-accounting-account-register": "staff",
    "erp-accounting-entries": "staff",
    "erp-accounting-entry-detail": "staff",
    "erp-accounting-import": "staff",
    "erp-accounting-periods": "staff",
    "erp-accounting-reconciliation": "staff",
    # /erp/ imports
    "erp-crm-import-report-old": (
        "staff;skip:a bare RedirectView to erp-crm-import-report (which is gated); it "
        "redirects anonymous visitors and the middleware refuses other sessions"),
    "erp-crm-import-job": "staff",
    "erp-crm-import-report": "staff",
    "erp-crm-import-progress": "staff",
    "erp-crm-import-action": "staff",
    "erp-crm-import-queue": "staff",
    "erp-crm-import-bulk": "staff",
    "erp-crm-import-decide": "staff",
    "erp-unify-batch-list": "staff",
    "erp-unify-batch-upload": "staff",
    "erp-unify-batch-detail": "staff",
    # /erp/ llm settings and chat
    "erp-llm-settings": "staff",
    "erp-llm-connection": "staff",
    "erp-llm-lanes": "staff",
    "erp-llm-model-add": "staff",
    "erp-llm-model-edit": "staff",
    "erp-llm-model-action": "staff",
    "erp-llm-test": "staff",
    "erp-llm-models-refresh": "staff",
    "chat:sessions": "staff",
    "chat:session_new": "staff",
    "chat:session_detail": "staff",
    "chat:stream": "staff",
    # hardening R1-R11 fix screens
    "erp-invoice-preview": "staff",
    "erp-invoice-record-payment": "staff",
    "erp-invoice-refund": "staff",
    "erp-purchaseorder-receive": "staff",
    "erp-quote-engagement-options": "staff",
    "erp-quote-revise": "staff",
    "erp-settings-labels": {"get": "staff", "post": "perm:core.change_labeloverride"},
    "erp-user-roles": "perm:auth.change_user",
    "erp-webhook-subscription-create": "perm:core.change_integrationaccount",
    "erp-webhook-subscription-ping": "perm:core.change_integrationaccount",
    "erp-webhook-subscription-toggle": "perm:core.change_integrationaccount",
}

#: Per-route tuning (see the module docstring).
ROUTE_OPTIONS = {
    "erp-logout": {"methods": ("post",), "fresh_client": True},
    # POSTs that would call a provider or carrier (the suite has no network):
    # the gate still runs on GET (405 after it for an allowed principal).
    "erp-integration-account-test": {"row": {"pk": "core.IntegrationAccount"}},
    "erp-integration-test": {"methods": ("get",)},
    "erp-shipping-rates": {"methods": ("get",)},
    "erp-llm-test": {"methods": ("get",)},
    "erp-llm-models-refresh": {"methods": ("get",)},
    "erp-llm-connection": {"methods": ("get",)},
    "erp-webhook-subscription-ping": {"methods": ("get",)},
    # a perm: class is checked after the row lookup, so bind real rows
    "erp-integration-account-update": {"row": {"pk": "core.IntegrationAccount"}},
    "erp-integration-account-credential": {"row": {"pk": "core.IntegrationAccount"}},
    "erp-integration-oauth-connect": {"row": {"pk": "core.IntegrationAccount"}},
    "erp-integration-oauth-callback": {"row": {"pk": "core.IntegrationAccount"}},
    "erp-integration-oauth-disconnect": {"row": {"pk": "core.IntegrationAccount"}},
    "erp-settings-team-edit": {"row": {"pk": "core.Team"}},
    "erp-party-erase": {"row": {"pk": "parties.Party"}},
    "party-erase": {"row": {"pk": "parties.Party"}},
    # views that .get() their row (unknown id raises): bind seeded rows
    "erp-product-detail": {"row": {"pk": "products.Product"}},
    "erp-supplier-cost-detail": {"row": {"pk": "products.SupplierProduct"}},
    "erp-supplier-cost-update": {"row": {"pk": "products.SupplierProduct"}},
    "erp-supplier-cost-delete": {"row": {"pk": "products.SupplierProduct"}},
    "erp-crm-import-queue": {"params": {"object_type": "contacts"}},
    "erp-crm-import-bulk": {"params": {"object_type": "contacts"}},
    "erp-crm-import-decide": {"params": {"object_type": "contacts"}},
    "integration-webhook": {"methods": ("post",)},
}
