from django.urls import path

from .erp_views import (
    CreatePickTicketAjaxView,
    PickTicketCreateView,
    PickTicketListView,
    PickTicketPrintView,
    PickTicketUpdateView,
)

urlpatterns = [
    path("", PickTicketListView.as_view(), name="erp-pickticket-list"),
    path("new/", PickTicketCreateView.as_view(), name="erp-pickticket-create"),
    path("create-ajax/", CreatePickTicketAjaxView.as_view(), name="erp-pickticket-create-ajax"),
    path("<uuid:pk>/edit/", PickTicketUpdateView.as_view(), name="erp-pickticket-edit"),
    path("<uuid:pk>/print/", PickTicketPrintView.as_view(), name="erp-pickticket-print"),
]
