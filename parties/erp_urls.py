"""``/erp/parties/`` routes (docs/crm/phase2-party.md 6.1, 6.2; step 9).

Mounted by ``core/erp_urls.py``. ``merge-review/`` (6.3) is step 6's.
"""

from django.urls import path

from . import erp_views as v

urlpatterns = [
    path("", v.PartyListView.as_view(), name="erp-party-list"),
    path("new/", v.PartyCreateView.as_view(), name="erp-party-create"),
    path("search/", v.PartySearchView.as_view(), name="erp-party-search"),
    path("<uuid:pk>/", v.PartyDetailView.as_view(), name="erp-party-detail"),
    path("<uuid:pk>/edit/", v.PartyUpdateView.as_view(), name="erp-party-edit"),
    path("<uuid:pk>/erase/", v.PartyEraseView.as_view(), name="erp-party-erase"),
    # Contact points
    path("<uuid:party_pk>/emails/new/", v.PartyEmailCreateView.as_view(), name="erp-party-email-create"),
    path("emails/<uuid:pk>/edit/", v.PartyEmailUpdateView.as_view(), name="erp-party-email-edit"),
    path("emails/<uuid:pk>/delete/", v.PartyEmailDeleteView.as_view(), name="erp-party-email-delete"),
    path("<uuid:party_pk>/phones/new/", v.PartyPhoneCreateView.as_view(), name="erp-party-phone-create"),
    path("phones/<uuid:pk>/edit/", v.PartyPhoneUpdateView.as_view(), name="erp-party-phone-edit"),
    path("phones/<uuid:pk>/delete/", v.PartyPhoneDeleteView.as_view(), name="erp-party-phone-delete"),
    path("<uuid:party_pk>/addresses/new/", v.PartyAddressCreateView.as_view(),
         name="erp-party-address-create"),
    path("addresses/<uuid:pk>/edit/", v.PartyAddressUpdateView.as_view(), name="erp-party-address-edit"),
    path("addresses/<uuid:pk>/delete/", v.PartyAddressDeleteView.as_view(),
         name="erp-party-address-delete"),
    # Roles, relationships, consent
    path("<uuid:party_pk>/roles/new/", v.PartyRoleCreateView.as_view(), name="erp-party-role-create"),
    path("roles/<uuid:pk>/edit/", v.PartyRoleUpdateView.as_view(), name="erp-party-role-edit"),
    path("<uuid:party_pk>/relationships/new/", v.PartyRelationshipCreateView.as_view(),
         name="erp-party-relationship-create"),
    path("relationships/<uuid:pk>/edit/", v.PartyRelationshipUpdateView.as_view(),
         name="erp-party-relationship-edit"),
    path("<uuid:party_pk>/consents/new/", v.ConsentRecordCreateView.as_view(),
         name="erp-party-consent-create"),
]
