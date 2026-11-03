from django.urls import path

from .views import (
    ChatStreamView,
    NewSessionView,
    SessionDetailView,
    SessionsListView,
)

app_name = "chat"

urlpatterns = [
    path("sessions/", SessionsListView.as_view(), name="sessions"),
    path("sessions/new/", NewSessionView.as_view(), name="session_new"),
    path("sessions/<uuid:pk>/", SessionDetailView.as_view(), name="session_detail"),
    path("stream/", ChatStreamView.as_view(), name="stream"),
]
