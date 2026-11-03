# Money and date formatting (as the code does it)

The rules below are read from the code as of v0.4.0, not decided here: the
release harness asserts them, and a screen that disagrees with them is a bug.
Each rule cites the file that
implements it. Inconsistencies found while reading are listed at the end as
divergences and are not fixed by this document.

## 1. Money

### 1.1 Back-office screens: the `money` filter

`core/templatetags/erp_tags.py`, `money(value, currency="USD")`, used as
`{{ amount|money }}` or `{{ amount|money:"CAD" }}` at 56 sites in 16
templates (accounting, CRM engagements, customer and party detail, the
dashboard, discounts, price lists, product detail and profile, and the
generic list row `golden_erp/templates/erp/list.html`).

| Input | Output | Rule |
|---|---|---|
| `1234.5`, USD | `$1,234.50` | USD is the only currency with a symbol (`MONEY_SYMBOLS = {"USD": "$"}`); two decimals always; `,` thousands separator, `.` decimal point (Python `:,.2f`, locale-independent) |
| `1234.5`, CAD | `CAD 1,234.50` | Any other code is a prefix, so 100 CAD never reads as $100 |
| `-1234.5`, USD | `($1,234.50)` | Negatives are parenthesized, never `-$` |
| `0` | `$0.00` | Zero is explicit |
| `None`, `""`, text | `—` (em dash) | Non-numeric input renders a dash, never a crash |
| `1.005` | `$1.00` | `Decimal.quantize(Decimal("0.01"))` under the default context: round half to even |

Before v0.4.0 the same filter rendered USD as
`USD 1,234.56` and negatives as `USD (1,234.56)`; the `$` form is what
v0.4.0 ships.

Quantities are not money: `|floatformat:0` (PDF lines, emails, the pick
ticket, price-list tiers), `|floatformat:1` for dimensions and weights
(`erp/product_detail.html`), and `|intcomma` for counts (accounting
dashboard, import queue headers).

### 1.2 PDFs

`documents/templates/documents/base_document.html` (lines, subtotal,
discount, shipping, tax, total) and `sales_invoice.html` (amount paid,
balance due, status banner): `${{ value|floatformat:2|intcomma }}`, a
hard-coded `$`, two decimals, thousands separators; the discount line is
`−$x` (U+2212). The `money` filter is not used in PDFs (divergence D1).

### 1.3 Emails

- `communications/templates/communications/emails/order_confirmation.html`
  and `.txt`: `${{ value|floatformat:2 }}` for lines, subtotal, discount
  (`-$x`), tax, shipping and total: **no thousands separator** (D2).
- `invoice_email.html` and `quote_email.html` print `${{ grand_total }}`,
  where `grand_total` arrives pre-formatted as `f"{x:,.2f}"` from
  `communications/services.py` (with separators). The invoice body text in
  the same file uses `f"${invoice.balance_due:.2f}"` (no separator, D2).

### 1.4 Reports

- CRM reports (`reporting/crm_reports.py`, `money(amount, currency)`):
  `$1,200.00` for USD, `€`/`£` for EUR/GBP, and **`1,200.00 CAD`** (code as
  a suffix) for anything else; never converted (`no FX`). The CSV form of a
  multi-currency total (`csv()`) is `1200.00 USD; ≥50.00 CAD` without
  separators (D3).
- The older commerce reports (`reporting/erp_views.py`, sales summary and
  accounts receivable): table cells `f"${total:.2f}"` (no separator), KPI
  tiles `f"${total:,.2f}"` (with), `"$0"` for an empty average (D4). Their
  CSV export writes those display strings, `$` included.

### 1.5 CSV exports

- CRM lists (`core/list_export.py`, `canonical()`; the `csv_canonical`
  lists): a `Decimal` is `format(value, "f")`, so `1234.56` with no symbol,
  no separator and the field's own scale; `None` is an empty cell; booleans
  are `true`/`false`; lists are `;`-joined. Headers are column ids, never
  labels.
- Commerce lists (`core/views/list_view.py`, `export_csv` when
  `csv_canonical` is off) and the accounting trial balance and
  reconciliation CSVs (`accounting/erp_views.py`): the Python value through
  `csv.writer`, i.e. `str(Decimal)` (`1234.56`) with no symbol.

### 1.6 API

Django REST Framework serializes `DecimalField`s as strings
(`COERCE_DECIMAL_TO_STRING`, the default; `golden_erp/settings/base.py`
`REST_FRAMEWORK` does not override it): `"1234.56"`. The storefront catalog
serializer formats explicitly (`products/serializers.py`,
`get_price`: `f"{price:.2f}"` or `null`). The storefront never invents a
price: `null` renders "Price on request".

## 2. Dates and times

### 2.1 Zone

- `golden_erp/settings/base.py`: `TIME_ZONE = "America/New_York"`,
  `USE_TZ = True`, `USE_I18N = True`, `LANGUAGE_CODE = "en-us"`. Instants
  are stored in UTC.
- Nothing calls `django.utils.timezone.activate`, so every `|date` and
  `|time` in a template converts an aware datetime to `America/New_York`,
  whatever the viewer's profile says (D5).
- The CRM resolves a **viewer zone** for *computation*: `core/tz.py`
  `resolve(profile, company)` picks `UserProfile.timezone`, else
  `Company.timezone`, else `settings.TIME_ZONE`, else UTC (IANA names,
  validated; a stale name is skipped, never raised). `crm/erp_views.py`
  `local_today(zone)` is "today" for due-date buckets, the My Day page and
  the month boundaries; a date-sensitive test is checked at 23:30 and
  03:30 UTC, when the UTC date and the local date differ. Report date ranges use
  `settings.TIME_ZONE`.
- Form defaults for "now" are written in the viewer zone
  (`crm/erp_views.py`: `now.astimezone(zone).strftime("%Y-%m-%dT%H:%M")`).

### 2.2 Formats in templates

Every `|date` in the templates carries an explicit format (no bare `|date`,
so Django's locale default is never used). Formats and where they are used:

| Format | Example | Used in |
|---|---|---|
| `M j, Y` | Oct 7, 2026 | the generic list date column (`erp/list.html`), `erp/accounting_dashboard.html`, `crm/lead_list.html`, `crm/engagement_detail.html`, `parties/party_detail.html`, PDFs `documents/purchase_order.html` and `documents/sales_quote.html` (date, valid until). 27 uses: the de facto default |
| `M d, Y` | Oct 07, 2026 | `erp/customer_detail.html`, `erp/dashboard.html`, `erp/pick_ticket_print.html` (D6) |
| `N j, Y` | Oct. 7, 2026 | `emails/order_confirmation.html`/`.txt`, `erp/account.html`, `erp/user_list.html` (D6) |
| `F j, Y` | October 7, 2026 | PDF `documents/sales_invoice.html` (date, due, paid), `documents/sales_quote.html` ("Pricing held until", D7), `erp/accounting_entry_detail.html` |
| `Y-m-d` | 2026-10-07 | accounting register, entries, dashboard; `erp/report.html`, `erp/crm_report.html` |
| `Y-m-d H:i` | 2026-10-07 14:05 | `crm_import/job_overview.html`, `erp/integrations.html`, `parties_merge/review.html`, `crm/lead_list.html`, `parties/party_list.html`; `Y-m-d H:i T` (zone abbreviation) in `crm/engagement_list.html` |
| `M j, Y g:i A` | Oct 7, 2026 2:05 PM | `erp/document_form.html`, `erp/product_profile.html`, `erp/_tracking_section.html` (D8) |
| `M j, Y H:i` | Oct 7, 2026 14:05 | `crm/engagement_detail.html`, `parties/party_detail.html`; `M d, Y H:i` in `discounts/detail.html`; `M j, H:i` in `llmhub/settings.html` and `chat/drawer.html` (D8) |
| `D M j`, `D M j, H:i`, `M j` | Wed Oct 7 | CRM timeline, task list, My Day, engagement detail, party detail (same-year dates) |
| `l, F j` | Wednesday, October 7 | My Day heading |
| `H:i`, `|time:"H:i"` | 14:05 | CRM communications and timeline, import queue rows, My Day |
| `c` | 2026-10-07T14:05:00+00:00 | `chat/drawer.html` (machine-readable attribute) |

Python-side: `communications/services.py` formats the invoice date as
`strftime("%B %d, %Y")` (October 07, 2026: zero-padded day, D7);
`orders/erp_views.py` uses `strftime('%b %d, %Y')` in a confirmation prompt.

### 2.3 CSV and API

- CRM list CSV (`core/list_export.py`): dates `YYYY-MM-DD`; aware
  datetimes converted to UTC and written ISO 8601 with a `Z` suffix
  (`2026-10-07T18:05:00Z`); naive datetimes ISO without a zone.
- Commerce and accounting CSVs: `str()` of the Python value, so an aware
  datetime is `2026-10-07 18:05:00+00:00` (D9).
- Report CSV file names: `<report>_<date_from>_<date_to>.csv` with
  `%Y-%m-%d` (`reporting/erp_views.py`).
- API (`reporting/api.py`, every DRF serializer): ISO 8601 (`date.isoformat()`,
  DRF's default `DATETIME_FORMAT`), in UTC.

## 3. Divergences (not fixed here)

- **D1** PDFs hard-code `$` with `floatformat:2|intcomma` instead of
  `|money`: a negative total would print `$-1,234.56`, not `($1,234.56)`,
  and a non-USD document still shows `$`. Rounding also differs: Django's
  `floatformat` rounds half up, `money` rounds half to even (Decimal
  default), so a 3-decimal computed value can differ by a cent between a
  screen and its PDF.
- **D2** Emails: `order_confirmation` and the invoice body have no thousands
  separator; `invoice_email`/`quote_email` do (pre-formatted in
  `communications/services.py`).
- **D3** Non-USD placement: screens prefix the code (`CAD 1,234.56`), CRM
  reports suffix it (`1,234.56 CAD`), their CSV drops the separator
  (`1234.56 CAD`); CRM reports symbolize EUR and GBP, the filter only USD.
- **D4** `reporting/erp_views.py` legacy reports: cells without and tiles
  with separators; their CSV carries `$` strings (not canonical).
- **D5** Templates render datetimes in `settings.TIME_ZONE`
  (America/New_York) because no request activates the viewer's zone, while
  CRM "today" and form defaults use `core.tz.resolve` (the module docstring
  of `crm/erp_views.py` says "times render in the viewer's zone"; only the
  computations do).
- **D6** Three short-date styles coexist: `M j, Y` (27 uses), `M d, Y`
  (zero-padded, 6) and `N j, Y` (AP abbreviation with a period, 4).
- **D7** `sales_quote.html` uses `M j, Y` for its header dates and `F j, Y`
  in the "pricing held until" sentence; the invoice email's
  `strftime("%B %d, %Y")` zero-pads the day where `F j, Y` does not.
- **D8** 12-hour `g:i A` (document form, product profile, tracking) and
  24-hour `H:i` (CRM, parties, imports, integrations) both appear.
- **D9** Commerce/accounting CSVs write `str(datetime)` (`+00:00`, a space
  separator) where the canonical exporter writes ISO with `Z`; the
  reconciliation CSV (`accounting/erp_views.py`) does not neutralise
  formula-shaped cells (plan 9.8 covers CSV escaping).
