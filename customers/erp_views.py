import json

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, UpdateView

from core.html_safety import display_html as safe_display_html
from core.views import staff_create_company
from core.views.form_view import ERPCreateView, ERPUpdateView
from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin
from orders.models import Order, Quote

from .forms import NO_TENANT_STAFF, CustomerAddressForm, CustomerForm, CustomerMoreInfoForm
from .models import Customer, CustomerAddress, CustomerContact

# -- Customer List --------------------------------------------------------

class CustomerListView(ERPListView):
    model = Customer
    current_section = "customers"
    page_title = "Customers"
    entity_name = "Customer"
    entity_label_key = "customers.customer"
    page_title_format = "{plural}"
    create_url_name = "erp-customer-create"
    ordering = ["last_name", "first_name"]
    search_fields = ["first_name", "last_name", "email", "company__name"]
    list_columns = [
        {"field": "full_name", "label": "Name", "link": True, "link_url_name": "erp-customer-detail"},
        # id "email" keeps the column's preference key, sort id and CSV header;
        # the cell reads display_email, which is empty for an erased customer.
        {"id": "email", "field": "display_email", "label": "Email"},
        {"field": "company_name", "label": "Company"},
        {"field": "customer_type", "label": "Type", "badge": True},
        {"field": "pricing_tier", "label": "Pricing Tier", "badge": True},
        {"field": "is_active", "label": "Active", "bool_badge": True},
    ]
    # ``full_name`` is a property, so it sorts by the name columns behind it.
    sortable_fields = {
        "full_name": ("last_name", "first_name"),
        "email": ("email",),
        "company_name": ("company_name",),
        "customer_type": ("customer_type",),
        "pricing_tier": ("pricing_tier",),
        "is_active": ("is_active",),
    }
    list_filters = [
        {"field": "customer_type", "label": "Type", "choices": Customer.CustomerType.choices},
        {"field": "pricing_tier", "label": "Pricing Tier", "choices": Customer.PricingTier.choices},
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("company")


# -- Customer Detail (Profile) -------------------------------------------

class CustomerDetailView(ERPViewMixin, DetailView):
    model = Customer
    template_name = "erp/customer_detail.html"
    context_object_name = "customer"
    current_section = "customers"
    page_title = "Customer Profile"

    def get_queryset(self):
        return Customer.objects.select_related(
            "company", "default_sales_rep", "default_price_list", "party",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.profile_context(self.object, context.get("more_info_form")))
        return context

    @classmethod
    def profile_context(cls, customer, more_info_form=None):
        """Everything ``erp/customer_detail.html`` reads besides ``customer``.

        Shared with ``CustomerMoreInfoView``, which re-renders the same
        template on GET and on an invalid POST: without this it rendered
        with only the form, and the ``status_color`` filter raised
        ``VariableDoesNotExist`` on ``status_colors`` (a 500 the access
        matrix found).
        """
        context = {}
        context["addresses"] = customer.addresses.all()

        context["recent_orders"] = (
            Order.objects.filter(customer=customer)
            .order_by("-created_at")[:10]
        )
        context["recent_quotes"] = (
            Quote.objects.filter(customer=customer)
            .order_by("-created_at")[:10]
        )

        context.update(cls._relationships(customer))

        context["more_info_form"] = more_info_form or CustomerMoreInfoForm(instance=customer)
        context["status_colors"] = ERPListView.STATUS_COLORS
        context["page_title"] = str(customer)
        return context

    @staticmethod
    def _relationships(customer):
        """The Relationships card: the customer's party and its relationships.

        Add and edit open the party relationship forms with ``next`` back to
        this card, so staff can link the customer to another party without
        leaving the profile.
        """
        from parties.erp_views import party_relationships

        party = customer.party
        groups, past = party_relationships(party)
        return {
            "party": party,
            "party_editable": not party.merged_into_id and not party.erased_at,
            "relationship_groups": groups,
            "past_relationships": past,
            "relationships_next": reverse("erp-customer-detail", args=[customer.pk]) + "#relationships",
        }


# -- Customer Create / Edit -----------------------------------------------

class CustomerFormMixin:
    model = Customer
    form_class = CustomerForm
    current_section = "customers"
    entity_name = "Customer"
    entity_label_key = "customers.customer"
    success_url_name = "erp-customer-list"
    form_sections = [
        {
            "title": "Basic Information",
            "fields": ["first_name", "last_name", "email", "phone", "company_name", "job_title"],
            "collapsible": False,
        },
        {
            "title": "Classification",
            "fields": ["customer_type", "pricing_tier"],
            "collapsible": False,
        },
        {
            "title": "Tax Exemption",
            "fields": [
                "is_tax_exempt", "tax_exempt_reason",
                "tax_exempt_certificate_number", "tax_exempt_document_url",
                "tax_exempt_expiration",
            ],
            "collapsible": True,
        },
        {
            "title": "Preferences",
            "fields": ["is_active", "accepts_marketing"],
            "collapsible": False,
        },
        {
            "title": "Sales Defaults",
            "fields": [
                "campaign", "lead_source", "channel",
                "default_sales_rep", "default_commission_rate",
                "default_currency", "default_price_list",
            ],
            "collapsible": True,
        },
        {
            "title": "Notes",
            "fields": ["notes"],
            "collapsible": True,
        },
    ]


class CustomerCreateView(CustomerFormMixin, ERPCreateView):
    page_title = "New Customer"
    page_title_format = "New {singular}"

    def get_form(self, form_class=None):
        # The requester's tenant, as in the quick-create below. Left blank,
        # ``Customer.save`` filed it under the portal tenant; with no tenant
        # the form refuses (``no_tenant``) and nothing is written.
        form = super().get_form(form_class)
        if form.instance.company_id is None:
            company = staff_create_company(self.request.user)
            if company is not None:
                form.instance.company = company
        return form


class CustomerUpdateView(CustomerFormMixin, ERPUpdateView):
    page_title = "Edit Customer"
    page_title_format = "Edit {singular}"


# -- Customer Address CRUD ------------------------------------------------

class CustomerAddressCreateView(ERPViewMixin, CreateView):
    model = CustomerAddress
    form_class = CustomerAddressForm
    template_name = "erp/customer_address_form.html"
    current_section = "customers"
    page_title = "New Address"

    def form_valid(self, form):
        form.instance.customer = get_object_or_404(Customer, pk=self.kwargs["customer_pk"])
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("erp-customer-detail", args=[self.kwargs["customer_pk"]]) + "#addresses"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["customer"] = get_object_or_404(Customer, pk=self.kwargs["customer_pk"])
        return context


class CustomerAddressUpdateView(ERPViewMixin, UpdateView):
    model = CustomerAddress
    form_class = CustomerAddressForm
    template_name = "erp/customer_address_form.html"
    current_section = "customers"
    page_title = "Edit Address"

    def get_success_url(self):
        return reverse("erp-customer-detail", args=[self.object.customer_id]) + "#addresses"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["customer"] = self.object.customer
        return context


class CustomerAddressDeleteView(ERPViewMixin, View):
    def post(self, request, pk):
        address = get_object_or_404(CustomerAddress, pk=pk)
        customer_pk = address.customer_id
        address.delete()
        return redirect(reverse("erp-customer-detail", args=[customer_pk]) + "#addresses")


# -- Customer More Info (inline form save) --------------------------------

class CustomerMoreInfoView(ERPViewMixin, UpdateView):
    """The More info card's form, posted from the customer profile.

    Renders the profile itself (GET, or a POST with errors), so it needs
    the profile's whole context, not just the form.
    """

    model = Customer
    form_class = CustomerMoreInfoForm
    template_name = "erp/customer_detail.html"
    context_object_name = "customer"
    current_section = "customers"

    def get_queryset(self):
        return CustomerDetailView.get_queryset(self)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(CustomerDetailView.profile_context(self.object, context["form"]))
        return context

    def get_success_url(self):
        return reverse("erp-customer-detail", args=[self.object.pk]) + "#more-info"


# -- Address AJAX Create ---------------------------------------------------

class CustomerCreateAjaxView(LoginRequiredMixin, UserPassesTestMixin, View):
    """AJAX endpoint to create a customer from the quote/order popup modal."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        first_name = (data.get("first_name") or "").strip()
        last_name = (data.get("last_name") or "").strip()
        email = (data.get("email") or "").strip()

        if not first_name:
            return JsonResponse({"error": "First name is required"}, status=400)
        if not email:
            return JsonResponse({"error": "Email is required"}, status=400)
        # The requester's own tenant (phase4-fields.md 6, item 2). Left blank,
        # the party sync filed the customer under the portal company, which on
        # a multi-company instance is not the tenant that created it. A login
        # with no profile company (the demo user, a fresh createsuperuser) is
        # unambiguous only on a single-company instance.
        company = staff_create_company(request.user)
        if company is None:
            return JsonResponse(
                {"error": NO_TENANT_STAFF, "code": "no_tenant"}, status=400,
            )
        # uniq_customer_email_ci_company: per tenant, case-insensitive (step 11).
        if Customer.email_taken(email, company.pk):
            return JsonResponse({"error": "A customer with this email already exists"}, status=400)

        customer = Customer.objects.create(
            company=company,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=(data.get("phone") or "").strip(),
            company_name=(data.get("company_name") or "").strip(),
            customer_type=data.get("customer_type", "individual"),
        )

        return JsonResponse({
            "id": str(customer.pk),
            "label": customer.full_name,
        })


class AddressCreateAjaxView(LoginRequiredMixin, UserPassesTestMixin, View):
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        customer_id = data.get("customer")
        if not customer_id:
            return JsonResponse({"error": "Customer is required"}, status=400)

        try:
            customer = Customer.objects.get(pk=customer_id)
        except Customer.DoesNotExist:
            return JsonResponse({"error": "Customer not found"}, status=404)

        # "Same as Billing" — copy an existing address as a new shipping address
        copy_from = data.get("copy_from")
        if copy_from:
            try:
                source = CustomerAddress.objects.get(pk=copy_from, customer=customer)
            except CustomerAddress.DoesNotExist:
                return JsonResponse({"error": "Source address not found"}, status=404)
            address = CustomerAddress.objects.create(
                customer=customer,
                address_type="shipping",
                label=data.get("label", f"Shipping — {source.label or source.address_line_1}"),
                first_name=source.first_name,
                last_name=source.last_name,
                company_name=source.company_name,
                address_line_1=source.address_line_1,
                address_line_2=source.address_line_2,
                city=source.city,
                state_province=source.state_province,
                postal_code=source.postal_code,
                country=source.country,
                phone=source.phone,
            )
        else:
            required = ["address_line_1", "city", "state_province", "postal_code"]
            for field in required:
                if not data.get(field):
                    return JsonResponse({"error": f"{field} is required"}, status=400)

            address = CustomerAddress.objects.create(
                customer=customer,
                address_type=data.get("address_type", "both"),
                label=data.get("label", ""),
                first_name=data.get("first_name", ""),
                last_name=data.get("last_name", ""),
                company_name=data.get("company_name", ""),
                address_line_1=data["address_line_1"],
                address_line_2=data.get("address_line_2", ""),
                city=data["city"],
                state_province=data["state_province"],
                postal_code=data["postal_code"],
                country=data.get("country", "US"),
                phone=data.get("phone", ""),
            )

        name = " ".join(filter(None, [address.first_name, address.last_name]))
        display = address.label or name or f"{address.address_line_1}, {address.city}"
        # Build display HTML for inline preview (values escaped —
        # this fragment is injected via innerHTML on the document form).
        lines = list(filter(None, [
            name, address.company_name, address.address_line_1, address.address_line_2,
            f"{address.city}, {address.state_province} {address.postal_code}",
            address.phone,
        ]))
        display_html = safe_display_html(lines)
        return JsonResponse({"id": str(address.pk), "label": display, "display_html": display_html})


class AddressDetailAjaxView(LoginRequiredMixin, UserPassesTestMixin, View):
    """GET /erp/customers/addresses/<uuid>/detail-ajax/ — return address fields for editing."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request, pk):
        try:
            a = CustomerAddress.objects.get(pk=pk)
        except CustomerAddress.DoesNotExist:
            return JsonResponse({"error": "Address not found"}, status=404)
        return JsonResponse({
            "id": str(a.pk),
            "address_type": a.address_type,
            "label": a.label,
            "first_name": a.first_name,
            "last_name": a.last_name,
            "company_name": a.company_name,
            "address_line_1": a.address_line_1,
            "address_line_2": a.address_line_2,
            "city": a.city,
            "state_province": a.state_province,
            "postal_code": a.postal_code,
            "country": a.country,
            "phone": a.phone,
        })


class AddressUpdateAjaxView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST /erp/customers/addresses/<uuid>/update-ajax/ — update an existing address."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        try:
            address = CustomerAddress.objects.get(pk=pk)
        except CustomerAddress.DoesNotExist:
            return JsonResponse({"error": "Address not found"}, status=404)

        # Update fields
        for field in [
            "address_type", "label", "first_name", "last_name", "company_name",
            "address_line_1", "address_line_2", "city", "state_province",
            "postal_code", "country", "phone",
        ]:
            if field in data:
                setattr(address, field, data[field])

        required = ["address_line_1", "city", "state_province", "postal_code"]
        for field in required:
            if not getattr(address, field):
                return JsonResponse({"error": f"{field} is required"}, status=400)

        address.save()

        name = " ".join(filter(None, [address.first_name, address.last_name]))
        display = address.label or name or f"{address.address_line_1}, {address.city}"
        lines = list(filter(None, [
            name, address.company_name, address.address_line_1, address.address_line_2,
            f"{address.city}, {address.state_province} {address.postal_code}",
            address.phone,
        ]))
        display_html = safe_display_html(lines)
        return JsonResponse({"id": str(address.pk), "label": display, "display_html": display_html})


class CustomerSearchAjaxView(UserPassesTestMixin, View):
    """GET /erp/customers/search-ajax/?q=...&customer=... — lightweight search."""

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request):
        q = request.GET.get("q", "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})

        qs = Customer.objects.filter(is_active=True)
        qs = qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
            | Q(company_name__icontains=q)
        )[:15]
        results = [
            {
                "id": str(c.pk),
                "label": f"{c.first_name} {c.last_name}".strip() or c.email,
                "company": c.company_name or "",
            }
            for c in qs
        ]
        return JsonResponse({"results": results})


class AddressSearchAjaxView(UserPassesTestMixin, View):
    """GET /erp/customers/address-search-ajax/?customer=...&q=... — search addresses for a customer."""

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request):
        customer_id = request.GET.get("customer", "").strip()
        if not customer_id:
            return JsonResponse({"results": []})

        qs = CustomerAddress.objects.filter(customer_id=customer_id).only(
            "id", "label", "address_type", "first_name", "last_name",
            "address_line_1", "city", "state_province", "postal_code",
            "is_default",
        )
        q = request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(label__icontains=q)
                | Q(address_line_1__icontains=q)
                | Q(city__icontains=q)
                | Q(postal_code__icontains=q)
            )
        results = []
        for a in qs[:20]:
            name = " ".join(filter(None, [a.first_name, a.last_name]))
            label = a.label or f"{a.address_line_1}, {a.city}"
            results.append({
                "id": str(a.pk),
                "label": f"{name} — {label}" if name else label,
                "type": a.address_type,
                "is_default": a.is_default,
            })
        return JsonResponse({"results": results})


class CustomerDefaultsAjaxView(UserPassesTestMixin, View):
    """GET /erp/customers/<uuid>/defaults/ — return default addresses + display HTML for a customer."""

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request, pk):
        addrs = CustomerAddress.objects.filter(customer_id=pk).only(
            "id", "label", "address_type", "is_default",
            "first_name", "last_name", "company_name",
            "address_line_1", "address_line_2",
            "city", "state_province", "postal_code", "phone",
        ).order_by("-is_default", "label")

        default_billing = None
        default_shipping = None
        addresses = []
        details = {}

        for a in addrs:
            aid = str(a.pk)
            name = " ".join(filter(None, [a.first_name, a.last_name]))
            lbl = a.label or f"{a.address_line_1}, {a.city}"
            option_label = f"{name} — {lbl}" if name else lbl

            addresses.append({
                "id": aid,
                "label": option_label,
                "type": a.address_type,
                "is_default": a.is_default,
            })

            # Build display HTML
            lines = list(filter(None, [
                name, a.company_name, a.address_line_1, a.address_line_2,
                f"{a.city}, {a.state_province} {a.postal_code}",
                a.phone,
            ]))
            details[aid] = safe_display_html(lines)

            # Auto-select defaults
            if a.is_default:
                if a.address_type in ("billing", "both") and not default_billing:
                    default_billing = aid
                if a.address_type in ("shipping", "both") and not default_shipping:
                    default_shipping = aid

        return JsonResponse({
            "addresses": addresses,
            "details": details,
            "default_billing": default_billing,
            "default_shipping": default_shipping,
        })


class ContactSearchAjaxView(LoginRequiredMixin, UserPassesTestMixin, View):
    """GET /erp/customers/contact-search-ajax/?customer=...&q=... — search contacts for a customer."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request):
        customer_id = request.GET.get("customer", "").strip()
        if not customer_id:
            return JsonResponse({"results": []})

        qs = CustomerContact.objects.filter(customer_id=customer_id)

        q = request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(email__icontains=q)
            )

        results = [
            {
                "id": str(c.pk),
                "label": c.full_name,
                "email": c.email,
            }
            for c in qs[:20]
        ]
        return JsonResponse({"results": results})


class ContactCreateAjaxView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST /erp/customers/contacts/create-ajax/ — create a new contact for a customer."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        customer_id = data.get("customer")
        if not customer_id:
            return JsonResponse({"error": "Customer is required"}, status=400)

        try:
            customer = Customer.objects.get(pk=customer_id)
        except Customer.DoesNotExist:
            return JsonResponse({"error": "Customer not found"}, status=404)

        first_name = (data.get("first_name") or "").strip()
        if not first_name:
            return JsonResponse({"error": "First name is required"}, status=400)

        contact = CustomerContact.objects.create(
            customer=customer,
            first_name=first_name,
            last_name=(data.get("last_name") or "").strip(),
            email=(data.get("email") or "").strip(),
            phone=(data.get("phone") or "").strip(),
            job_title=(data.get("job_title") or "").strip(),
        )

        return JsonResponse({
            "id": str(contact.pk),
            "label": contact.full_name,
        })


class ContactDetailAjaxView(LoginRequiredMixin, UserPassesTestMixin, View):
    """GET /erp/customers/contacts/<uuid>/detail-ajax/ — return contact fields for editing."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request, pk):
        try:
            c = CustomerContact.objects.get(pk=pk)
        except CustomerContact.DoesNotExist:
            return JsonResponse({"error": "Contact not found"}, status=404)
        return JsonResponse({
            "id": str(c.pk),
            "first_name": c.first_name,
            "last_name": c.last_name,
            "email": c.email,
            "phone": c.phone,
            "job_title": c.job_title,
        })


class ContactUpdateAjaxView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST /erp/customers/contacts/<uuid>/update-ajax/ — update an existing contact."""
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        try:
            contact = CustomerContact.objects.get(pk=pk)
        except CustomerContact.DoesNotExist:
            return JsonResponse({"error": "Contact not found"}, status=404)

        for field in ["first_name", "last_name", "email", "phone", "job_title"]:
            if field in data:
                setattr(contact, field, (data[field] or "").strip())

        if not contact.first_name:
            return JsonResponse({"error": "First name is required"}, status=400)

        contact.save()
        return JsonResponse({
            "id": str(contact.pk),
            "label": contact.full_name,
        })
