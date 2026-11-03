"""
URL routes for the customer portal admin API.
"""

from django.urls import path

from .portal_views import PortalInviteResendView, PortalInviteView

urlpatterns = [
    path("invite/", PortalInviteView.as_view(), name="portal-invite"),
    path(
        "invite/resend/",
        PortalInviteResendView.as_view(),
        name="portal-invite-resend",
    ),
]
