from django.contrib.auth.views import (
    LogoutView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.urls import include, path

from .integrations import views as account_views
from .oidc_views import oidc_callback, oidc_start
from .sso_views import storefront_staff_sso
from .throttling import ThrottledLoginView
from .views.account import (
    AccountEditView,
    AccountPasswordChangeView,
    AccountView,
    EmailVerifyView,
    UserCreateView,
    UserListView,
    UserRolesView,
    UserToggleActiveView,
)
from .views.dashboard import DashboardQuickActionsView, DashboardView
from .views.integrations import IntegrationsView, IntegrationTestView
from .views.labels import LabelsView
from .views.teams import TeamCreateView, TeamListView, TeamUpdateView

urlpatterns = [
    # Staff SSO handoff to the storefront admin
    path("sso/storefront/", storefront_staff_sso, name="erp-sso-storefront"),

    # Auth
    path("login/", ThrottledLoginView.as_view(template_name="registration/login.html"), name="erp-login"),
    path("oidc/<slug:provider_name>/start/", oidc_start, name="erp-oidc-start"),
    path("oidc/<slug:provider_name>/callback/", oidc_callback, name="erp-oidc-callback"),
    path("logout/", LogoutView.as_view(next_page="/erp/login/"), name="erp-logout"),

    # Password reset
    path("password-reset/", PasswordResetView.as_view(
        template_name="registration/password_reset.html",
        email_template_name="registration/password_reset_email.html",
        subject_template_name="registration/password_reset_subject.txt",
        success_url="/erp/password-reset/done/",
    ), name="erp-password-reset"),
    path("password-reset/done/", PasswordResetDoneView.as_view(
        template_name="registration/password_reset_done.html",
    ), name="erp-password-reset-done"),
    path("password-reset/<uidb64>/<token>/", PasswordResetConfirmView.as_view(
        template_name="registration/password_reset_confirm.html",
        success_url="/erp/password-reset/complete/",
    ), name="erp-password-reset-confirm"),
    path("password-reset/complete/", PasswordResetCompleteView.as_view(
        template_name="registration/password_reset_complete.html",
    ), name="erp-password-reset-complete"),

    # Dashboard
    path("", DashboardView.as_view(), name="erp-dashboard"),
    path(
        "dashboard/quick-actions/", DashboardQuickActionsView.as_view(),
        name="erp-dashboard-quick-actions",
    ),

    # Account
    path("account/", AccountView.as_view(), name="erp-account"),
    path("account/edit/", AccountEditView.as_view(), name="erp-account-edit"),
    path("account/change-password/", AccountPasswordChangeView.as_view(), name="erp-account-change-password"),

    # User management (staff only)
    path("users/", UserListView.as_view(), name="erp-user-list"),
    path("users/create/", UserCreateView.as_view(), name="erp-user-create"),
    path("users/<int:pk>/toggle-active/", UserToggleActiveView.as_view(), name="erp-user-toggle-active"),
    path("users/<int:pk>/roles/", UserRolesView.as_view(), name="erp-user-roles"),

    # Owner teams (phase4-fields.md 2.1; edits need core.manage_teams)
    path("settings/teams/", TeamListView.as_view(), name="erp-settings-teams"),
    path("settings/teams/new/", TeamCreateView.as_view(), name="erp-settings-team-create"),
    path("settings/teams/<uuid:pk>/edit/", TeamUpdateView.as_view(), name="erp-settings-team-edit"),
    # Tenant label rewording (docs/labels.md; edits need core.change_labeloverride)
    path("settings/labels/", LabelsView.as_view(), name="erp-settings-labels"),

    # External-service inventory + connection tests (staff only)
    path("integrations/", IntegrationsView.as_view(), name="erp-integrations"),
    # Connected accounts (docs/crm/phase6-adapters.md 2.7), ahead of <str:target>/test/.
    path("integrations/accounts/new/", account_views.AccountCreateView.as_view(),
         name="erp-integration-account-create"),
    path("integrations/accounts/<uuid:pk>/", account_views.AccountUpdateView.as_view(),
         name="erp-integration-account-update"),
    path("integrations/accounts/<uuid:pk>/credentials/", account_views.CredentialSetView.as_view(),
         name="erp-integration-account-credential"),
    path("integrations/accounts/<uuid:pk>/test/", account_views.AccountTestView.as_view(),
         name="erp-integration-account-test"),
    path("integrations/accounts/<uuid:pk>/connect/", account_views.OAuthConnectView.as_view(),
         name="erp-integration-oauth-connect"),
    path("integrations/accounts/<uuid:pk>/callback/", account_views.OAuthCallbackView.as_view(),
         name="erp-integration-oauth-callback"),
    path("integrations/accounts/<uuid:pk>/disconnect/", account_views.OAuthDisconnectView.as_view(),
         name="erp-integration-oauth-disconnect"),
    # Outbound webhook subscriptions (docs/webhooks.md), ahead of <str:target>/test/.
    path("integrations/webhooks/", include("webhooks.erp_urls")),
    path("integrations/<str:target>/test/", IntegrationTestView.as_view(), name="erp-integration-test"),

    # Email verification (public)
    path("verify-email/<str:token>/", EmailVerifyView.as_view(), name="erp-verify-email"),

    # App routes
    path("orders/", include("orders.erp_urls")),
    path("products/", include("products.erp_urls")),
    path("customers/", include("customers.erp_urls")),
    path("parties/", include("parties.erp_urls")),
    path("crm/", include("crm.erp_urls")),
    path("purchasing/", include("purchasing.erp_urls")),
    path("fulfillment/", include("fulfillment.erp_urls")),
    path("inventory/", include("inventory.erp_urls")),
    path("pricing/", include("pricing.erp_urls")),
    path("payments/", include("payments.erp_urls")),
    path("documents/", include("documents.erp_urls")),
    path("reports/", include("reporting.erp_urls")),
    path("discounts/", include("discounts.erp_urls")),
    path("content/", include("content.erp_urls")),
    path("accounting/", include("accounting.erp_urls")),
    # CRM import report (Phase 5 step 8): a job id, never a batch id; ahead of unify's.
    path("imports/", include("crm_import.erp_urls")),
    path("imports/", include("unify.erp_urls")),
    path("llm/", include("llmhub.erp_urls")),
    path("chat/", include("chat.urls", namespace="chat")),
]
