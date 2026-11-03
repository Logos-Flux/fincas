from django import forms

from .models import Customer, CustomerAddress

DUPLICATE_EMAIL = "A customer with this email already exists."
NO_TENANT_STAFF = (
    "Your staff account has no company, so the customer would have no tenant. "
    "Ask an administrator to set the company on your profile."
)


def _check_email(instance, email, company_id):
    """``uniq_customer_email_ci_company`` for forms that do not carry both
    fields (Django skips a constraint whose fields are not all on the form).

    ``company_id`` None is the only Company, never the portal tenant: these
    are back-office forms (phase4-fields.md 6, item 2)."""
    if company_id is None:
        from core.views import sole_company

        only = sole_company()
        company_id = only.pk if only is not None else None
    if company_id is None:
        raise forms.ValidationError(NO_TENANT_STAFF, code="no_tenant")
    exclude = None if instance._state.adding else instance.pk
    if Customer.email_taken(email, company_id, exclude_pk=exclude):
        raise forms.ValidationError(DUPLICATE_EMAIL, code="uniq_customer_email_ci_company")


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            # Basic info
            "first_name", "last_name", "email", "phone",
            "company_name", "job_title",
            # Classification
            "customer_type", "pricing_tier",
            # Tax exemption
            "is_tax_exempt", "tax_exempt_reason",
            "tax_exempt_certificate_number", "tax_exempt_document_url",
            "tax_exempt_expiration",
            # Preferences
            "is_active", "accepts_marketing",
            # Sales defaults
            "campaign", "lead_source", "channel",
            "default_sales_rep", "default_commission_rate",
            "default_currency", "default_price_list",
            # Notes
            "notes",
        ]
        widgets = {
            "tax_exempt_expiration": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def clean_email(self):
        email = self.cleaned_data.get("email")
        _check_email(self.instance, email, self.instance.company_id)
        return email


class CustomerAddressForm(forms.ModelForm):
    class Meta:
        model = CustomerAddress
        fields = [
            "address_type", "is_default", "label",
            "first_name", "last_name", "company_name",
            "address_line_1", "address_line_2",
            "city", "state_province", "postal_code", "country",
            "phone",
            "is_residential", "has_loading_dock", "has_forklift",
            "requires_liftgate", "delivery_instructions",
        ]
        widgets = {
            "delivery_instructions": forms.Textarea(attrs={"rows": 3}),
        }


class CustomerMoreInfoForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "company", "campaign", "lead_source", "channel",
            "default_sales_rep", "default_commission_rate",
            "default_currency", "default_price_list",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company"].required = True  # NOT NULL since CRM Phase 2 step 11

    def clean_company(self):
        company = self.cleaned_data.get("company")
        if company is not None and company.pk != self.instance.company_id:
            _check_email(self.instance, self.instance.email, company.pk)
        return company
