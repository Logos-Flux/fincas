"""The one reader for staff-UI noun labels (CRM Phase 3, §3.2).

    from core.labels import label
    label("crm.engagement")                          # "Engagement"
    label("crm.engagement", plural=True)             # "Engagements"
    label("crm.engagement", article=True)            # "an Engagement"
    label("orders.quote", company=request_company)   # tenant layer first

Resolution: tenant override → deployment override → golden default
(``core.label_registry``). Staff UI only: never call this from an API
serializer, CSV export, PDF, email, signal or webhook payload.
"""

from __future__ import annotations

from typing import Any

from . import label_registry
from .label_registry import Label

CASES = ("as_is", "lower", "title")


class UnknownLabel(KeyError):
    """A label key that is not in the registry."""


def company_id_of(company: Any) -> str | None:
    """A Company, its id (UUID or str), or None, as a normalised id string."""
    if company is None or company == "":
        return None
    company_id = getattr(company, "pk", company)
    return str(company_id) if company_id is not None else None


def company_id_for_request(request: Any) -> str | None:
    """The requesting user's Company id, or None (deployment labels).

    Only touches the profile when a tenant layer is configured, so a
    deployment without tenant overrides pays no query for labels, and one
    with them pays at most one (the profile, cached on ``request.user``). A
    user with no profile sees deployment labels; a superuser *with* a profile
    sees that profile's company's labels (the spec's rule; unlike
    ``core.views.request_company``, which is about data scope, not wording).
    """
    if not label_registry.has_tenant_overrides():
        return None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    profile = getattr(user, "userprofile", None)
    company_id = getattr(profile, "company_id", None)
    return str(company_id) if company_id else None


def get_label(key: str, *, company: Any = None) -> Label:
    """The resolved Label for ``key``; ``UnknownLabel`` if the key is not registered."""
    if key not in label_registry.DEFAULTS:
        raise UnknownLabel(f"unknown label key {key!r}")
    company_id = company_id_of(company)
    if company_id is not None:
        tenant = label_registry.tenant_layer(company_id)
        if tenant and key in tenant:
            return tenant[key]
    if key in label_registry.DEPLOYMENT:
        return label_registry.DEPLOYMENT[key]
    return label_registry.DEFAULTS[key]


def render(resolved: Label, *, plural: bool = False, article: bool = False, case: str = "as_is") -> str:
    if case not in CASES:
        raise ValueError(f"case={case!r}; must be one of {', '.join(CASES)}")
    text = resolved.plural if plural else resolved.singular
    if article and not plural and resolved.article:
        text = f"{resolved.article} {text}"
    if case == "lower":
        text = text.lower()
    elif case == "title":
        text = text.title()
    return text


def label(
    key: str,
    plural: bool = False,
    *,
    company: Any = None,
    article: bool = False,
    case: str = "as_is",
) -> str:
    """The display text for ``key``: tenant → deployment → golden default."""
    return render(get_label(key, company=company), plural=plural, article=article, case=case)


class LabelNamespace:
    """Template-friendly view of the labels for one company.

    ``{{ labels.crm.engagement }}`` → singular, ``{{ labels.crm.engagement.plural }}``
    → plural. Lookups resolve lazily, so a template that never touches
    ``labels`` costs nothing.
    """

    def __init__(self, company: Any = None, prefix: str = "", *, request: Any = None):
        self._company = company
        self._request = request
        self._prefix = prefix
        self._resolved = company is not None or request is None

    @property
    def company_id(self) -> str | None:
        if not self._resolved:
            self._company = company_id_for_request(self._request)
            self._resolved = True
        return company_id_of(self._company)

    def __getitem__(self, segment: str) -> Any:
        if not isinstance(segment, str):
            raise KeyError(segment)
        key = f"{self._prefix}.{segment}" if self._prefix else segment
        if key in label_registry.DEFAULTS:
            return get_label(key, company=self.company_id)
        if any(k.startswith(f"{key}.") for k in label_registry.DEFAULTS):
            child = LabelNamespace(prefix=key)
            child._company = self.company_id
            child._resolved = True
            return child
        raise KeyError(key)

    def __str__(self) -> str:  # never render a namespace as text by accident
        return ""


def labels_for_request(request: Any) -> LabelNamespace:
    """One lazy namespace per request, shared by the context processor, the
    ``{% label %}`` tag and ``ERPViewMixin`` so the company resolves once."""
    if request is None:
        return LabelNamespace()
    namespace = getattr(request, "_golden_labels", None)
    if namespace is None:
        namespace = LabelNamespace(request=request)
        try:
            request._golden_labels = namespace
        except AttributeError:  # pragma: no cover - a request we cannot annotate
            pass
    return namespace
