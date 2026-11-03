from django.urls import path

from . import erp_views

urlpatterns = [
    path("", erp_views.BatchListView.as_view(), name="erp-unify-batch-list"),
    path(
        "upload/",
        erp_views.BatchUploadView.as_view(),
        name="erp-unify-batch-upload",
    ),
    path(
        "<uuid:pk>/",
        erp_views.BatchDetailView.as_view(),
        name="erp-unify-batch-detail",
    ),
]
