"""Access-matrix enumeration (docs/release-hardening-plan.md section 6.1).

Walks the shipped URLconf (``golden_erp.urls``; ``config/urls.py`` is a thin
layer over it) and sorts every route into one of three groups:

(a) :func:`api_viewsets` -- every DRF viewset on the API routers, one
    :class:`ApiEndpoint` per (action, method): ``list``, ``retrieve``,
    ``create``, ``update``, ``partial_update``, ``destroy`` and every
    ``@action``.
(b) :func:`erp_model_views` -- every ``/erp/`` class-based view with a declared
    ``model`` (Django's generic views and the project's ``ERPListView``), one
    :class:`ErpModelView` per route, its action inferred from the base class
    (``list`` / ``detail`` / ``create`` / ``edit`` / ``delete``; an ``ERPListView``
    also gets ``export`` for ``?export=csv``); a model view that is none of
    those is ``custom:<url name>``.
(c) :func:`other_routes` -- everything else outside the Django admin: function
    views, class-based views without a model, the OIDC, login, logout and
    password-reset routes, the portal and token endpoints, and plugin routes
    (mounted last by ``core.plugins.url_auth``).

Nothing here touches the database: the test
(``core/tests/test_access_matrix.py``) seeds rows and drives the requests; the
expectations live in ``core/tests/access_matrix_expectations.py``. The
unversioned ``/api/<app>/`` aliases mount the same views as ``/api/v1/`` and are
dropped, so each viewset is listed once.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Optional

from django.urls import URLPattern, URLResolver, get_resolver

#: The URLconf the wheel ships. Tests pass it explicitly so a project whose
#: ``ROOT_URLCONF`` wraps it is enumerated the same way.
URLCONF = "golden_erp.urls"

#: Standard viewset actions, in the order the matrix reports them.
API_ACTIONS = ("list", "retrieve", "create", "update", "partial_update", "destroy")

#: Model-view actions, in the order the matrix reports them.
ERP_ACTIONS = ("list", "export", "detail", "create", "edit", "delete")

_REGEX_GROUP = re.compile(r"\(\?P<(\w+)>[^)]*\)")
_CONVERTER = re.compile(r"<(?:\w+:)?(\w+)>")
_TYPED_CONVERTER = re.compile(r"<(\w+):(\w+)>")
_PARAM = re.compile(r"\{(\w+)\}")
_SKIPPED_PREFIXES = ("admin/",)
_ALIAS_API_PREFIX = "api/"
_API_PREFIX = "api/v1/"


@dataclass(frozen=True)
class ApiEndpoint:
    """One (viewset action, HTTP method) pair on the API."""

    model: str  # "app_label.ModelName"
    view: str  # dotted class name
    cls: type
    basename: str
    action: str  # list / retrieve / ... or the @action's name
    method: str  # lower-case HTTP method
    template: str  # "/api/v1/orders/orders/{pk}/"
    detail: bool
    converters: dict = dataclasses.field(default_factory=dict)  # param -> "uuid" / "int" / ...

    @property
    def params(self) -> tuple[str, ...]:
        return tuple(_PARAM.findall(self.template))


@dataclass(frozen=True)
class ErpModelView:
    """One ``/erp/`` class-based view with a declared ``model``."""

    model: str
    view: str
    cls: type
    name: str  # URL name (namespaced when the include has one)
    template: str  # "/erp/orders/{pk}/edit/"
    action: str  # list / export / detail / create / edit / delete / custom:<name>
    methods: tuple[str, ...]
    converters: dict = dataclasses.field(default_factory=dict)

    @property
    def params(self) -> tuple[str, ...]:
        return tuple(_PARAM.findall(self.template))


@dataclass(frozen=True)
class OtherRoute:
    """A route that is neither an API viewset nor an ``/erp/`` model view."""

    name: str
    template: str
    view: str
    cls: Optional[type]
    methods: tuple[str, ...]
    plugin: bool
    converters: dict = dataclasses.field(default_factory=dict)

    @property
    def params(self) -> tuple[str, ...]:
        return tuple(_PARAM.findall(self.template))


def _template(route: str) -> str:
    """A URL with ``{param}`` placeholders from a route or regex pattern."""
    route = route.replace("^", "").replace("$", "").replace("\\.", ".")
    route = _REGEX_GROUP.sub(r"{\1}", route)
    route = _CONVERTER.sub(r"{\1}", route)
    return "/" + route.lstrip("/")


def _converters(route: str) -> dict:
    """``{param: converter}`` for the path converters a route names
    (``<uuid:pk>`` -> ``{"pk": "uuid"}``); regex groups are left out."""
    return {name: kind for kind, name in _TYPED_CONVERTER.findall(route)}


def _walk(patterns, prefix="", namespace=None, plugin=False):
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from _walk(
                pattern.url_patterns, route, pattern.namespace or namespace, plugin,
            )
        elif isinstance(pattern, URLPattern):
            name = pattern.name or ""
            if namespace and name:
                name = f"{namespace}:{name}"
            yield route, name, pattern.callback, plugin


def _patterns(urlconf: str):
    """``(patterns, plugin?)`` pairs: the core URLconf, then the plugin block."""
    from core.plugins import plugin_registry

    resolver = get_resolver(urlconf)
    plugin_count = len(plugin_registry.get_urls())
    all_patterns = list(resolver.url_patterns)
    core = all_patterns[: len(all_patterns) - plugin_count] if plugin_count else all_patterns
    plugins = all_patterns[len(core):]
    return [(core, False), (plugins, True)]


def _routes(urlconf: str):
    """Every leaf route once: ``(template, name, callback, plugin?)``.

    Format-suffix patterns (``.json``) and the unversioned ``/api/`` aliases
    are dropped; so is the Django admin, which has its own auth.
    """
    collected = []
    for patterns, plugin in _patterns(urlconf):
        for route, name, callback, is_plugin in _walk(patterns, plugin=plugin):
            if route.startswith(_SKIPPED_PREFIXES):
                continue
            template = _template(route)
            if "{format}" in template:
                continue
            collected.append((template, name, callback, is_plugin, _converters(route)))
    versioned = {t for t, *_ in collected if t.startswith("/" + _API_PREFIX)}
    seen = set()
    for template, name, callback, is_plugin, converters in collected:
        if template.startswith("/" + _ALIAS_API_PREFIX) and not template.startswith("/" + _API_PREFIX):
            alias_of = "/" + _API_PREFIX + template[len("/" + _ALIAS_API_PREFIX):]
            if alias_of in versioned:
                continue
        key = (template, name)
        if key in seen:
            continue
        seen.add(key)
        yield template, name, callback, is_plugin, converters


def _view_class(callback):
    """The class behind a callback: DRF ``.cls``, Django ``.view_class``."""
    return getattr(callback, "cls", None) or getattr(callback, "view_class", None)


def _model_of(cls) -> Optional[str]:
    model = getattr(cls, "model", None)
    if model is None:
        queryset = getattr(cls, "queryset", None)
        model = getattr(queryset, "model", None)
    return model._meta.label if model is not None else None


def _dotted(obj) -> str:
    return f"{obj.__module__}.{getattr(obj, '__qualname__', getattr(obj, '__name__', obj))}"


def _unwrapped(callback):
    """The view the plugin staff gate wrapped, so its class is still visible.

    Only a plain function is unwrapped: DRF's ``as_view`` sets ``__wrapped__``
    on its view function to the viewset class itself.
    """
    inner = callback
    while _view_class(inner) is None and getattr(inner, "__wrapped__", None) is not None:
        inner = inner.__wrapped__
    return inner


def api_viewsets(urlconf: str = URLCONF) -> list[ApiEndpoint]:
    """(a) every routed viewset action with a model, once per method."""
    from rest_framework.viewsets import ViewSetMixin

    found: list[ApiEndpoint] = []
    for template, name, callback, _plugin, converters in _routes(urlconf):
        cls = _view_class(callback)
        actions = getattr(callback, "actions", None)
        if cls is None or not actions or not issubclass(cls, ViewSetMixin):
            continue
        model = _model_of(cls)
        if model is None:
            continue  # no declared model: classified by route name instead
        basename = re.sub(r"-(list|detail)$", "", name)
        detail = "{pk}" in template
        for method, action in actions.items():
            found.append(ApiEndpoint(
                model=model, view=_dotted(cls), cls=cls, basename=basename,
                action=action, method=method.lower(), template=template, detail=detail,
                converters=converters,
            ))
    return found


def _erp_action(cls, name: str) -> tuple[str, tuple[str, ...]]:
    from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

    if issubclass(cls, ListView):
        return "list", ("get",)
    if issubclass(cls, DetailView):
        return "detail", ("get",)
    if issubclass(cls, CreateView):
        return "create", ("get", "post")
    if issubclass(cls, UpdateView):
        return "edit", ("get", "post")
    if issubclass(cls, DeleteView):
        return "delete", ("post",)
    methods = tuple(m for m in ("get", "post") if hasattr(cls, m))
    if name.endswith("-delete") and "post" in methods:
        return "delete", ("post",)
    return f"custom:{name}", methods


def erp_model_views(urlconf: str = URLCONF) -> list[ErpModelView]:
    """(b) every ``/erp/`` class-based view that declares a ``model``.

    An ``ERPListView`` (or any list view with ``export_csv``) yields a second
    entry, action ``export``, for ``?export=csv``.
    """
    found: list[ErpModelView] = []
    for template, name, callback, _plugin, converters in _routes(urlconf):
        if not template.startswith("/erp/"):
            continue
        cls = _view_class(callback)
        if cls is None or getattr(cls, "model", None) is None:
            continue
        model = _model_of(cls)
        action, methods = _erp_action(cls, name)
        base = dict(model=model, view=_dotted(cls), cls=cls, name=name, template=template,
                    converters=converters)
        found.append(ErpModelView(action=action, methods=methods, **base))
        if action == "list" and hasattr(cls, "export_csv"):
            found.append(ErpModelView(action="export", methods=("get",), **base))
    return found


def other_routes(urlconf: str = URLCONF) -> list[OtherRoute]:
    """(c) every remaining route: no viewset model and no ``/erp/`` model view."""
    from rest_framework.viewsets import ViewSetMixin

    found: list[OtherRoute] = []
    for template, name, callback, plugin, converters in _routes(urlconf):
        inner = _unwrapped(callback)
        cls = _view_class(inner)
        if cls is not None:
            actions = getattr(inner, "actions", None)
            if actions and issubclass(cls, ViewSetMixin) and _model_of(cls) is not None:
                continue  # (a)
            if template.startswith("/erp/") and getattr(cls, "model", None) is not None:
                continue  # (b)
            if actions:
                methods = tuple(m.lower() for m in actions)
            else:
                methods = tuple(m for m in ("get", "post") if hasattr(cls, m)) or ("get",)
            view = _dotted(cls)
        else:
            methods = ("get",)
            view = _dotted(inner)
        found.append(OtherRoute(
            name=name, template=template, view=view, cls=cls, methods=methods, plugin=plugin,
            converters=converters,
        ))
    return found
