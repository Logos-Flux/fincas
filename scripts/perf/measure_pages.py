"""Real-volume page timings and query counts (docs/release-hardening-plan.md section 8).

Two measurements, one script:

* **Query counts** are taken in-process with the Django test client and a
  ``connection.execute_wrapper`` (``CaptureQueriesContext`` is reset by
  ``request_started`` outside the test runner). Every list view is also run
  at two page sizes (10 and 100 rows) to catch an N+1: a page whose query
  count grows with its row count fails whatever its absolute number.
* **Timings** are taken over HTTP against a gunicorn started by the caller
  (``scripts/perf/README.md``), with a staff session cookie: ``--warmups``
  requests are discarded, then ``--requests`` sequential requests give
  p50 / p95 / max per URL.

Budgets: the caps pinned in ``core/tests/test_query_counts.py``,
``parties/tests/test_erp_views.py`` and ``crm/tests/test_erp_views.py`` for the
pages they cover; every other page must not grow with its row count; p95
under ``--p95-budget`` ms (500). Numbers are host-relative.

Usage (settings and DATABASE_URL in the environment, see the README)::

    python scripts/perf/measure_pages.py --label 1x --base http://127.0.0.1:8830 --out .runs/perf/1x.json

``--no-http`` skips the timings (query counts only); ``--no-queries`` skips
the in-process pass. Nothing here prints a name, an amount or an address:
the output is URLs, counts and milliseconds, plus the shape of the slowest
query (its SQL with literals replaced).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.contrib.sessions.backends.db import SessionStore  # noqa: E402
from django.db import connection  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.urls import reverse  # noqa: E402

STAFF_USERNAME = "perf-staff"
SEARCH = "mar"  # a three-letter query; matching rows change nothing about the count

#: Query caps pinned by the test suite (page key -> cap). Everything else is
#: judged on growth only.
CAPS = {
    "dashboard": 15,
    "order-list": 10,
    "order-edit": 25,
    "product-list": 10,
    "product-detail": 15,
    "party-list": 15,
    "engagement-list?chip=open": 14,
}

LIST_VIEWS = [
    # (key, url name)
    ("order-list", "erp-order-list"),
    ("quote-list", "erp-quote-list"),
    ("product-list", "erp-product-list"),
    ("brand-list", "erp-brand-list"),
    ("category-list", "erp-category-list"),
    ("customer-list", "erp-customer-list"),
    ("party-list", "erp-party-list"),
    ("engagement-list", "erp-crm-engagement-list"),
    ("task-list", "erp-crm-task-list"),
    ("lead-list", "erp-crm-lead-list"),
    ("purchaseorder-list", "erp-purchaseorder-list"),
    ("supplier-list", "erp-supplier-list"),
    ("pickticket-list", "erp-pickticket-list"),
    ("warehouse-list", "erp-warehouse-list"),
    ("pricelist-list", "erp-pricelist-list"),
    ("invoice-list", "erp-invoice-list"),
    ("document-library", "erp-document-library"),
    ("discount-list", "erp-discount-list"),
    ("article-list", "erp-article-list"),
    ("crm-import-job-list", "erp-crm-import-job-list"),
]

OTHER_PAGES = [
    ("dashboard", "erp-dashboard", ""),
    ("my-day", "erp-crm-my-day", ""),
    ("user-list", "erp-user-list", ""),
    ("teams", "erp-settings-teams", ""),
    ("pipeline-list", "erp-crm-pipeline-list", ""),
    ("integrations", "erp-integrations", ""),
    ("unify-batch-list", "erp-unify-batch-list", ""),
    ("chat-sessions", "chat:sessions", ""),
    ("party-merge-review", "erp-party-merge-review", ""),
    ("accounting-dashboard", "erp-accounting-dashboard", ""),
    ("accounting-entries", "erp-accounting-entries", ""),
    ("accounting-periods", "erp-accounting-periods", ""),
    ("accounting-trial-balance", "erp-accounting-trial-balance", ""),
    ("accounting-reconciliation", "erp-accounting-reconciliation", ""),
    ("report-list", "erp-report-list", ""),
]

SALES_REPORTS = ["sales_summary", "order_status", "inventory_levels", "quote_conversion", "accounts_receivable"]
CRM_REPORTS = [
    ("report-crm-funnel", "erp-report-crm-funnel"),
    ("report-crm-stages", "erp-report-crm-stages"),
    ("report-crm-follow-ups", "erp-report-crm-follow-ups"),
    ("report-crm-attribution", "erp-report-crm-attribution"),
]

AJAX_SEARCHES = [
    ("search-variant", "erp-variant-search"),
    ("search-customer", "erp-customer-search-ajax"),
    ("search-address", "erp-address-search-ajax"),
    ("search-contact", "erp-contact-search-ajax"),
    ("search-party", "erp-party-search"),
    ("search-supplier", "erp-supplier-search-ajax"),
    ("search-warehouse", "erp-warehouse-search-ajax"),
]


# -- fixtures ------------------------------------------------------------------


def ensure_staff_user():
    """The measuring login: a non-superuser staff account pinned to the (only)
    company, so every ``for_request`` scope has rows. Password is random and
    never printed; the HTTP pass uses a session row, not a login form."""
    from core.models import Company, UserProfile
    from core.roles import grant_role

    company = Company.objects.order_by("created_at").first()
    if company is None:
        sys.exit("no Company in this database")
    user, created = User.objects.get_or_create(
        username=STAFF_USERNAME, defaults={"email": f"{STAFF_USERNAME}@example.invalid"},
    )
    if created:
        user.set_password(secrets.token_urlsafe(24))
        user.save()
    grant_role(user, "staff")
    profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"company": company})
    if profile.company_id != company.id:
        profile.company = company
        profile.save(update_fields=["company"])
    return user


def session_cookie(user) -> str:
    store = SessionStore()
    store[SESSION_KEY] = str(user.pk)
    store[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    store[HASH_SESSION_KEY] = user.get_session_auth_hash()
    store.create()
    return store.session_key


def sample_ids() -> dict:
    """Detail-page targets: ``{which: ((busiest pk, related rows), (leanest pk, related rows))}``.
    The busiest row makes the page do the most work; the leanest one is the
    second point of the growth check (a detail page whose query count rises
    with its related rows has an N+1)."""
    from crm.models import Engagement
    from customers.models import Customer
    from orders.models import Order, Quote
    from parties.models import Party
    from products.models import Product

    def ends(qs, *counts):
        qs = qs.annotate(**{f"n{i}": Count(c, distinct=True) for i, c in enumerate(counts)})
        keys = [f"n{i}" for i in range(len(counts))]
        most = qs.order_by(*[f"-{k}" for k in keys], "pk").values_list("pk", "n0").first()
        least = qs.order_by(*keys, "pk").values_list("pk", "n0").first()
        return most, least

    return {
        "engagement": ends(Engagement.objects.all(), "stage_changes", "communications", "tasks"),
        "party": ends(Party.objects.filter(merged_into__isnull=True), "emails", "addresses"),
        "customer": ends(Customer.objects.all(), "addresses"),
        "order": ends(Order.objects.all(), "line_items"),
        "quote": ends(Quote.objects.all(), "line_items"),
        "product": ends(Product.objects.all(), "variants"),
    }


def page_set() -> list[dict]:
    """``[{key, url, kind}]``: every list (plain and searched), search box,
    report and the detail pages the plan names."""
    ids = sample_ids()
    pages = []
    for key, name in LIST_VIEWS:
        url = reverse(name)
        pages.append({"key": key, "url": url, "kind": "list"})
        pages.append({"key": f"{key}?q", "url": f"{url}?q={SEARCH}", "kind": "list"})
    pages.append({"key": "engagement-list?chip=open", "url": reverse("erp-crm-engagement-list") + "?chip=open",
                  "kind": "list"})
    for key, name, query in OTHER_PAGES:
        pages.append({"key": key, "url": reverse(name) + query, "kind": "page"})
    for report in SALES_REPORTS:
        pages.append({"key": f"report-{report}", "url": f"{reverse('erp-report-list')}?report={report}&period=year",
                      "kind": "report"})
    for key, name in CRM_REPORTS:
        pages.append({"key": key, "url": f"{reverse(name)}?period=year", "kind": "report"})
    for key, name in AJAX_SEARCHES:
        pages.append({"key": key, "url": f"{reverse(name)}?q={SEARCH}", "kind": "search"})
    details = [
        ("engagement-detail", "erp-crm-engagement-detail", "engagement"),
        ("engagement-timeline", "erp-crm-engagement-timeline", "engagement"),
        ("party-detail", "erp-party-detail", "party"),
        ("customer-detail", "erp-customer-detail", "customer"),
        ("order-edit", "erp-order-edit", "order"),
        ("quote-edit", "erp-quote-edit", "quote"),
        ("product-detail", "erp-product-detail", "product"),
    ]
    for key, name, which in details:
        most, least = ids[which]
        if most is None:
            continue
        pages.append({"key": key, "url": reverse(name, args=[most[0]]), "kind": "detail", "rows": most[1],
                      "lean_url": reverse(name, args=[least[0]]), "rows@lean": least[1]})
    return pages


# -- query counting -------------------------------------------------------------


_LITERAL = re.compile(r"'(?:[^']|'')*'|\b\d+(?:\.\d+)?\b")
_COLUMNS = re.compile(r"^SELECT (DISTINCT )?(.*?) FROM ")


def shape(sql: str) -> str:
    """The SQL with literals replaced by ``?``, a long column list folded to
    ``SELECT <table>.* FROM``, whitespace collapsed, cut to 400 chars. The
    WHERE / JOIN / ORDER BY part is what tells one query from another."""
    out = _LITERAL.sub("?", sql)
    out = re.sub(r"\s+", " ", out).strip()
    m = _COLUMNS.match(out)
    if m and m.group(2).count(",") >= 3:
        table = re.search(r'"(\w+)"\."\w+"', m.group(2))
        out = f"SELECT {m.group(1) or ''}\"{table.group(1) if table else '?'}\".* FROM " + out[m.end():]
    return out[:400]


class QueryLog:
    def __init__(self):
        self.count = 0
        self.total_ms = 0.0
        self.slowest_ms = 0.0
        self.slowest_sql = ""
        self.shapes: dict[str, int] = {}

    def __call__(self, execute, sql, params, many, context):
        t0 = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            self.count += 1
            self.total_ms += ms
            if ms > self.slowest_ms:
                self.slowest_ms, self.slowest_sql = ms, sql
            s = shape(sql)
            self.shapes[s] = self.shapes.get(s, 0) + 1

    @property
    def repeated(self) -> list[tuple[str, int]]:
        """Shapes that ran more than twice in one request: the N+1 signature."""
        return sorted(((s, n) for s, n in self.shapes.items() if n > 2), key=lambda x: -x[1])


@contextmanager
def forced_page_size(size: int | None):
    """Make every ERPListView paginate at ``size`` (bypassing the viewer's
    preference), so two runs differ only in row count."""
    from core.views.list_view import ERPListView

    if size is None:
        yield
        return
    original = ERPListView.get_paginate_by
    ERPListView.get_paginate_by = lambda self, queryset: size
    try:
        yield
    finally:
        ERPListView.get_paginate_by = original


def count_queries(client: Client, url: str, size: int | None = None) -> dict:
    log = QueryLog()
    with forced_page_size(size), connection.execute_wrapper(log):
        t0 = time.perf_counter()
        response = client.get(url, HTTP_HOST="127.0.0.1")
        wall_ms = (time.perf_counter() - t0) * 1000
    rows = None
    ctx = getattr(response, "context", None)
    if ctx is not None:
        try:
            obj = ctx.get("object_list")
            if obj is not None:
                rows = len(obj)
        except Exception:  # a context without the key, or a non-sized object
            rows = None
    return {
        "status": response.status_code,
        "queries": log.count,
        "rows": rows,
        "db_ms": round(log.total_ms, 1),
        "inproc_ms": round(wall_ms, 1),
        "slowest_ms": round(log.slowest_ms, 1),
        "slowest_sql": shape(log.slowest_sql),
        "repeated": log.repeated[:3],
    }


def query_pass(pages: list[dict], user) -> None:
    setup_test_environment()
    client = Client()
    client.force_login(user)
    for page in pages:
        client.get(page["url"], HTTP_HOST="127.0.0.1")  # warm caches (llmhub settings, labels)
        base = count_queries(client, page["url"])
        if base["rows"] is None and page.get("rows") is not None:
            base["rows"] = page["rows"]
        page.update(base)
        if page["kind"] == "detail":
            lean = count_queries(client, page["lean_url"])
            page["queries@lean"] = lean["queries"]
            # A detail page may branch on its data (a next task, a shipment
            # group), so a few extra queries are shape, not growth; a rise of
            # more than three with a shape that repeats is an N+1.
            page["grows"] = page["queries"] > lean["queries"] + 3 and bool(page["repeated"])
        elif page["kind"] == "list":
            small = count_queries(client, page["url"], 10)
            large = count_queries(client, page["url"], 100)
            page["queries@10"] = small["queries"]
            page["rows@10"] = small["rows"]
            page["queries@100"] = large["queries"]
            page["rows@100"] = large["rows"]
            page["grows"] = large["queries"] > small["queries"]
            if large["queries"] > small["queries"]:
                page["repeated"] = large["repeated"][:3]
                page["slowest_sql"] = large["slowest_sql"]
        else:
            page["grows"] = None
        sys.stderr.write(f"  queries {page['key']:34} {page['status']} q={page['queries']}"
                         f"{'' if page['grows'] is None else ' grows' if page['grows'] else ' flat'}\n")


# -- HTTP timings -------------------------------------------------------------


def time_pass(pages: list[dict], base: str, cookie: str, warmups: int, requests: int) -> None:
    parts = urlsplit(base)
    host, port = parts.hostname, parts.port or 80
    headers = {"Cookie": f"{settings.SESSION_COOKIE_NAME}={cookie}", "Host": f"{host}:{port}",
               "Accept": "text/html,application/json"}
    conn = http.client.HTTPConnection(host, port, timeout=120)
    for page in pages:
        samples = []
        status = None
        for i in range(warmups + requests):
            t0 = time.perf_counter()
            conn.request("GET", page["url"], headers=headers)
            resp = conn.getresponse()
            resp.read()
            ms = (time.perf_counter() - t0) * 1000
            status = resp.status
            if i >= warmups:
                samples.append(ms)
        samples.sort()
        page["http_status"] = status
        page["p50"] = round(statistics.median(samples), 1)
        page["p95"] = round(samples[max(0, int(round(0.95 * len(samples))) - 1)], 1)
        page["max"] = round(samples[-1], 1)
        sys.stderr.write(f"  http    {page['key']:34} {status} p50={page['p50']} p95={page['p95']} max={page['max']}\n")
    conn.close()


# -- verdicts and report ---------------------------------------------------------


def judge(pages: list[dict], p95_budget: float) -> None:
    for page in pages:
        reasons = []
        cap = CAPS.get(page["key"])
        if page.get("status") not in (None, 200):
            reasons.append(f"status {page['status']}")
        if page.get("http_status") not in (None, 200):
            reasons.append(f"http {page['http_status']}")
        if cap is not None and page.get("queries") is not None and page["queries"] > cap:
            reasons.append(f"queries {page['queries']} > cap {cap}")
        if page.get("grows") and page["kind"] == "detail":
            reasons.append(f"queries grow with rows ({page['queries@lean']} with {page['rows@lean']} related rows, "
                           f"{page['queries']} with {page['rows']})")
        elif page.get("grows"):
            reasons.append(f"queries grow with rows ({page['queries@10']} at 10 rows, {page['queries@100']} at 100)")
        if page.get("p95") is not None and page["p95"] > p95_budget:
            reasons.append(f"p95 {page['p95']} ms > {p95_budget:g}")
        page["budget"] = f"<= {cap} queries" if cap else "no growth"
        page["verdict"] = "FAIL" if reasons else "pass"
        page["reasons"] = reasons


def markdown(pages: list[dict], label: str) -> str:
    lines = [f"| key | URL | rows | queries | q@small | q@100 | p50 ms | p95 ms | max ms | budget | {label} |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    order = sorted(pages, key=lambda p: (p["verdict"] != "FAIL", p["key"]))
    for p in order:
        small = p.get("queries@10", p.get("queries@lean", ""))
        lines.append(
            f"| {p['key']} | `{p['url']}` | {p.get('rows') if p.get('rows') is not None else ''} | "
            f"{p.get('queries', '')} | {small} | {p.get('queries@100', '')} | "
            f"{p.get('p50', '')} | {p.get('p95', '')} | {p.get('max', '')} | {p['budget']} | "
            f"{p['verdict']}{(' - ' + '; '.join(p['reasons'])) if p['reasons'] else ''} |"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8830", help="gunicorn base URL")
    ap.add_argument("--label", default="run")
    ap.add_argument("--requests", type=int, default=30)
    ap.add_argument("--warmups", type=int, default=3)
    ap.add_argument("--p95-budget", type=float, default=500.0)
    ap.add_argument("--out", help="JSON results file")
    ap.add_argument("--markdown", help="write the table here as well")
    ap.add_argument("--no-http", action="store_true")
    ap.add_argument("--no-queries", action="store_true")
    ap.add_argument("--only", help="regex on page keys")
    args = ap.parse_args(argv)

    user = ensure_staff_user()
    pages = page_set()
    if args.only:
        pages = [p for p in pages if re.search(args.only, p["key"])]
    sys.stderr.write(f"{len(pages)} pages, settings {os.environ['DJANGO_SETTINGS_MODULE']}, "
                     f"DEBUG={settings.DEBUG}, db={connection.settings_dict['NAME']}\n")
    if not args.no_queries:
        query_pass(pages, user)
    if not args.no_http:
        cookie = session_cookie(user)
        time_pass(pages, args.base, cookie, args.warmups, args.requests)
    judge(pages, args.p95_budget)

    result = {
        "label": args.label,
        "settings": os.environ["DJANGO_SETTINGS_MODULE"],
        "debug": settings.DEBUG,
        "database": connection.settings_dict["NAME"],
        "requests": args.requests,
        "warmups": args.warmups,
        "p95_budget_ms": args.p95_budget,
        "pages": pages,
    }
    table = markdown(pages, args.label)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2, default=str))
    if args.markdown:
        Path(args.markdown).write_text(table + "\n")
    print(table)
    failures = [p for p in pages if p["verdict"] == "FAIL"]
    print(f"\n{len(pages)} pages, {len(failures)} over budget")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
