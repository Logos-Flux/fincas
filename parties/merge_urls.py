"""``/erp/parties/merge-review/`` (phase2-party.md 6.3), mounted from ``golden_erp.urls``."""

from django.urls import path

from . import merge_views

urlpatterns = [
    path("", merge_views.MergeReviewView.as_view(), name="erp-party-merge-review"),
]
