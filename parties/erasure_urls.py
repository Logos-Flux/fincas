"""The erasure route under ``/api/v1/parties/`` (step 12; see ``parties.erasure_api``)."""

from django.urls import path

from .erasure_api import PartyEraseView

urlpatterns = [
    path("parties/<uuid:pk>/erase/", PartyEraseView.as_view(), name="party-erase"),
]
