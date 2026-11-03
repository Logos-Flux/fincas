"""``{% label %}`` / ``|label`` — staff-UI noun labels (CRM Phase 3, §3.3).

    {% load labels %}
    <h1>{% label "crm.engagement" plural=True %}</h1>
    <a class="btn">New {% label "crm.engagement" case="lower" %}</a>
    <p>{% label "crm.engagement" article=True %} needs a next action.</p>
    {% label "orders.quote" plural=True as quotes_label %}
    {{ "crm.stage"|label }}  {{ "crm.stage"|label:"plural" }}

The tag resolves the full chain (tenant → deployment → default) from the
request's company. The **filter cannot see the request** (Django filters get
no context), so it resolves deployment → default only: prefer the tag
wherever a tenant may have reworded the noun.

Staff UI only. A PDF, email or other machine-facing template must not load
this library (``core/tests/test_labels_never_leak.py``).
"""

from __future__ import annotations

from django import template
from django.template.base import token_kwargs
from django.utils.html import conditional_escape

from core import label_registry
from core.labels import CASES, LabelNamespace, get_label, labels_for_request, render

register = template.Library()

_OPTIONS = ("plural", "article", "case")


def _context_company_id(context) -> str | None:
    namespace = context.get("labels")
    if not isinstance(namespace, LabelNamespace):
        namespace = labels_for_request(context.get("request"))
    return namespace.company_id


class LabelNode(template.Node):
    def __init__(self, key, options, asvar):
        self.key = key
        self.options = options
        self.asvar = asvar

    def render(self, context):
        key = self.key.resolve(context)
        opts = {name: value.resolve(context) for name, value in self.options.items()}
        case = opts.get("case") or "as_is"
        if case not in CASES:
            raise template.TemplateSyntaxError(
                f"{{% label %}} case={case!r}; must be one of {', '.join(CASES)}."
            )
        text = render(
            get_label(key, company=_context_company_id(context)),
            plural=bool(opts.get("plural")),
            article=bool(opts.get("article")),
            case=case,
        )
        if self.asvar:
            context[self.asvar] = text  # escaped when {{ var }} renders it
            return ""
        # Node output is not autoescaped by Django; labels will become
        # staff-edited data (core.LabelOverride), so escape here.
        return conditional_escape(text) if context.autoescape else text


@register.tag(name="label")
def do_label(parser, token):
    bits = token.split_contents()
    tag = bits.pop(0)
    if not bits:
        raise template.TemplateSyntaxError(f"{{% {tag} %}} needs a label key.")
    asvar = None
    if len(bits) >= 2 and bits[-2] == "as":
        asvar = bits[-1]
        bits = bits[:-2]
    if not bits:
        raise template.TemplateSyntaxError(f"{{% {tag} %}} needs a label key.")
    key = parser.compile_filter(bits.pop(0))
    literal = key.token[1:-1] if key.token[:1] in "\"'" and key.token[-1:] == key.token[:1] else None
    if literal is not None and not key.filters and not label_registry.is_label_key(literal):
        raise template.TemplateSyntaxError(f"{{% {tag} %}} unknown label key {literal!r}.")
    options = token_kwargs(bits, parser, support_legacy=False)
    if bits:
        raise template.TemplateSyntaxError(
            f"{{% {tag} %}} got unexpected argument(s) {' '.join(bits)!r}; "
            f"options are {', '.join(_OPTIONS)} (as name=value)."
        )
    unknown = sorted(set(options) - set(_OPTIONS))
    if unknown:
        raise template.TemplateSyntaxError(
            f"{{% {tag} %}} unknown option(s) {', '.join(unknown)}; allowed: {', '.join(_OPTIONS)}."
        )
    return LabelNode(key, options, asvar)


@register.filter(name="label")
def label_filter(key, form="singular"):
    """``{{ "crm.stage"|label }}`` / ``|label:"plural"`` — deployment layer only (see module doc)."""
    if form not in ("singular", "plural"):
        raise template.TemplateSyntaxError(
            f'|label:{form!r} must be "singular" or "plural".'
        )
    return render(get_label(key), plural=form == "plural")
