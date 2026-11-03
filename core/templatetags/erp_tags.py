from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from core import once

register = template.Library()


@register.filter(name="get_field")
def get_field(obj, field_path):
    """Resolve a field path like 'customer__first_name' or 'status' on an object.
    If the resolved attribute has a get_FOO_display method, use that."""
    parts = field_path.split("__")
    val = obj
    for i, part in enumerate(parts):
        if val is None:
            return ""
        # If this is the last part and has a display method, use it
        if i == len(parts) - 1:
            display_method = getattr(val, f"get_{part}_display", None)
            if display_method and callable(display_method):
                return display_method()
        val = getattr(val, part, "")
    if callable(val):
        val = val()
    return val if val is not None else ""


@register.filter(name="get_raw_field")
def get_raw_field(obj, field_path):
    """Resolve a field path and return the raw value (no get_FOO_display)."""
    parts = field_path.split("__")
    val = obj
    for part in parts:
        if val is None:
            return ""
        val = getattr(val, part, "")
    if callable(val):
        val = val()
    return val if val is not None else ""


@register.filter(name="status_color")
def status_color(value, color_map):
    """Look up a status value in the color map dict. Default to 'secondary'."""
    if isinstance(color_map, dict):
        return color_map.get(str(value), "secondary")
    return "secondary"


@register.filter(name="bool_badge")
def bool_badge(value):
    """Strict boolean classifier for Yes/No badge cells.

    Returns "yes" / "no" / "". isinstance-strict on purpose: numeric 1/0
    and "True"/"False" strings are NOT booleans and must fall through to
    the plain-text cell instead of picking up a badge.
    """
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return ""


@register.filter(name="add_class")
def add_class(field, css_class):
    """Add a CSS class to a form field widget.

    Also gives the control its field label as an accessible name: this filter
    renders the formset grids (line items, variants) and totals inputs, whose
    only label is a visual column header, so screen readers announced nothing.
    An explicit aria-label on the widget wins.
    """
    if hasattr(field, "as_widget"):
        attrs = {"class": css_class}
        if field.label and "aria-label" not in field.field.widget.attrs:
            attrs["aria-label"] = str(field.label)
        return field.as_widget(attrs=attrs)
    return field


#: Currencies shown with a symbol. Golden runs single-currency USD for now
#: (multi-currency display is a backlog item), so only USD has one: any
#: other code keeps its prefix, and 100 CAD can never read as $100.
MONEY_SYMBOLS = {"USD": "$"}


@register.filter(name="money")
def money(value, currency="USD"):
    """One money filter (DD-14): amount + currency + negative/zero.

    ``{{ amount|money }}`` or ``{{ amount|money:"CAD" }}``. USD renders as
    ``$1,234.56``; any other currency keeps its code prefix
    (``CAD 1,234.56``). Negatives are parenthesized (``($1,234.56)``), zero
    is explicit, and non-numeric input renders as an em dash, never a crash.
    """
    from decimal import Decimal, InvalidOperation

    code = (currency or "USD").upper()
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    quantized = amount.quantize(Decimal("0.01"))
    symbol = MONEY_SYMBOLS.get(code)
    text = f"{symbol}{abs(quantized):,.2f}" if symbol else f"{code} {abs(quantized):,.2f}"
    return f"({text})" if quantized < 0 else text


@register.filter
def aria_label(field, label=None):
    """Render a field with an accessible name only — no class change.

    For controls whose visible label is a column header or an icon (the
    formset DELETE / is_active checkboxes). Defaults to the field's label.
    """
    if hasattr(field, "as_widget"):
        return field.as_widget(attrs={"aria-label": str(label or field.label)})
    return field


@register.filter(name="get_form_field")
def get_form_field(form, field_name):
    """Get a form field by name."""
    try:
        return form[field_name]
    except KeyError:
        return None


@register.simple_tag(takes_context=True)
def plugin_slot(context, slot):
    """Render every plugin ``TemplateExtension`` that targets ``slot``.

    Each extension's template is rendered, in ``order``, with the page's own
    context (so a product-profile panel sees ``product``, ``request``,
    ``perms``…) plus ``plugin_extension``, the extension itself. No plugin
    targets the slot → renders nothing. Slots a page offers are listed in
    docs/plugins.md, "Template slots".
    """
    from core.plugins import plugin_registry

    rendered = []
    for extension in plugin_registry.get_template_extensions(slot):
        tpl = context.template.engine.get_template(extension.template_name)
        with context.push(plugin_extension=extension):
            rendered.append(tpl.render(context))
    return mark_safe("".join(rendered))


@register.simple_tag
def list_cell(obj, col):
    """One registry column's cell (``core.list_columns``): definition columns
    show choice labels, plugin columns their ``format``, field columns the
    model's display value. Unknown renders "—" (spec 5.2: listed, not hidden).
    Always escaped."""
    from django.template.defaultfilters import date as date_filter
    from django.utils.html import format_html

    from core import list_columns

    if col.source == "definition":
        text = list_columns.display_value(col, list_columns.raw_value(col, obj))
    else:
        value = list_columns.raw_value(col, obj) if col.source == "plugin" else get_field(obj, col.field)
        if value is None or value == "":
            text = ""
        elif col.format == "boolean" or isinstance(value, bool):
            text = "Yes" if value else "No"
        elif col.format == "datetime":
            text = date_filter(value, "M j, Y H:i")
        elif col.format == "date":
            text = date_filter(value, "M j, Y")
        elif col.format == "money":
            text = money(value)
        else:
            text = str(value)
    return format_html('<span class="text-sm">{}</span>', text or "—")


@register.simple_tag
def once_field():
    """The hidden ``_once`` token a money-moving or mail-sending form posts.

    Minted per render; ``core.once.claim`` refuses a second POST carrying the
    same token (F-033, double submit).
    """
    return format_html('<input type="hidden" name="{}" value="{}">', once.ONCE_FIELD, once.mint())
