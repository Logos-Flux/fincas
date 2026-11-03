import csv

from django.contrib import messages
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db.models import F
from django.db.models.fields.json import KeyTextTransform
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.utils.translation import gettext as _
from django.views.generic import ListView

from core import list_columns as registry
from core import list_export, list_prefs, saved_filters
from core.csv_safety import csv_safe

from .mixins import ERPViewMixin

#: Page sizes a user may pick in the column chooser.
PAGE_SIZES = (25, 50, 100)


class ERPListView(ERPViewMixin, ListView):
    """
    Generic reusable list view for all ERP entities.

    Subclasses must define:
        model = SomeModel
        current_section = "orders"
        page_title = "Sales Orders"
        entity_name = "Order"
        list_columns = [
            {"field": "order_number", "label": "Order #", "link": True},
            {"field": "customer__first_name", "label": "Customer"},
            {"field": "status", "label": "Status", "badge": True},
            {"field": "grand_total", "label": "Total", "money": True, "align": "end"},
        ]
        list_filters = [
            {"field": "status", "label": "Status", "choices": Order.Status.choices},
        ]
        create_url_name = "erp-order-create"  # optional
        entity_label_key = "orders.order"     # optional: relabelled noun
        page_title_format = "{plural}"        # optional: title from the label
        sortable_fields = None                # optional: see below

    **Column registry** (docs/crm/phase4-fields.md 5.1). ``list_columns``
    entries are ``core.list_columns.Column`` objects or the legacy dicts
    above (``id`` defaults to ``field``; ``priority`` 1-3 and ``default``
    are optional). ``get_columns()`` adds the definition columns of
    ``definition_object`` (``"party"``, ``"engagement"``, ``"task"``) and the
    plugin columns for ``list_id``. ``list_id`` (default: the model's
    ``app_label.model``) keys the user's column choice
    (``ERPUserSettings.list_preferences``), saved filters (``SavedFilter``)
    and the plugin hook.

    List querysets ``defer("metadata")``; visible definition columns are
    read with ``KeyTextTransform`` and plugin columns with ``F(path)``, both
    as annotations, so neither adds a query (3.4).

    **CRM exports** (``csv_canonical = True``, 5.5): canonical column ids as
    headers, canonical values, the visible columns or ``?columns=all``,
    streamed, refused above ``export_row_cap`` rows, one ``AuditLog`` row,
    and personal-data columns only with ``parties.export_pii``. Other lists
    keep their label headers.

    Sorting is allow-listed. ``?sort=<id>&dir=asc|desc`` only applies when
    ``<id>`` is one of the view's sortable ids; anything else (an unknown
    name, a related path the list never shows, ``customer__user__password``)
    is **ignored** and the default ``ordering`` applies, with no error, so a
    stale bookmark still loads. ``sortable_fields`` is:

    - ``None`` (default): every ``list_columns`` field that resolves to a
      concrete model field or forward foreign key (properties and reverse or
      many-to-many paths are not sortable), unless the column says
      ``"sortable": False``;
    - an iterable of ids, each sorted by itself;
    - a dict ``{id: (order_path, ...)}``, for a column whose value is a
      property (``full_name`` -> ``("last_name", "first_name")``).

    Sortable plugin columns are added to whichever form applies.
    """
    template_name = "erp/list.html"
    context_object_name = "object_list"
    paginate_by = 25
    entity_name = ""
    list_columns = []
    list_filters = []
    create_url_name = None
    ordering = ["-created_at"]
    search_fields = []
    sortable_fields = None
    #: Older ``?sort=`` ids mapped to today's column ids, so a bookmark or a
    #: saved filter from before a column was renamed keeps its order.
    sort_aliases: dict = {}
    #: Preference / saved-filter / plugin key; default ``app_label.model``.
    list_id = ""
    #: ``GOLDEN_FIELD_DEFINITIONS`` object whose definitions become columns.
    definition_object = ""
    #: Canonical streamed CRM export (5.5) instead of the legacy CSV.
    csv_canonical = False
    export_row_cap = 50_000
    #: Saved-filter allow-list: ``{param: allowed values or None}``. ``None``
    #: (default) derives it from ``list_filters`` plus ``q``; ``sort``/``dir``
    #: are always checked against the sort allow-list.
    filter_params = None
    #: Whether the page offers saved filters and the column chooser.
    saved_filters_enabled = True
    column_chooser = True

    # Status badge color mapping
    STATUS_COLORS = {
        # Success (green)
        "DELIVERED": "success", "PAID": "success", "COMPLETED": "success",
        "RECEIVED": "success", "CLOSED": "success", "ACCEPTED": "success",
        "SETTLED": "success", "FULFILLED": "success", "CLOSED_WON": "success",
        "CONVERTED": "success", "ACTIVE": "success", "PICKED": "success",
        "PACKED": "success", "POSTED": "success", "PASSED": "success",
        "PUBLISHED": "success",
        # Info (blue)
        "PROCESSING": "info", "IN_TRANSIT": "info", "SHIPPING": "info",
        "SHIPPED": "info", "PARTIAL": "info", "PICKING": "info",
        "PACKING": "info", "AUTHORIZED": "info", "CAPTURED": "info",
        "IN_PRODUCTION": "info", "QUALIFIED": "info", "CONTACTED": "info",
        "RECEIVING": "info", "SUBMITTED": "info",
        "AWAITING_SHIPMENT": "info", "PARTIALLY_SHIPPED": "info",
        "AWAITING_FULFILLMENT": "info",
        # Warning (yellow)
        "DRAFT": "warning", "PENDING": "warning", "PENDING_APPROVAL": "warning",
        "BOOKED": "warning", "UNPAID": "warning", "SENT": "warning",
        "CREATED": "warning", "ASSIGNED": "warning", "NEW": "warning",
        "PROSPECTING": "warning", "PROPOSAL_SENT": "warning",
        "NEGOTIATING": "warning", "ACKNOWLEDGED": "warning", "VERIFIED": "warning",
        "INCOMPLETE": "warning", "AWAITING_PAYMENT": "warning",
        "MANUAL_VERIFICATION_REQUIRED": "warning",
        # Danger (red)
        "CANCELLED": "danger", "OVERDUE": "danger", "FAILED": "danger",
        "RETURNED": "danger", "VOID": "danger", "REFUNDED": "danger",
        "DISPUTED": "danger", "CLOSED_LOST": "danger", "CLOSED_JUNK": "danger",
        "REJECTED": "danger", "MAJOR_DAMAGE": "danger",
        "DECLINED": "danger", "PARTIALLY_REFUNDED": "danger",
        # Secondary (gray)
        "ON_HOLD": "secondary", "EXPIRED": "secondary", "SUPERSEDED": "secondary",
        "LOST": "secondary", "ARCHIVED": "secondary", "BACKORDERED": "secondary",
    }

    def get_queryset(self):
        qs = super().get_queryset()

        # Apply search
        search = self.request.GET.get("q", "").strip()
        if search and self.search_fields:
            from django.db.models import Q
            q = Q()
            for f in self.search_fields:
                q |= Q(**{f"{f}__icontains": search})
            qs = qs.filter(q)

        # Apply filters from GET params
        for filt in self.list_filters:
            val = self.request.GET.get(filt["field"])
            if val:
                qs = qs.filter(**{filt["field"]: val})

        return self.apply_sort(qs)

    # -- the column registry (phase4-fields.md 5.1) ---------------------------

    def get_list_id(self):
        return self.list_id or self.model._meta.label_lower

    def viewer_company_id(self):
        """The tenant whose definitions apply (None: deployment-wide only)."""
        profile = getattr(self.request.user, "userprofile", None)
        return getattr(profile, "company_id", None)

    def get_columns(self):
        """Every column of the list, in registry order: the view's own, then
        definition columns, then plugin columns. Cached per request."""
        cached = getattr(self, "_columns_cache", None)
        if cached is not None:
            return cached
        columns = registry.from_legacy(self.list_columns, self._declared_sortable())
        if self.definition_object:
            columns += registry.definition_columns(self.definition_object, self.viewer_company_id())
        columns += self.get_plugin_columns()
        seen, aliases = set(), {}
        for col in columns:
            if col.id in seen:
                raise ImproperlyConfigured(f"{type(self).__name__}: column id {col.id!r} is declared twice.")
            if col.annotation in aliases:
                raise ImproperlyConfigured(
                    f"{type(self).__name__}: column ids {aliases[col.annotation]!r} and {col.id!r} "
                    "map to the same query alias; rename one.",
                )
            seen.add(col.id)
            aliases[col.annotation] = col.id
        self._columns_cache = columns
        return columns

    def get_plugin_columns(self):
        cached = getattr(self, "_plugin_columns_cache", None)
        if cached is None:
            cached = self._plugin_columns_cache = registry.plugin_columns(self.get_list_id(), self.model)
        return cached

    def get_preferences(self):
        cached = getattr(self, "_prefs_cache", None)
        if cached is None:
            cached = self._prefs_cache = list_prefs.get(getattr(self.request, "user", None), self.get_list_id())
        return cached

    def get_screen_columns(self):
        """``get_columns()`` minus what this viewer may not see on screen.

        Step 6 (phase4-fields.md 5.5): a personal-data *definition* or
        *plugin* column is shown and offered in the chooser only with
        ``parties.export_pii``. Contact-point columns (email, phone) stay on
        screen as before; the export gate withholds them, and the export
        (``cap=False``) reads ``get_columns()`` so its audit still names what
        it withheld."""
        columns = self.get_columns()
        gated = [c for c in columns if c.pii and c.source in ("definition", "plugin")]
        if not gated:
            return columns  # no permission lookup (no query) on lists without them
        user = getattr(self.request, "user", None)
        if user is not None and user.is_authenticated and user.has_perm("parties.export_pii"):
            return columns
        return [c for c in columns if c not in gated]

    def get_visible_columns(self, cap=True):
        """The columns on screen: the user's choice, else the defaults
        (``cap=False``: without the 12-column screen limit, for exports)."""
        columns = self.get_screen_columns() if cap else self.get_columns()
        ids = registry.visible_ids(columns, self.get_preferences().get("columns"), cap=cap)
        by_id = {c.id: c for c in columns}
        return [by_id[i] for i in ids]

    def with_columns(self, qs, columns):
        """``defer("metadata")`` (on the row and on every ``select_related``
        model that has one) and annotate definition and plugin columns."""
        deferred = [f"{path}__metadata" if path else "metadata"
                    for path, model in _joined_models(qs) if _has_field(model, "metadata")]
        if deferred:
            qs = qs.defer(*deferred)
        annotations = {}
        for col in columns:
            if col.source == "definition":
                annotations[col.annotation] = KeyTextTransform(col.path, "metadata")
            elif col.source == "plugin":
                annotations[col.annotation] = F(col.path)
        return qs.annotate(**annotations) if annotations else qs

    def get_paginate_by(self, queryset):
        size = self.get_preferences().get("page_size")
        return size if size in PAGE_SIZES else self.paginate_by

    # -- sorting (allow-listed) ------------------------------------------------

    def _declared_sortable(self):
        declared = self.sortable_fields
        if declared is None:
            return {
                col["field"]: (col["field"],)
                for col in self.list_columns
                if isinstance(col, dict)
                and col.get("sortable", True) and _is_orderable_path(self.model, col["field"])
            }
        if isinstance(declared, dict):
            return {key: tuple(paths) for key, paths in declared.items()}
        return {key: (key,) for key in declared}

    def get_sortable_fields(self):
        """``{sort id: (order paths...)}`` this view accepts in ``?sort=``."""
        sortable = self._declared_sortable()
        screen = {c.id for c in self.get_screen_columns()}
        for col in self.get_plugin_columns():
            if col.sortable and col.id in screen:  # a hidden pii column never sorts
                sortable.setdefault(col.id, col.sort_paths)
        return sortable

    def get_sort(self):
        """The effective ``(sort id, dir)``, or ``("", "asc")`` when the request
        asks for nothing sortable."""
        sort = self.request.GET.get("sort", "").strip()
        sort = self.sort_aliases.get(sort, sort)  # an older sort id still bookmarked
        if sort not in self.get_sortable_fields():
            return "", "asc"
        return sort, "desc" if self.request.GET.get("dir") == "desc" else "asc"

    def get_header_columns(self, sort, direction):
        """The visible columns plus what a header needs to be a keyboard sort
        link: ``sortable``, ``sort_query`` (the next click's query string) and
        ``aria_sort`` (only on the sorted column, per the ARIA table pattern)."""
        sortable = self.get_sortable_fields()
        base = self.request.GET.copy()
        for name in ("page", "export", "sort", "dir", "columns"):
            base.pop(name, None)
        columns = []
        for col in self.get_visible_columns():
            header = {"id": col.id, "field": col.field, "label": col.label, "align": col.align,
                      "priority_class": col.priority_class, "source": col.source,
                      "sortable": col.id in sortable, "aria_sort": ""}
            if header["sortable"]:
                active = col.id == sort
                if active:
                    header["aria_sort"] = "descending" if direction == "desc" else "ascending"
                query = base.copy()
                query["sort"] = col.id
                query["dir"] = "desc" if active and direction == "asc" else "asc"
                header["sort_query"] = query.urlencode()
            columns.append(header)
        return columns

    def apply_sort(self, qs):
        sort, direction = self.get_sort()
        if not sort:
            return qs
        prefix = "-" if direction == "desc" else ""
        paths = self.get_sortable_fields()[sort]
        return qs.order_by(*(f"{prefix}{p}" for p in paths), f"{prefix}pk")

    # -- saved filters (5.4) ---------------------------------------------------

    def get_filter_params(self):
        """``{param: allowed values or None}`` a saved filter may hold."""
        if self.filter_params is not None:
            return dict(self.filter_params)
        allowed = {"q": None}
        for filt in self.list_filters:
            choices = filt.get("choices")
            allowed[filt["field"]] = tuple(str(v) for v, _l in choices) if choices else None
        return allowed

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entity_name"] = self.entity_name
        context["list_columns"] = self.list_columns
        context["list_filters"] = self.list_filters
        context["create_url_name"] = self.create_url_name
        context["status_colors"] = self.STATUS_COLORS
        context["search_query"] = self.request.GET.get("q", "")
        context["active_filters"] = {
            f["field"]: self.request.GET.get(f["field"], "")
            for f in self.list_filters
        }
        sort, direction = self.get_sort()
        context["current_sort"] = sort
        context["current_sort_dir"] = direction
        # The whole query string (search, filters, sort), so the CSV export
        # and the pager return exactly the rows on screen.
        kept = self.request.GET.copy()
        for name in ("page", "export", "columns"):
            kept.pop(name, None)
        context["list_query"] = kept.urlencode()
        kept["export"] = "csv"
        context["export_query"] = kept.urlencode()
        kept["columns"] = "all"
        context["export_all_query"] = kept.urlencode()
        context["csv_canonical"] = self.csv_canonical
        visible = self.get_visible_columns()
        context["visible_columns"] = visible
        context["header_columns"] = self.get_header_columns(sort, direction)
        if self.column_chooser:
            context["chooser"] = self.get_chooser(visible)
        if self.saved_filters_enabled:
            context["saved_filters"] = saved_filters.chips(self)
        context.update(self.get_list_status(
            context, self.list_filters, context["active_filters"], sort, direction,
        ))
        context["list_view_id"] = self.model._meta.label_lower.replace(".", "-")
        return context

    def get_chooser(self, visible):
        """The column chooser's groups (5.3): Default, Fields, Other."""
        shown = {c.id for c in visible}
        groups = {"Default": [], "Fields": [], "Other": []}
        for col in self.get_screen_columns():
            if col.export_only:
                continue
            group = col.group or ("Default" if col.default else "Other")
            groups.setdefault(group, []).append({"column": col, "checked": col.id in shown})
        page_size = self.get_preferences().get("page_size")
        return {
            "groups": [(name, items) for name, items in groups.items() if items],
            "max": registry.MAX_COLUMNS,
            "has_preference": bool(self.get_preferences().get("columns")),
            "page_sizes": PAGE_SIZES,
            "page_size": page_size if page_size in PAGE_SIZES else self.paginate_by,
        }

    # -- caption and live region (phase4-fields.md 5.7) -----------------------

    def get_list_status(self, context, filters, active, sort, direction, *, filtered=None):
        """``list_caption`` (the table's visually hidden caption: the list, its
        active search and filters and its sort), ``list_status`` (the result
        count for the polite live region) and ``list_filtered`` (whether the
        page shows a search or filter result, which the live region announces).

        ``filters`` are ``{"field", "label", "choices"}`` dicts and ``active``
        maps a field to its current value; a value outside the choices is
        shown as given (the queryset decides what it means). A view whose
        defaults filter the list (mine, open) passes the effective values in
        ``active`` so the caption says what is shown, and ``filtered`` (did
        the request ask for a search or filter) so a default page is not
        announced."""
        search = self.request.GET.get("q", "").strip()
        conditions = []
        for filt in filters:
            value = active.get(filt["field"], "")
            if not value:
                continue
            text = next((str(lbl) for val, lbl in filt.get("choices", ()) if str(val) == value),
                        value)
            conditions.append(f"{filt['label']}: {text}")
        title = str(context.get("page_title") or self.entity_name or "List")
        parts = [title]
        if search:
            parts.append(f"search “{search}”")
        if conditions:
            parts.append("filtered by " + "; ".join(conditions))
        if sort:
            shown = [(h.get("id") or h.get("field"), h["label"]) for h in context.get("header_columns", ())]
            shown += [(c.id, c.label) for c in self.get_columns()]
            label = next((str(text) for col_id, text in shown if col_id == sort),
                         sort.replace("_", " "))
            parts.append(f"sorted by {label}, {'descending' if direction == 'desc' else 'ascending'}")

        paginator = context.get("paginator")
        count = paginator.count if paginator is not None else len(context.get("object_list") or ())
        return {
            "list_caption": ", ".join(parts),
            "list_status": f"{count} result" if count == 1 else f"{count} results",
            "list_filtered": bool(search or conditions) if filtered is None else filtered,
        }

    # -- request handling ------------------------------------------------------

    def get(self, request, *args, **kwargs):
        if request.GET.get("saved"):
            return saved_filters.load(self, request.GET["saved"])
        if request.GET.get("export") == "csv":
            return self.export_csv()
        self.object_list = self.with_columns(self.get_queryset(), self.get_visible_columns())
        if not self.get_allow_empty() and not self.object_list.exists():
            raise Http404(_("Empty list."))
        context = self.get_context_data()
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        """The chooser and saved-filter forms post here, then redirect back
        to the list with the query string they were posted from."""
        action = request.POST.get("list_action", "")
        if action == "columns" and self.column_chooser:
            self._save_columns(reset=bool(request.POST.get("reset")))
        elif action == "save_filter" and self.saved_filters_enabled:
            saved_filters.save(self)
        elif action == "delete_filter" and self.saved_filters_enabled:
            saved_filters.delete(self, request.POST.get("filter", ""))
        else:
            return HttpResponse(status=400)
        return self.redirect_back()

    def redirect_back(self, query=None):
        if query is None:
            query = list_prefs.clean_query(self.request.POST.get("query", ""))
        return HttpResponseRedirect(self.request.path + (f"?{query}" if query else ""))

    def _save_columns(self, *, reset):
        columns = self.get_screen_columns()
        if reset:
            list_prefs.save(self.request.user, self.get_list_id(), columns=None)
            return
        offered = {c.id for c in columns if not c.export_only}
        chosen = list(dict.fromkeys(i for i in self.request.POST.getlist("columns") if i in offered))
        if not chosen:
            messages.error(self.request, "Choose at least one column.")
            return
        if len(chosen) > registry.MAX_COLUMNS:
            messages.error(self.request, f"Choose at most {registry.MAX_COLUMNS} columns.")
            return
        size = self.request.POST.get("page_size", "")
        page_size = int(size) if size.isdigit() and int(size) in PAGE_SIZES else None
        list_prefs.save(self.request.user, self.get_list_id(),
                        columns=[c.id for c in columns if c.id in chosen], page_size=page_size)

    # -- CSV export ------------------------------------------------------------

    def export_csv(self):
        if self.csv_canonical:
            return list_export.export(self)
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{self.entity_name.lower()}s.csv"'
        writer = csv.writer(response)
        headers = [col["label"] for col in self.list_columns]
        writer.writerow(headers)
        for obj in self.get_queryset():
            row = []
            for col in self.list_columns:
                val = self._resolve_field(obj, col["field"])
                row.append(self._csv_safe(val))
            writer.writerow(row)
        return response

    def export_value(self, obj, col):
        """A field column's canonical export value. Override for computed
        columns (a list of role names, a related name)."""
        return self._resolve_field(obj, col.path)

    # Kept as a method so older callers keep working; the rule lives in
    # core.csv_safety, shared by every CSV writer.
    _csv_safe = staticmethod(csv_safe)

    @staticmethod
    def _resolve_field(obj, field_path):
        """Resolve dotted/dunder field paths like 'customer__first_name'."""
        parts = field_path.split("__")
        val = obj
        for part in parts:
            if val is None:
                return ""
            val = getattr(val, part, "")
        if callable(val):
            val = val()
        return val if val is not None else ""


def _joined_models(qs):
    """``(path, model)`` for the queryset's model and each ``select_related``
    model (``""`` for the row itself)."""
    out = [("", qs.model)]
    tree = qs.query.select_related
    if tree is True:  # select_related() with no names: not used by list views
        return out

    def walk(model, branch, prefix):
        for name, sub in (branch or {}).items():
            try:
                fld = model._meta.get_field(name)
            except FieldDoesNotExist:
                continue
            related = fld.related_model
            if related is None:
                continue
            path = f"{prefix}{name}"
            out.append((path, related))
            walk(related, sub, f"{path}__")

    if isinstance(tree, dict):
        walk(qs.model, tree, "")
    return out


def _has_field(model, name):
    try:
        model._meta.get_field(name)
    except FieldDoesNotExist:
        return False
    return True


def _is_orderable_path(model, path):
    """True when ``path`` walks forward relations to a concrete field.

    Reverse and many-to-many hops are refused (they multiply rows), as is
    anything that is not a model field (a property cannot be ordered on)."""
    parts = path.split("__")
    for i, part in enumerate(parts):
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return False
        if not field.concrete or field.many_to_many or field.one_to_many:
            return False
        if i < len(parts) - 1:
            if not field.is_relation or field.related_model is None:
                return False
            model = field.related_model
    return True
