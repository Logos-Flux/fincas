from django.urls import path

from .erp_views import DocumentLibraryView

urlpatterns = [
    path("", DocumentLibraryView.as_view(), name="erp-document-library"),
]
