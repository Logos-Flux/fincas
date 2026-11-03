"""``/erp/settings/labels/``: a tenant rewords the staff-UI nouns (docs/labels.md).

Every staff user reads the page (what each noun is called here, and where
the word comes from: golden, the deployment's settings, or this tenant);
saving or clearing a rewording needs ``core.change_labeloverride``. The
tenant is the requester's company (``parties.erp_views.request_company_id``);
a user with no company cannot edit (there is no tenant to reword for).

Rows are ``core.LabelOverride``; ``core.label_registry`` reads them through
its cache, which the model's receivers invalidate on every save or delete.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import TemplateView

from core import label_registry
from core.label_registry import MAX_TEXT, Label, default_article
from core.models import LabelOverride

from .mixins import ERPViewMixin

SECTION = "settings_labels"
PERMISSION = "core.change_labeloverride"
ARTICLES = (("", "derive from the word"), ("a", "a"), ("an", "an"))


def _company_id(request):
    from parties.erp_views import request_company_id

    return request_company_id(request)


class LabelOverrideForm(forms.Form):
    key = forms.ChoiceField(choices=())
    singular = forms.CharField(max_length=MAX_TEXT, required=False, strip=True)
    plural = forms.CharField(max_length=MAX_TEXT, required=False, strip=True)
    article = forms.ChoiceField(choices=ARTICLES, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["key"].choices = [(k, k) for k in label_registry.DEFAULTS]

    def clean(self):
        data = super().clean()
        singular = data.get("singular", "")
        if singular and not data.get("plural"):
            data["plural"] = singular + "s"
        if data.get("article") not in ("a", "an"):
            data["article"] = ""
        return data


def label_rows(company_id) -> list[dict]:
    """One row per registered key: golden default, deployment override, the
    tenant's effective label and where it comes from."""
    stored = {}
    if company_id is not None:
        stored = {row.key: row for row in LabelOverride.objects.filter(company_id=company_id)}
    configured = label_registry.TENANTS.get(str(company_id), {}) if company_id is not None else {}
    rows = []
    for key, default in label_registry.DEFAULTS.items():
        deployment = label_registry.DEPLOYMENT.get(key)
        row = stored.get(key)
        if row is not None:
            effective = Label(row.singular, row.plural, row.article or default_article(row.singular))
            source = "tenant"
        elif key in configured:
            effective, source = configured[key], "tenant setting"
        elif deployment is not None:
            effective, source = deployment, "deployment"
        else:
            effective, source = default, "golden"
        rows.append({
            "key": key, "default": default, "deployment": deployment, "stored": row,
            "effective": effective, "source": source,
        })
    return rows


class LabelsView(ERPViewMixin, TemplateView):
    template_name = "erp/settings/labels.html"
    current_section = SECTION
    page_title = "Labels"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company_id = _company_id(self.request)
        context["rows"] = label_rows(company_id)
        context["can_manage"] = self.request.user.has_perm(PERMISSION) and company_id is not None
        context["has_company"] = company_id is not None
        context["articles"] = ARTICLES
        return context

    def post(self, request):
        if not request.user.has_perm(PERMISSION):
            raise PermissionDenied("Rewording labels needs the core.change_labeloverride permission.")
        company_id = _company_id(request)
        if company_id is None:
            raise PermissionDenied("Your account has no company, so there is no tenant to reword labels for.")
        form = LabelOverrideForm(request.POST)
        if not form.is_valid():
            messages.error(request, "; ".join(f"{f}: {e[0]}" for f, e in form.errors.items()))
            return redirect(reverse("erp-settings-labels"))
        key = form.cleaned_data["key"]
        singular = form.cleaned_data["singular"]
        if not singular:
            deleted, _ = LabelOverride.objects.filter(company_id=company_id, key=key).delete()
            messages.success(request, f"{key}: back to the default." if deleted else f"{key}: nothing stored.")
        else:
            LabelOverride.objects.update_or_create(
                company_id=company_id, key=key,
                defaults={"singular": singular, "plural": form.cleaned_data["plural"],
                          "article": form.cleaned_data["article"]},
            )
            messages.success(request, f'{key} is now "{singular}" ({form.cleaned_data["plural"]}).')
        return redirect(reverse("erp-settings-labels") + f"#label-{key.replace('.', '-')}")
