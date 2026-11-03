from rest_framework import viewsets

from core.permissions import PrincipalPermission

from .models import Article
from .serializers import ArticleDetailSerializer, ArticleListSerializer


class ArticleViewSet(viewsets.ModelViewSet):
    # Global CMS content (no company FK) by design. Explicit auth so
    # prod/test/dev agree (F-003) + storefront gate.
    permission_classes = [PrincipalPermission]
    queryset = Article.objects.all()
    filterset_fields = ["status", "author"]
    search_fields = ["title", "slug"]
    ordering_fields = ["published_at", "created_at"]

    def get_serializer_class(self):
        if self.action == "list":
            return ArticleListSerializer
        return ArticleDetailSerializer
