from django.urls import path

from .views import (
    PortalActivityView,
    PortalAddressDetailView,
    PortalAddressListView,
    PortalConsentView,
    PortalContactListView,
    PortalDocumentListView,
    PortalInvoiceListView,
    PortalMeView,
    PortalModuleCountsView,
    PortalOrderDetailView,
    PortalOrderListView,
    PortalProfileView,
    PortalQuoteDetailView,
    PortalQuoteListView,
)

urlpatterns = [
    path("profile/", PortalProfileView.as_view(), name="portal-profile"),
    path("addresses/", PortalAddressListView.as_view(), name="portal-address-list"),
    path("addresses/<uuid:pk>/", PortalAddressDetailView.as_view(), name="portal-address-detail"),
    path("contacts/", PortalContactListView.as_view(), name="portal-contact-list"),
    path("orders/", PortalOrderListView.as_view(), name="portal-order-list"),
    path("orders/<uuid:pk>/", PortalOrderDetailView.as_view(), name="portal-order-detail"),
    path("quotes/", PortalQuoteListView.as_view(), name="portal-quote-list"),
    path("quotes/<uuid:pk>/", PortalQuoteDetailView.as_view(), name="portal-quote-detail"),
    path("invoices/", PortalInvoiceListView.as_view(), name="portal-invoice-list"),
    path("documents/", PortalDocumentListView.as_view(), name="portal-document-list"),
    path("activity/", PortalActivityView.as_view(), name="portal-activity"),
    # CRM Phase 2 step 7 (docs/crm/phase2-party.md 5.3): the customer's own party.
    path("me/", PortalMeView.as_view(), name="portal-me"),
    path("me/consents/", PortalConsentView.as_view(), name="portal-me-consents"),
    path("module-counts/", PortalModuleCountsView.as_view(), name="portal-module-counts"),
]
