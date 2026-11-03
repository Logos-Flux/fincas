from django.contrib.auth.mixins import LoginRequiredMixin

from core.labels import get_label, labels_for_request


class ERPViewMixin(LoginRequiredMixin):
    """Base mixin for all ERP views. Adds common context.

    Label layer (docs/labels.md): set ``entity_label_key`` to a
    ``core.label_registry`` key and the view's noun follows the tenant's /
    deployment's wording. ``page_title_format`` then builds the title from it
    (``"{plural}"``, ``"New {singular}"``…); ``page_title`` stays the
    fallback. Context gains ``entity_label`` / ``entity_label_plural`` for
    display. ``entity_name`` stays canonical: it names CSV export files and
    the chat-context payload, which are machine-facing and never relabelled.
    """
    login_url = "/erp/login/"
    current_section = ""
    page_title = ""
    entity_label_key = ""
    page_title_format = ""

    def get_entity_label(self):
        """The resolved Label for ``entity_label_key`` (None when unset)."""
        if not self.entity_label_key:
            return None
        namespace = labels_for_request(getattr(self, "request", None))
        return get_label(self.entity_label_key, company=namespace.company_id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_section"] = self.current_section
        page_title = self.page_title
        resolved = self.get_entity_label()
        canonical = getattr(self, "entity_name", "") or ""
        if resolved is not None:
            context["entity_label"] = resolved.singular
            context["entity_label_plural"] = resolved.plural
            if self.page_title_format:
                page_title = self.page_title_format.format(
                    singular=resolved.singular, plural=resolved.plural
                )
        else:
            context["entity_label"] = canonical
            context["entity_label_plural"] = f"{canonical}s" if canonical else ""
        context["page_title"] = page_title
        return context
