"""Access matrix (docs/release-hardening-plan.md section 6.1, criterion 4).

Every API viewset and every ``/erp/`` model view, by action, and every other
route, are driven as each principal the deployment knows:

- ``anonymous``;
- ``superuser`` (break-glass);
- ``staff`` and one ``role:<name>`` per further ``staff=True`` role in
  ``GOLDEN_ROLES`` (this module adds ``clerk``, a staff role declaring no
  permission, so a scoped role is always driven);
- two portal customers, ``portal:a`` and ``portal:b`` (object ownership);
- three service accounts: the ``storefront`` identity (bearer), a pinned
  ``service:matrix-reader`` key whose role declares ``purchasing.view_supplier``
  only, and the ``legacy-key`` (``API_KEY``, acting as the ``api-service``
  superuser; API only, it has no session).

Expectations never come from the code under test: they are derived from
``docs/roles.md`` and ``core/tests/access_matrix_expectations.py`` (the
per-model overrides and the route classification dict). A failure names the
row it contradicts and points at ``docs/release-testing.md#access-matrix``.

Enumeration is ``core/access_matrix.py``: an unclassified route or model fails
with a copy-paste entry. Every write is sent inside a savepoint that is
rolled back, so the seeded rows are the same for every principal and no
case depends on the order the matrix runs in.

Not covered on purpose: cross-company isolation (section 11, decision 2: one
instance per company is the design; F-026, F-027 and F-028 are S2
limitations, not release criteria). The Django admin has its own auth and
is outside the matrix. ``core/tests/test_auth_matrix.py`` keeps the
permission-class audit and the credential mechanics (headers, expiry).
"""

from __future__ import annotations

import csv
import io
import re
import secrets
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core import access_matrix as am
from core.tests import access_matrix_expectations as exp
from core.tests.helpers import make_portal_customer, make_staff_user, portal_tenant
from core.tests.test_portal_auth import SECRET as PORTAL_SECRET
from core.tests.test_portal_auth import make_token as portal_token

DOC = "docs/release-testing.md#access-matrix"
EXPECTATIONS_PATH = "core/tests/access_matrix_expectations.py"
LEGACY_KEY = "test-" + secrets.token_hex(16)

#: A staff role that declares nothing (so ``perm:`` gates refuse it) and a
#: service role with one view permission: the "scoped" principals.
CLERK_ROLE = {"name": "clerk", "label": "Clerk", "staff": True}
READER_ROLE = {
    "name": "matrix-reader", "label": "Matrix reader", "kind": "service",
    "permissions": ["purchasing.view_supplier"],
}
MATRIX_ROLES = list(getattr(settings, "GOLDEN_ROLES", [])) + [CLERK_ROLE, READER_ROLE]

ALLOWED, DENIED = "allowed", "denied"

#: Server errors met on paths a principal is allowed to reach (printed after
#: the run; findings-log material, not access failures).
SERVER_ERRORS: list = []


# --- principals ---------------------------------------------------------------


@dataclass
class Principal:
    id: str
    kind: str  # anonymous | superuser | staff | portal | service | legacy
    roles: tuple = ()
    pinned: bool = False
    storefront: bool = False
    erp: Client | None = None
    api: APIClient | None = None
    user: User | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Spec:
    cls: str
    leaks: dict
    skip: str | None
    source: str
    human: bool = False

    @classmethod
    def parse(cls, text: str, source: str) -> "Spec":
        parts = [p.strip() for p in re.split(r";(?=\s*(?:leak|skip|human))", text) if p.strip()]
        klass, leaks, skip, human = "staff", {}, None, False
        for i, part in enumerate(parts):
            if part.startswith("leak:"):
                _, principal, fid = part.split(":", 2)
                leaks[principal] = fid
            elif part.startswith("skip:"):
                skip = part[len("skip:"):]
            elif part == "human":
                human = True
            elif i == 0:
                klass = part
            else:
                raise ValueError(f"{source}: unknown spec token {part!r}")
        if klass != "public" and klass not in ("staff", "superuser", "portal") and not klass.startswith("perm:"):
            raise ValueError(f"{source}: unknown class {klass!r}")
        return cls(klass, leaks, skip, source, human)


def declared_permissions(principal: Principal) -> set:
    """What the principal's roles declare in ``GOLDEN_ROLES`` (docs/roles.md)."""
    from core.role_registry import ROLES

    perms = set()
    for role in principal.roles:
        perms.update(ROLES[role].permissions)
    return perms


def expected_outcome(spec: Spec, principal: Principal, *, surface: str,
                     model: str | None = None, action: str | None = None) -> str:
    """``allowed`` / ``denied`` from the spec class and docs/roles.md alone."""
    if spec.cls == "public":
        return ALLOWED
    if spec.cls == "portal":
        return ALLOWED if principal.kind == "portal" else DENIED
    if principal.kind == "anonymous" or principal.kind == "portal":
        return DENIED
    if spec.human and principal.kind in ("service", "legacy"):
        return DENIED
    if principal.kind in ("superuser", "legacy"):
        return ALLOWED
    if spec.cls == "superuser":
        return DENIED
    if spec.cls.startswith("perm:"):
        return ALLOWED if spec.cls[len("perm:"):] in declared_permissions(principal) else DENIED
    # spec.cls == "staff"
    if principal.kind == "staff":
        return ALLOWED
    # a service account: its contract, API only (identity-unification 2.6)
    if surface != "api" or model is None:
        return DENIED
    verb = exp.CONTRACT_VERBS.get(action or "")
    if verb is None:
        return DENIED
    app_label, model_name = model.split(".")
    if f"{app_label}.{verb}_{model_name.lower()}" not in declared_permissions(principal):
        return DENIED
    if verb == "view":
        return ALLOWED
    if principal.storefront:
        return ALLOWED if verb == "add" else DENIED
    return ALLOWED if principal.pinned else DENIED


def observed_outcome(response) -> str:
    """What the server did: ``denied`` (401, 403, or a login redirect with
    ``?next=``), ``error:<status>`` (5xx), else ``allowed``."""
    status = response.status_code
    if status in (401, 403):
        return DENIED
    if status in (301, 302):
        location = response.get("Location", "")
        if "/erp/login/" in location and "next=" in location:
            return DENIED
    if status >= 500:
        return f"error:{status}"
    return ALLOWED


# --- seed ---------------------------------------------------------------------


def seed(company, customer_a, customer_b) -> dict:
    """One row per matrix model, keyed ``app.Model``; the portal customers'
    documents are keyed ``<label>:a`` / ``<label>:b`` for the ownership cases."""
    from accounting.models import FiscalPeriod
    from communications.models import Communication
    from content.models import Article
    from core.integrations.models import IntegrationAccount
    from core.models import AuditLog, Team, UserProfile
    from crm.models import (
        Engagement,
        EngagementContact,
        EngagementStageChange,
        Lead,
        Pipeline,
        Stage,
        Task,
        Touchpoint,
    )
    from crm_import.models import ImportJob
    from customers.models import CustomerAddress
    from discounts.models import DiscountCode
    from documents.models import ProductDocument
    from fulfillment.models import PickTicket, PickTicketLine
    from inventory.models import Warehouse, WarehouseStock
    from orders.models import Order, OrderLineItem, Quote, QuoteLineItem, SalesInvoice, SubOrder
    from parties.models import (
        ConsentRecord,
        Party,
        PartyAddress,
        PartyEmail,
        PartyPhone,
        PartyRelationship,
        PartyRole,
    )
    from payments.models import Payment
    from pricing.models import PriceList, PriceListItem, TaxRate
    from products.models import (
        Brand,
        Category,
        Product,
        ProductOption,
        ProductOptionValue,
        ProductVariant,
        SupplierProduct,
        VariantOptionValue,
    )
    from purchasing.models import POLineItem, POReceipt, PurchaseOrder, Supplier
    from reporting.models import ReportDefinition
    from webhooks.models import WebhookDeliveryLog, WebhookSubscription

    now = timezone.now()
    rows = {"core.Company": company}
    rows["core.Team"] = Team.objects.create(company=company, key="matrix", name="Matrix team")
    rows["core.AuditLog"] = AuditLog.objects.create(
        action="matrix", model_name="Matrix", object_id=uuid.uuid4())
    rows["core.IntegrationAccount"] = IntegrationAccount.objects.create(
        company=company, provider="matrix")

    brand = Brand.objects.create(title="Matrix brand")
    category = Category.objects.create(title="Matrix category")
    product = Product.objects.create(title="Matrix product", brand=brand)
    product.categories.add(category)
    variant = ProductVariant.objects.create(product=product, sku="MATRIX-1")
    option = ProductOption.objects.create(product=product, name="Size")
    option_value = ProductOptionValue.objects.create(option=option, value="L")
    rows.update({
        "products.Brand": brand, "products.Category": category, "products.Product": product,
        "products.ProductVariant": variant, "products.ProductOption": option,
        "products.ProductOptionValue": option_value,
        "products.VariantOptionValue": VariantOptionValue.objects.create(
            variant=variant, option_value=option_value),
    })
    supplier = Supplier.objects.create(
        supplier_code="MATRIX-SUP", company_name="Matrix supplier", supplier_type="distributor")
    rows["purchasing.Supplier"] = supplier
    rows["products.SupplierProduct"] = SupplierProduct.objects.create(
        variant=variant, supplier=supplier, cost=Decimal("1.00"))
    warehouse = Warehouse.objects.create(warehouse_code="MATRIX-WH", name="Matrix warehouse")
    rows["inventory.Warehouse"] = warehouse
    rows["inventory.WarehouseStock"] = WarehouseStock.objects.create(
        variant=variant, warehouse=warehouse)
    price_list = PriceList.objects.create(name="Matrix prices")
    rows["pricing.PriceList"] = price_list
    rows["pricing.PriceListItem"] = PriceListItem.objects.create(
        price_list=price_list, variant=variant, price=Decimal("10.00"))
    rows["pricing.TaxRate"] = TaxRate.objects.create(
        name="Matrix tax", rate=Decimal("0.1"), jurisdiction="Matrix")
    rows["documents.ProductDocument"] = ProductDocument.objects.create(
        product=product, document_type="manual", title="Matrix manual",
        r2_key="matrix/manual.pdf", r2_url="https://files.example.com/matrix/manual.pdf")
    rows["content.Article"] = Article.objects.create(
        title="Matrix article", slug="matrix-article", content="Hello.")
    rows["discounts.DiscountCode"] = DiscountCode.objects.create(
        code="MATRIX", value=Decimal("1.00"))
    rows["reporting.ReportDefinition"] = ReportDefinition.objects.create(
        name="Matrix report", query="SELECT 1")
    rows["accounting.FiscalPeriod"] = FiscalPeriod.objects.create(
        company=company, name="Matrix period", start_date=now.date(), end_date=now.date())
    subscription = WebhookSubscription.objects.create(target_url="https://hooks.example.com/matrix")
    rows["webhooks.WebhookSubscription"] = subscription
    rows["webhooks.WebhookDeliveryLog"] = WebhookDeliveryLog.objects.create(
        subscription=subscription, event_type="order.created", payload={})
    rows["crm_import.ImportJob"] = ImportJob.objects.create(
        company=company, source="matrix", provider="matrix")

    rows["core.UserProfile"] = UserProfile.objects.filter(company=company).first()
    rows["customers.Customer"] = customer_a
    rows["customers.CustomerAddress:a"] = rows["customers.CustomerAddress"] = (
        CustomerAddress.objects.create(
            customer=customer_a, address_line_1="1 Matrix St", city="Springfield",
            state_province="IL", postal_code="62701"))
    rows["customers.CustomerAddress:b"] = CustomerAddress.objects.create(
        customer=customer_b, address_line_1="2 Matrix St", city="Springfield",
        state_province="IL", postal_code="62701")
    for key, customer in (("a", customer_a), ("b", customer_b)):
        order = Order.objects.create(customer=customer, company=company)
        OrderLineItem.objects.create(
            order=order, item_name="Matrix product", sku=variant.sku, variant=variant,
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("10.00"))
        quote = Quote.objects.create(customer=customer, company=company)
        QuoteLineItem.objects.create(
            quote=quote, item_name="Matrix product", sku=variant.sku, variant=variant,
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("10.00"))
        invoice = SalesInvoice.objects.create(order=order, company=company)
        rows[f"orders.Order:{key}"] = order
        rows[f"orders.Quote:{key}"] = quote
        rows[f"orders.SalesInvoice:{key}"] = invoice
    order, quote = rows["orders.Order:a"], rows["orders.Quote:a"]
    rows["orders.Order"] = order
    rows["orders.OrderLineItem"] = order.line_items.first()
    rows["orders.SubOrder"] = SubOrder.objects.create(order=order)
    rows["orders.Quote"] = quote
    rows["orders.QuoteLineItem"] = quote.line_items.first()
    rows["orders.SalesInvoice"] = rows["orders.SalesInvoice:a"]
    rows["payments.Payment"] = Payment.objects.create(
        order=order, payment_method="cash", payment_provider="manual", amount=Decimal("1.00"))

    purchase_order = PurchaseOrder.objects.create(company=company, supplier=supplier)
    rows["purchasing.PurchaseOrder"] = purchase_order
    rows["purchasing.POLineItem"] = POLineItem.objects.create(
        purchase_order=purchase_order, variant=variant, item_name="Matrix product",
        quantity_ordered=Decimal("1"), unit_cost=Decimal("1.00"))
    rows["purchasing.POReceipt"] = POReceipt.objects.create(purchase_order=purchase_order)
    pick_ticket = PickTicket.objects.create()
    rows["fulfillment.PickTicket"] = pick_ticket
    rows["fulfillment.PickTicketLine"] = PickTicketLine.objects.create(
        pick_ticket=pick_ticket, quantity=Decimal("1"))

    # The customers' parties are mirrored from Customer rows, so their contact
    # points and roles are read-only by design (parties/erp_views.py
    # ``read_only``); the child rows come from a staff-entered organisation.
    party_a = customer_a.party
    org = Party.objects.create(
        company=company, kind="organisation", display_name="Matrix Org", origin="erp")
    rows["parties.Party"] = party_a
    rows["parties.Party:org"] = org
    rows["parties.PartyEmail"] = PartyEmail.objects.create(
        party=org, company=company, email="org@example.com", purpose="work")
    rows["parties.PartyPhone"] = PartyPhone.objects.create(
        party=org, company=company, number_raw="+15555550100", purpose="work")
    rows["parties.PartyAddress"] = PartyAddress.objects.create(
        party=org, company=company, purpose="billing", address_line_1="1 Matrix St",
        city="Springfield", state_province="IL", postal_code="62701", country="US")
    rows["parties.PartyRole"] = PartyRole.objects.create(
        party=org, company=company, role="supplier", valid_from=now.date())
    rows["parties.PartyRelationship"] = PartyRelationship.objects.create(
        company=company, from_party=party_a, to_party=org, kind="employment", is_symmetric=False)
    rows["parties.ConsentRecord"] = ConsentRecord.objects.create(
        company=company, party=party_a, channel="email", purpose="marketing", state="granted",
        occurred_at=now, source="erp")

    rows["crm.Pipeline"] = Pipeline.objects.filter(company=company).order_by("key").first()
    rows["crm.Stage"] = Stage.objects.filter(company=company).order_by("sort_order").first()
    engagement = Engagement.objects.filter(company=company).order_by("created_at").first()
    rows["crm.Engagement"] = engagement
    rows["crm.EngagementContact"] = EngagementContact.objects.create(
        engagement=engagement, company=company, party=party_a)
    rows["crm.EngagementStageChange"] = EngagementStageChange.objects.filter(
        engagement=engagement).first()
    rows["crm.Lead"] = Lead.objects.create(
        company=company, first_name="Lee", last_name="Matrix", email="lead-matrix@example.com")
    rows["crm.Task"] = Task.objects.create(company=company, title="Matrix task")
    rows["crm.Touchpoint"] = Touchpoint.objects.create(
        company=company, occurred_at=now, source="web", party=party_a)
    rows["communications.Communication"] = (
        Communication.objects.filter(company=company).first()
        or Communication.objects.create(
            company=company, kind="note", direction="internal", occurred_at=now)
    )
    return rows


# --- the matrix ------------------------------------------------------------------


@override_settings(
    API_KEY=LEGACY_KEY,
    SUPABASE_JWT_SECRET=PORTAL_SECRET,
    GOLDEN_ROLES=MATRIX_ROLES,
    STAFF_SESSION_SECRET="matrix-" + secrets.token_hex(8),
    STAFF_SSO_CALLBACK_URL="https://storefront.example.com/sso/callback",
)
class AccessMatrixTests(TestCase):
    maxDiff = None

    @classmethod
    def setUpTestData(cls):
        from core.models import UserProfile
        from core.service_accounts import ensure_service_account, issue_api_key
        from core.storefront import ensure_storefront_identity

        cls.company = portal_tenant()
        cls.superuser = User.objects.create_superuser("matrix-super", "su@example.com", "pw")
        # A profile (company) like a real administrator: actions that attribute
        # a change to a person (crm reopen) refuse a profile-less superuser.
        UserProfile.objects.create(user=cls.superuser, company=cls.company, display_name="su")
        cls.staff_users = {"staff": make_staff_user("matrix-staff", "staff@example.com", "pw")}
        for role in MATRIX_ROLES:
            if role.get("staff") and role["name"] not in cls.staff_users:
                cls.staff_users[role["name"]] = make_staff_user(
                    f"matrix-{role['name']}", f"{role['name']}@example.com", "pw",
                    role=role["name"])
        for user in cls.staff_users.values():
            UserProfile.objects.get_or_create(
                user=user, defaults={"company": cls.company, "display_name": user.username})
        cls.customer_a, cls.subject_a = make_portal_customer("matrix-a@example.com")
        cls.customer_b, cls.subject_b = make_portal_customer("matrix-b@example.com")
        cls.storefront_user = ensure_storefront_identity()
        cls.service_user = ensure_service_account(
            "matrix-reader", ["matrix-reader"], company=cls.company)
        _key, cls.service_raw_key = issue_api_key(cls.service_user, name="matrix")
        cls.rows = seed(cls.company, cls.customer_a, cls.customer_b)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if SERVER_ERRORS:
            unique = sorted(set(SERVER_ERRORS))
            print(  # noqa: T201 - a report, like the roles post_migrate line
                f"\naccess matrix: {len(unique)} server error(s) on allowed paths (not access "
                f"failures; log them in docs/findings-log.md):\n  " + "\n  ".join(unique))
            SERVER_ERRORS.clear()

    def setUp(self):
        cache.clear()  # login / token / OIDC throttles share the cache
        self.addCleanup(cache.clear)
        self.principals = self._principals()

    # -- principals ---------------------------------------------------------

    def _principals(self) -> list:
        from core.authentication import issue_api_jwt
        from core.service_accounts import STOREFRONT_SUBJECT

        def session(user):
            client = Client()
            client.force_login(user)
            api = APIClient()
            api.force_login(user)
            return client, api

        principals = [Principal("anonymous", "anonymous", erp=Client(), api=APIClient())]
        erp, api = session(self.superuser)
        principals.append(Principal("superuser", "superuser", erp=erp, api=api, user=self.superuser))
        for role, user in self.staff_users.items():
            erp, api = session(user)
            principals.append(Principal(
                "staff" if role == "staff" else f"role:{role}", "staff", roles=(role,),
                erp=erp, api=api, user=user))
        for key, customer, subject in (("a", self.customer_a, self.subject_a),
                                       ("b", self.customer_b, self.subject_b)):
            erp = Client()
            erp.force_login(customer.user)
            token = portal_token(sub=subject, email=customer.email, email_verified=True)
            api = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
            principals.append(Principal(
                f"portal:{key}", "portal", roles=("customer",), erp=erp, api=api,
                user=customer.user, extra={"customer": customer}))
        erp = Client()
        erp.force_login(self.storefront_user)
        token = issue_api_jwt(lifetime_s=3600, sub=STOREFRONT_SUBJECT)
        principals.append(Principal(
            "storefront", "service", roles=("storefront",), storefront=True, erp=erp,
            api=APIClient(HTTP_AUTHORIZATION=f"Bearer {token}"), user=self.storefront_user))
        erp = Client()
        erp.force_login(self.service_user)
        principals.append(Principal(
            "service:matrix-reader", "service", roles=("matrix-reader",), pinned=True, erp=erp,
            api=APIClient(HTTP_AUTHORIZATION=f"Api-Key {self.service_raw_key}"),
            user=self.service_user))
        principals.append(Principal(
            "legacy-key", "legacy", api=APIClient(HTTP_AUTHORIZATION=f"Api-Key {LEGACY_KEY}")))
        return principals

    def _refresh_client(self, principal: Principal):
        """Rebuild a principal's session client (after a route that ended it)."""
        if principal.user is None or principal.erp is None:
            return
        principal.erp = Client()
        principal.erp.force_login(principal.user)

    # -- requests ---------------------------------------------------------------

    def _request(self, principal: Principal, surface: str, method: str, url: str):
        """Send one request inside a savepoint that is always rolled back.

        Returns ``(response, outcome)``; an exception that escapes the view
        (a 500 the test client re-raises) is ``(None, "error:<Exception>")``.
        """
        client = principal.api if surface == "api" else principal.erp
        kwargs = {}
        if method != "get":
            if surface == "api":
                kwargs = {"data": {}, "format": "json"}
            else:
                kwargs = {"data": "{}", "content_type": "application/json"}
        try:
            with transaction.atomic():
                response = getattr(client, method)(url, **kwargs)
                if getattr(response, "streaming", False):
                    b"".join(response.streaming_content)
                transaction.set_rollback(True)
        except Exception as exc:  # noqa: BLE001 - reported as the row's outcome
            return None, f"error:{type(exc).__name__}: {str(exc)[:120]}"
        return response, observed_outcome(response)

    def _check(self, subject: str, spec: Spec, principal: Principal, surface: str,
               method: str, url: str, *, model=None, action=None):
        if spec.skip:
            self.skipTest(f"{subject}: {spec.skip}")
        if spec.cls == "public" and surface == "api" and principal.kind == "portal":
            # DRF authenticates before AllowAny: a portal bearer is not a
            # staff-API credential, so it is 401 there whatever the view
            # (test_auth_matrix.test_portal_jwt_rejected_on_staff_route).
            self.skipTest(f"{subject}: the portal bearer is not a staff-API credential")
        expected = expected_outcome(spec, principal, surface=surface, model=model, action=action)
        leak = spec.leaks.get(principal.id)
        if leak and expected == DENIED:
            expected = ALLOWED
        response, observed = self._request(principal, surface, method, url)
        status = response.status_code if response is not None else observed
        if leak and expected == ALLOWED and observed == DENIED:
            self.fail(
                f"{subject} as {principal.id}: got {status} (denied), but {spec.source} lists "
                f"it as known leak {leak}; the gap has closed, remove the leak entry and close "
                f"{leak} in docs/findings-log.md - see {DOC}")
        if observed.startswith("error:"):
            # The view ran, so the gate passed: an access answer of "allowed".
            # For an allowed principal that is a server bug, not an access
            # failure; it is collected and printed, and belongs in the
            # findings log. For a denied principal it is a leak.
            SERVER_ERRORS.append(f"{subject} as {principal.id}: {observed}")
            observed = ALLOWED
        if observed != expected:
            self.fail(
                f"{subject} as {principal.id}: got {status} ({observed}), expected {expected} "
                f"({spec.source}) - see {DOC}")

    # -- URL building -------------------------------------------------------------

    #: Placeholder per path converter: a value that resolves, so a request
    #: reaches the view's gate instead of the URL resolver's 404.
    _PLACEHOLDERS = {"uuid": None, "int": "1", "slug": "matrix", "str": "matrix", "path": "matrix"}
    _PARENT_PARAMS = {
        "party_pk": "parties.Party", "customer_pk": "customers.Customer",
        "pipeline_pk": "crm.Pipeline",
    }

    def _row(self, label: str):
        return self.rows.get(label)

    def _url(self, template: str, params, converters, *, model: str | None = None,
             options=None) -> str:
        options = options or {}
        values = {}
        for name in params:
            if name in options.get("params", {}):
                values[name] = options["params"][name]
            elif name in options.get("row", {}):
                values[name] = str(self._row(options["row"][name]).pk)
            elif name == "pk" and model is not None and self._row(model) is not None:
                values[name] = str(self._row(model).pk)
            elif name in self._PARENT_PARAMS:
                values[name] = str(self._row(self._PARENT_PARAMS[name]).pk)
            else:
                values[name] = self._PLACEHOLDERS.get(converters.get(name, "uuid")) or str(uuid.uuid4())
        return template.format(**values)

    # -- model expectations ----------------------------------------------------------

    def _model_spec(self, model: str, surface: str, action: str) -> Spec:
        entry = exp.MODEL_OVERRIDES[model]
        key = f"{surface}:{action}"
        if key in entry:
            return Spec.parse(entry[key], f"MODEL_OVERRIDES[{model!r}][{key!r}] = {entry[key]!r}")
        if surface in entry:
            return Spec.parse(entry[surface], f"MODEL_OVERRIDES[{model!r}][{surface!r}] = {entry[surface]!r}")
        return Spec.parse("staff", "docs/roles.md 'Built-in roles': back office and the staff "
                                   f"API are staff; MODEL_OVERRIDES[{model!r}] keeps the default")

    def _row_skip(self, model: str) -> str | None:
        row = exp.MODEL_OVERRIDES[model].get("row", "")
        if row.startswith("skip:"):
            return row[len("skip:"):]
        return None

    # -- completeness ------------------------------------------------------------------

    def test_every_other_route_is_classified(self):
        """(c) every route outside the viewsets and model views is in ROUTE_CLASSES."""
        routes = am.other_routes()
        names = {r.name for r in routes}
        missing = sorted({r.name: r for r in routes if r.name not in exp.ROUTE_CLASSES}.values(),
                         key=lambda r: r.name)
        if missing:
            lines = "\n".join(
                f'    "{r.name}": "staff",  # {r.template} ({r.view})' for r in missing)
            self.fail(
                f"{len(missing)} route(s) are not classified. Add to ROUTE_CLASSES in "
                f"{EXPECTATIONS_PATH} (class public / staff / superuser / perm:<app.codename> / "
                f"portal, see {DOC}):\n{lines}")
        stale = sorted(set(exp.ROUTE_CLASSES) - names)
        self.assertEqual(stale, [], f"ROUTE_CLASSES names routes that no longer exist; remove "
                                     f"them from {EXPECTATIONS_PATH}: {stale}")
        stale_options = sorted(set(exp.ROUTE_OPTIONS) - names)
        self.assertEqual(stale_options, [], f"ROUTE_OPTIONS names routes that no longer exist: "
                                             f"{stale_options}")

    def test_every_model_is_classified_and_seeded(self):
        """(a)/(b) every model has a MODEL_OVERRIDES entry and a row or an explicit skip."""
        models = sorted({e.model for e in am.api_viewsets()} | {v.model for v in am.erp_model_views()})
        missing = [m for m in models if m not in exp.MODEL_OVERRIDES]
        if missing:
            lines = "\n".join(f'    "{m}": {{"row": "seed"}},' for m in missing)
            self.fail(
                f"{len(missing)} model(s) are not classified. Add to MODEL_OVERRIDES in "
                f"{EXPECTATIONS_PATH} ({{}} keeps the docs/roles.md default; add a row to "
                f"seed() in core/tests/test_access_matrix.py or set \"row\" to "
                f"\"skip:<reason>\", see {DOC}):\n{lines}")
        stale = sorted(set(exp.MODEL_OVERRIDES) - set(models))
        self.assertEqual(stale, [], f"MODEL_OVERRIDES names models no view serves; remove them "
                                     f"from {EXPECTATIONS_PATH}: {stale}")
        unseeded = [
            m for m in models
            if exp.MODEL_OVERRIDES[m].get("row", "seed") == "seed" and self._row(m) is None
        ]
        self.assertEqual(unseeded, [], f"seed() made no row for these models; add one or mark "
                                        f"\"row\": \"skip:<reason>\" in {EXPECTATIONS_PATH}: "
                                        f"{unseeded}")

    # -- the sweeps -------------------------------------------------------------------------

    def test_api_viewsets(self):
        """(a) every viewset action, every principal."""
        for endpoint in am.api_viewsets():
            spec = self._model_spec(endpoint.model, "api", endpoint.action)
            subject = f"{endpoint.model} api:{endpoint.action} ({endpoint.method.upper()} {endpoint.template})"
            row_skip = self._row_skip(endpoint.model) if endpoint.detail else None
            url = self._url(endpoint.template, endpoint.params, endpoint.converters,
                            model=endpoint.model)
            for principal in self.principals:
                if principal.api is None:
                    continue
                with self.subTest(model=endpoint.model, action=endpoint.action,
                                  method=endpoint.method, principal=principal.id):
                    if row_skip:
                        self.skipTest(f"{endpoint.model}: no row ({row_skip})")
                    self._check(subject, spec, principal, "api", endpoint.method, url,
                                model=endpoint.model, action=endpoint.action)

    def test_erp_model_views(self):
        """(b) every /erp/ model view action, every principal (sessions)."""
        for view in am.erp_model_views():
            spec = self._model_spec(view.model, "erp", view.action)
            row_skip = self._row_skip(view.model) if "pk" in view.params else None
            url = self._url(view.template, view.params, view.converters, model=view.model)
            if view.action == "export":
                url += ("&" if "?" in url else "?") + "export=csv"
            for method in view.methods:
                subject = f"{view.model} erp:{view.action} ({method.upper()} {view.template})"
                for principal in self.principals:
                    if principal.erp is None:
                        continue
                    with self.subTest(model=view.model, action=view.action, method=method,
                                      principal=principal.id):
                        if row_skip:
                            self.skipTest(f"{view.model}: no row ({row_skip})")
                        self._check(subject, spec, principal, "erp", method, url,
                                    model=view.model, action=view.action)

    def test_other_routes(self):
        """(c) every classified route, every principal."""
        for route in am.other_routes():
            options = exp.ROUTE_OPTIONS.get(route.name, {})
            classes = exp.ROUTE_CLASSES[route.name]
            surface = "api" if route.template.startswith("/api/") else "erp"
            methods = options.get("methods", route.methods)
            url = self._url(route.template, route.params, route.converters, options=options)
            for method in methods:
                text = classes[method] if isinstance(classes, dict) else classes
                spec = Spec.parse(text, f"ROUTE_CLASSES[{route.name!r}] = {text!r}")
                subject = f"{route.name} ({method.upper()} {route.template})"
                for principal in self.principals:
                    client = principal.api if surface == "api" else principal.erp
                    if client is None:
                        continue
                    with self.subTest(route=route.name, method=method, principal=principal.id):
                        self._check(subject, spec, principal, surface, method, url)
                    if options.get("fresh_client") and method != "get":
                        self._refresh_client(principal)

    # -- object ownership -----------------------------------------------------------------

    def _portal(self, key: str) -> Principal:
        return next(p for p in self.principals if p.id == f"portal:{key}")

    def test_portal_customers_are_refused_each_others_documents(self):
        """Section 6.1: two portal customers, each refused the other's documents."""
        cases = [
            ("portal-order-detail", "get", "/api/v1/portal/customer/orders/{pk}/", "orders.Order"),
            ("portal-quote-detail", "get", "/api/v1/portal/customer/quotes/{pk}/", "orders.Quote"),
            ("portal-address-detail", "patch", "/api/v1/portal/customer/addresses/{pk}/",
             "customers.CustomerAddress"),
        ]
        for mine, other in (("a", "b"), ("b", "a")):
            principal = self._portal(mine)
            for name, method, template, label in cases:
                own = self.rows[f"{label}:{mine}"]
                theirs = self.rows[f"{label}:{other}"]
                with self.subTest(route=name, principal=principal.id, row="own"):
                    response, _ = self._request(principal, "api", method, template.format(pk=own.pk))
                    self.assertEqual(
                        response.status_code, 200,
                        f"{name} as {principal.id}: own row got {response.status_code}, expected "
                        f"200 (ROUTE_CLASSES[{name!r}] = 'portal') - see {DOC}")
                with self.subTest(route=name, principal=principal.id, row="other customer's"):
                    response, _ = self._request(principal, "api", method, template.format(pk=theirs.pk))
                    self.assertIn(
                        response.status_code, (403, 404),
                        f"{name} as {principal.id}: the other customer's row got "
                        f"{response.status_code}, expected 403/404 (docs/roles.md 'customer': "
                        f"the customer portal, i.e. their own documents) - see {DOC}")
            with self.subTest(route="portal-invoice-list", principal=principal.id):
                response, _ = self._request(principal, "api", "get", "/api/v1/portal/customer/invoices/")
                self.assertEqual(response.status_code, 200)
                numbers = {row["invoice_number"] for row in self._results(response)}
                self.assertIn(self.rows[f"orders.SalesInvoice:{mine}"].invoice_number, numbers)
                self.assertNotIn(
                    self.rows[f"orders.SalesInvoice:{other}"].invoice_number, numbers,
                    f"portal-invoice-list as {principal.id} lists the other customer's invoice "
                    f"- see {DOC}")
            with self.subTest(route="portal-order-list", principal=principal.id):
                response, _ = self._request(principal, "api", "get", "/api/v1/portal/customer/orders/")
                ids = {str(row["id"]) for row in self._results(response)}
                self.assertIn(str(self.rows[f"orders.Order:{mine}"].pk), ids)
                self.assertNotIn(str(self.rows[f"orders.Order:{other}"].pk), ids)

    @staticmethod
    def _results(response):
        data = response.json()
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def test_payment_link_needs_the_token(self):
        """The by-token invoice view is public by design; the token is the credential,
        so a customer holding another customer's invoice id (not its token) gets nothing."""
        invoice_b = self.rows["orders.SalesInvoice:b"]
        staff = next(p for p in self.principals if p.id == "staff")
        for principal in (self.principals[0], staff):  # anonymous, staff
            with self.subTest(principal=principal.id):
                response, _ = self._request(
                    principal, "api", "get", f"/api/v1/orders/sales-invoices/by-token/{invoice_b.pk}/")
                self.assertEqual(response.status_code, 404)
                response, _ = self._request(
                    principal, "api", "get",
                    f"/api/v1/orders/sales-invoices/by-token/{invoice_b.payment_token}/")
                self.assertEqual(response.status_code, 200, "the token itself must still work")

    # -- a scoped staff role ------------------------------------------------------------------

    def test_scoped_staff_role_is_refused_what_it_does_not_declare(self):
        """A ``staff=True`` role declaring no permission (``clerk``) is refused the
        ``perm:`` gates that ``staff`` and a superuser pass or fail per their own
        declarations: docs/roles.md 'Adding roles' (``permissions`` are the grant)."""
        clerk = next(p for p in self.principals if p.id == "role:clerk")
        staff = next(p for p in self.principals if p.id == "staff")
        superuser = next(p for p in self.principals if p.id == "superuser")
        team = self.rows["core.Team"]
        gates = [
            ("erp-settings-team-create", "get", "/erp/settings/teams/new/"),
            ("erp-settings-team-edit", "get", f"/erp/settings/teams/{team.pk}/edit/"),
            ("erp-party-erase", "post", f"/erp/parties/{self.rows['parties.Party'].pk}/erase/"),
        ]
        for name, method, url in gates:
            for principal in (clerk, staff):
                with self.subTest(route=name, principal=principal.id):
                    _resp, observed = self._request(principal, "erp", method, url)
                    self.assertEqual(observed, DENIED, f"{name} as {principal.id}: {observed} - see {DOC}")
            with self.subTest(route=name, principal="superuser"):
                _resp, observed = self._request(superuser, "erp", method, url)
                self.assertEqual(observed, ALLOWED)

    def test_scoped_staff_role_export_withholds_personal_data(self):
        """docs/roles.md: ``staff`` carries ``parties.export_pii``; a staff role without
        it gets the CRM CSV export minus the personal-data columns."""
        clerk = next(p for p in self.principals if p.id == "role:clerk")
        staff = next(p for p in self.principals if p.id == "staff")
        headers = {}
        for principal in (staff, clerk):
            with transaction.atomic():
                response = principal.erp.get("/erp/parties/?export=csv")
                body = b"".join(response.streaming_content) if response.streaming else response.content
                transaction.set_rollback(True)  # the export writes an AuditLog row
            self.assertEqual(response.status_code, 200, f"party export as {principal.id}")
            headers[principal.id] = next(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertTrue(set(headers["role:clerk"]) < set(headers["staff"]),
                        f"clerk export columns {headers['role:clerk']} are not a strict subset of "
                        f"staff's {headers['staff']} - see {DOC}")
        self.assertIn("email", headers["staff"])
        self.assertNotIn("email", headers["role:clerk"])
