from core.managers import scope_to_company
from core.views import CompanyScopedViewSet

from .models import ProductDocument
from .serializers import ProductDocumentSerializer


class ProductDocumentViewSet(CompanyScopedViewSet):
    queryset = ProductDocument.objects.select_related("product")
    serializer_class = ProductDocumentSerializer
    filterset_fields = ["product", "document_type", "match_method", "is_verified"]
    search_fields = ["title", "product__title"]
    company_field = None
    parent_links = [("product", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "product__company"
        )
