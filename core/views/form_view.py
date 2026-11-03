from django.urls import reverse
from django.views.generic import CreateView, UpdateView

from .mixins import ERPViewMixin


class ERPFormMixin(ERPViewMixin):
    """
    Shared mixin for create/update form views.

    Subclasses must define:
        model = SomeModel
        form_class = SomeModelForm
        current_section = "customers"
        page_title = "New Customer"  (or "Edit Customer")
        entity_name = "Customer"
        success_url_name = "erp-customer-list"
        form_sections = [
            {
                "title": "Basic Information",
                "fields": ["first_name", "last_name", "email", "phone"],
                "collapsible": False,
            },
            {
                "title": "Address",
                "fields": ["address_line_1", "city", "state_province"],
                "collapsible": True,
            },
        ]
    """
    template_name = "erp/form.html"
    entity_name = ""
    success_url_name = ""
    form_sections = []

    def get_success_url(self):
        if self.success_url_name:
            return reverse(self.success_url_name)
        return "/"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entity_name"] = self.entity_name
        context["form_sections"] = self.form_sections
        context["is_edit"] = hasattr(self, "object") and self.object and self.object.pk
        return context


class ERPCreateView(ERPFormMixin, CreateView):
    pass


class ERPUpdateView(ERPFormMixin, UpdateView):
    pass
