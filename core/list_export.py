"""CRM CSV export (docs/crm/phase4-fields.md 5.5, F11).

- **Rows:** the list's own queryset, i.e. the current search, filters and
  sort (``ERPListView.get_queryset``), never a page.
- **Columns:** the user's visible columns, or every exportable column with
  ``?columns=all`` (every column the user may see plus every definition with
  ``export=True``).
- **Headers:** canonical column ids, never labels.
- **Values:** canonical: registry keys and choice values, ISO 8601 dates and
  UTC date-times (``Z``), decimals as plain strings, ``true``/``false``,
  empty for unknown; formula-neutralised (``core.csv_safety``).
- **Size:** counted first; above ``view.export_row_cap`` (50,000) the export
  is refused ("narrow the filter"); otherwise streamed in chunks.
- **Personal data:** a column with ``pii`` (contact points, ``pii``
  definitions, ``pii`` plugin columns) is exported only for users with
  ``parties.export_pii``. This is the one gate every column passes through,
  whichever way it was selected.
- **Audit:** one ``AuditLog`` row per export (``action="export"``), written
  before the first byte, with the list id, parameters, columns, withheld
  columns and row count.
"""

from __future__ import annotations

import csv
import datetime
import re
import uuid
from decimal import Decimal

from django.contrib import messages
from django.http import HttpResponseRedirect, StreamingHttpResponse

from . import list_columns as registry
from .csv_safety import csv_safe

EXPORT_PII = "parties.export_pii"
CHUNK = 2000
# Contact-shaped text (the check_sidecar heuristics). A display name falls
# back to the email when a person has no name, so without export_pii such a
# value is blanked in every text column, not only in the pii ones.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\A\+?[\d\s().-]{7,}\Z")


class _Echo:
    def write(self, value):
        return value


def export_columns(view):
    """``(columns, withheld ids)`` for this request."""
    if view.request.GET.get("columns") == "all":
        columns = [c for c in view.get_columns() if c.export]
    else:
        # The visible set without the on-screen cap: a list whose defaults
        # outnumber MAX_COLUMNS (the engagement export) keeps every column.
        columns = view.get_visible_columns(cap=False)
    allowed = view.request.user.has_perm(EXPORT_PII)
    withheld = [c.id for c in columns if c.pii and not allowed]
    return [c for c in columns if not c.pii or allowed], withheld


def export(view):
    request = view.request
    columns, withheld = export_columns(view)
    qs = view.with_columns(view.get_queryset(), columns)
    rows = qs.count()
    if rows > view.export_row_cap:
        messages.error(request, (
            f"This export has {rows:,} rows; the limit is {view.export_row_cap:,}. "
            "Narrow the filter and export again."
        ))
        query = request.GET.copy()
        for name in ("export", "columns"):
            query.pop(name, None)
        encoded = query.urlencode()
        return HttpResponseRedirect(request.path + (f"?{encoded}" if encoded else ""))
    redact = not request.user.has_perm(EXPORT_PII)
    _audit(view, columns, withheld, rows, redact)
    writer = csv.writer(_Echo())

    def cell(obj, col):
        text = _value(view, obj, col)
        if redact and col.format == "text" and _contact_shaped(text):
            return ""
        return csv_safe(text)

    def stream():
        yield writer.writerow([c.id for c in columns])
        for obj in qs.iterator(chunk_size=CHUNK):
            yield writer.writerow([cell(obj, c) for c in columns])

    response = StreamingHttpResponse(stream(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{view.entity_name.lower()}s.csv"'
    return response


def _value(view, obj, col):
    if col.source == "definition":
        return registry.export_value(col, registry.raw_value(col, obj))
    if col.source == "plugin":
        return canonical(registry.raw_value(col, obj))
    return canonical(view.export_value(obj, col))


def _contact_shaped(text: str) -> bool:
    if not text:
        return False
    if _EMAIL_RE.search(text):
        return True
    return bool(_PHONE_RE.match(text.strip())) and sum(c.isdigit() for c in text) >= 7


def canonical(value) -> str:
    """One value as canonical CSV text (5.5)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc)
            return value.isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (list, tuple)):
        return ";".join(canonical(v) for v in value)
    return str(value)


def _audit(view, columns, withheld, rows, redacted) -> None:
    """One row per export. ``params`` are the list's own allow-listed
    parameters (``saved_filters.clean_params``), with the search text
    replaced by its length: a party search can be an email or a phone
    number, and audit rows never hold personal data."""
    from .client_ip import client_ip
    from .models import AuditLog
    from .saved_filters import clean_params

    request = view.request
    params, _dropped = clean_params(view, {k: v for k, v in request.GET.items()
                                           if k not in ("export", "columns")})
    if "q" in params:
        params["q"] = f"<{len(params['q'])} characters>"
    if request.GET.get("columns") == "all":
        params["columns"] = "all"
    try:
        ip = client_ip(request) or None
    except Exception:  # an unparsable proxy chain must not block the audit row
        ip = None
    AuditLog.objects.create(
        user=getattr(request.user, "userprofile", None),
        action="export",
        model_name=view.model._meta.label,
        object_id=uuid.uuid4(),
        changes={
            "list_id": view.get_list_id(), "params": params, "rows": rows,
            "columns": [c.id for c in columns], "withheld": withheld,
            "contact_values_redacted": redacted,
        },
        ip_address=ip,
    )
