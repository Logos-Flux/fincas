from django.urls import path

from .erp_views import (
    ArticleCreateView,
    ArticleDeleteView,
    ArticleDetailView,
    ArticleListView,
    ArticleUpdateView,
)

urlpatterns = [
    path("", ArticleListView.as_view(), name="erp-article-list"),
    path("new/", ArticleCreateView.as_view(), name="erp-article-create"),
    path("<uuid:pk>/", ArticleDetailView.as_view(), name="erp-article-detail"),
    path("<uuid:pk>/edit/", ArticleUpdateView.as_view(), name="erp-article-edit"),
    path("<uuid:pk>/delete/", ArticleDeleteView.as_view(), name="erp-article-delete"),
]
