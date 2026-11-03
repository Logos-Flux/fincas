from django.db.models import Q
from django.views.generic import ListView

from core.views.mixins import ERPViewMixin

from .models import ProductDocument


class DocumentLibraryView(ERPViewMixin, ListView):
    model = ProductDocument
    template_name = "erp/document_library.html"
    context_object_name = "object_list"
    paginate_by = 24
    current_section = "documents"
    page_title = "Document Library"

    def get_queryset(self):
        qs = ProductDocument.objects.select_related("product").order_by("-created_at")

        search = self.request.GET.get("q", "").strip()
        if search:
            qs = qs.filter(
                Q(title__icontains=search) | Q(product__title__icontains=search)
            )

        doc_type = self.request.GET.get("document_type")
        if doc_type:
            qs = qs.filter(document_type=doc_type)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["view_mode"] = self.request.GET.get("view", "grid")
        context["search_query"] = self.request.GET.get("q", "")
        context["active_type"] = self.request.GET.get("document_type", "")
        context["document_type_choices"] = ProductDocument.DocumentType.choices
        return context
