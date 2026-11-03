from django.db import IntegrityError, models, transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Lower

from core.managers import CompanyScopedManager
from core.models import BaseModel

NO_TENANT = (
    "Customer has no tenant: set company, or GOLDEN_PORTAL_COMPANY when several "
    "Companies exist (phase2-party.md 2.4)"
)


class Customer(BaseModel):
    """Customer record (25 fields)."""

    class CustomerType(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        BUSINESS = "business", "Business"

    class PricingTier(models.TextChoices):
        RETAIL = "retail", "Retail"
        WHOLESALE = "wholesale", "Wholesale"

    class TaxExemptReason(models.TextChoices):
        RESALE = "resale", "Resale"
        GOVERNMENT = "government", "Government"
        NON_PROFIT = "non_profit", "Non-Profit"
        EDUCATION = "education", "Education"
        AGRICULTURAL = "agricultural", "Agricultural"
        OTHER = "other", "Other"

    customer_type = models.CharField(
        max_length=20, choices=CustomerType.choices, default=CustomerType.INDIVIDUAL,
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    # Unique per tenant, case-insensitively: ``uniq_customer_email_ci_company``
    # (phase2-party.md 2.2, step 11). Forms and the API check it through
    # ``email_taken`` because neither carries ``company``.
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=50, blank=True)
    company_name = models.CharField(max_length=255, blank=True, help_text="Customer's own company name")
    # The tenant (2.4). NOT NULL since step 11: a write that leaves it unset
    # gets ``GOLDEN_PORTAL_COMPANY`` / the only Company from the party sync.
    company = models.ForeignKey(
        "core.Company", on_delete=models.PROTECT, blank=True,
    )
    job_title = models.CharField(max_length=100, blank=True)
    pricing_tier = models.CharField(
        max_length=20, choices=PricingTier.choices, default=PricingTier.RETAIL, db_index=True,
    )

    # Tax exemption
    is_tax_exempt = models.BooleanField(default=False)
    tax_exempt_reason = models.CharField(
        max_length=20, choices=TaxExemptReason.choices, blank=True,
    )
    tax_exempt_certificate_number = models.CharField(max_length=100, blank=True)
    tax_exempt_document_url = models.URLField(blank=True)
    tax_exempt_expiration = models.DateField(null=True, blank=True)
    tax_exempt_states = models.JSONField(default=list, blank=True)

    is_active = models.BooleanField(default=True, db_index=True)
    accepts_marketing = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    stripe_customer_id = models.CharField(max_length=64, blank=True, db_index=True)

    # Sales defaults / More Info
    campaign = models.CharField(max_length=255, blank=True)
    lead_source = models.CharField(max_length=255, blank=True)
    channel = models.CharField(max_length=50, blank=True)
    default_sales_rep = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_customers",
    )
    default_commission_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
    )
    default_currency = models.CharField(max_length=3, default="USD", blank=True)
    default_price_list = models.ForeignKey(
        "pricing.PriceList", on_delete=models.SET_NULL, null=True, blank=True,
    )

    # Portal auth. ``user`` is this customer's own login (identity-unification
    # §2.1): set by the identity backfill, and from step 4 by the portal
    # resolver and invite views only. Never writable through the API.
    user = models.OneToOneField(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="customer",
    )
    # The legacy ``supabase_user_id`` column was dropped by identity step 8:
    # a portal login is ``user`` plus its ``UserIdentity`` rows.

    # eBay marketplace buyer identity. The stable match key for imported eBay
    # orders — buyer email aliases expire, usernames don't (apps/erp/ebay/).
    ebay_username = models.CharField(max_length=255, blank=True, db_index=True)

    # Migration provenance
    bc_id = models.CharField(max_length=64, blank=True, db_index=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)
    bc_customer_group = models.CharField(max_length=100, blank=True)

    last_order_at = models.DateTimeField(null=True, blank=True)

    # CRM Phase 2 (docs/crm/phase2-party.md 2.1). Written only by
    # ``parties.sync`` and the backfill, never through the API or forms.
    # ``party`` countersigns: the organisation for a business customer, the
    # person for an individual; NOT NULL since step 11, so ``save()`` resolves
    # it before the INSERT. ``account_holder`` is the person whose portal
    # login reaches this account: set only while the customer has a login
    # (``user``), NULL otherwise (stays nullable, 4.1).
    party = models.ForeignKey(
        "parties.Party", on_delete=models.PROTECT, blank=True,
        related_name="customer_accounts", editable=False,
    )
    account_holder = models.ForeignKey(
        "parties.Party", on_delete=models.PROTECT, null=True, blank=True,
        related_name="held_accounts", editable=False,
    )

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["last_name", "first_name"]
        constraints = [
            # One person holds at most one commerce account (2.1).
            models.UniqueConstraint(
                fields=["account_holder"], condition=Q(account_holder__isnull=False),
                name="uniq_customer_account_holder",
            ),
            # An individual's holder, when it has one, is the party itself.
            # The spec's form omits the NULL case; it is explicit here because
            # a customer without a login has no holder (step 5 decision).
            models.CheckConstraint(
                condition=Q(customer_type="business") | Q(account_holder__isnull=True)
                | Q(party=F("account_holder")),
                name="customer_individual_holder_is_party",
            ),
            # One commerce account per address per tenant, case-insensitively
            # (2.2). Replaced the instance-wide, case-sensitive unique=True.
            models.UniqueConstraint(
                Lower("email"), "company", name="uniq_customer_email_ci_company",
                violation_error_code="uniq_customer_email_ci_company",
                violation_error_message="A customer with this email already exists.",
            ),
        ]

    @staticmethod
    def tenant_id(company_id=None):
        """``company_id``, else the tenant ``save()`` assigns to a customer
        without one (``GOLDEN_PORTAL_COMPANY`` / the only Company), else None."""
        if company_id is not None:
            return company_id
        from core.models import Company
        from parties.mirror import default_company_id

        return default_company_id(Company)

    @classmethod
    def email_taken(cls, email, company_id, *, exclude_pk=None) -> bool:
        """Whether ``uniq_customer_email_ci_company`` would reject ``email``.

        ``company_id`` None means the tenant ``save()`` would assign. With no
        tenant at all this answers False: ``save()`` refuses that write anyway
        (callers check ``tenant_id`` to answer it cleanly).
        """
        company_id = cls.tenant_id(company_id)
        if not email or company_id is None:
            return False
        # The database lowers both sides, exactly as the index does (SQLite's
        # LOWER is ASCII-only, Python's is not).
        rows = cls._base_manager.annotate(email_ci=Lower("email")).filter(
            company_id=company_id, email_ci=Lower(Value(email)))
        if exclude_pk is not None:
            rows = rows.exclude(pk=exclude_pk)
        return rows.exists()

    #: What staff screens print for an erased customer's name (plain English,
    #: not the label layer: it also reaches __str__, documents and search).
    ERASED_DISPLAY_NAME = "Erased customer"

    @property
    def is_erased(self) -> bool:
        """Erasure left this row as a tombstone (``parties.erasure``): names
        blanked, email ``erased+<uuid>@invalid``. Never print that address."""
        from parties.erasure import is_erased_customer

        return is_erased_customer(self)

    @property
    def full_name(self):
        if self.is_erased:
            return self.ERASED_DISPLAY_NAME
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def display_email(self) -> str:
        """The email for staff screens: empty for an erased customer. The API
        and exports read ``email`` itself (a tombstone is data there)."""
        return "" if self.is_erased else self.email

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        from parties.sync import CUSTOMER_SYNC_FIELDS, run_guarded, sync_from_customer, wants_sync

        adding = self._state.adding
        # The party sync runs in the same transaction as the write (2.1).
        with transaction.atomic(using=kwargs.get("using")):
            # Step 11: ``company`` and ``party`` are NOT NULL. An unset
            # company is the portal tenant (2.4), and a new customer's parties
            # are resolved before the INSERT: the pk is a client-side UUID, so
            # the sync's tags already name this row, and its row updates match
            # nothing yet and only set the instance. Unguarded: without a
            # party there is no customer to write.
            if self.company_id is None:
                self.company_id = self.tenant_id()
            if self.company_id is None:
                raise IntegrityError(NO_TENANT)
            if self.party_id is None:
                # On failure the atomic rolls these parties back: put the
                # instance back too, so a retried save() resolves afresh.
                links = (self.party_id, self.account_holder_id)
                try:
                    sync_from_customer(self, origin=getattr(self, "party_origin", "erp"))
                    super().save(*args, **kwargs)
                except BaseException:
                    self.party_id, self.account_holder_id = links
                    raise
            else:
                super().save(*args, **kwargs)
            if wants_sync(kwargs.get("update_fields"), CUSTOMER_SYNC_FIELDS):
                # ``party_origin`` (not a column): how a new party was born (1.1).
                done = run_guarded(sync_from_customer, self, origin=getattr(self, "party_origin", "erp"))
                if done is None:
                    # The sync's savepoint rolled back, but it may already have
                    # pointed this instance at parties that no longer exist.
                    # Re-read the committed links so the signal never names them.
                    self.party_id, self.account_holder_id = (
                        Customer.objects.filter(pk=self.pk).values_list("party_id", "account_holder_id").first()
                        or (None, None)
                    )
        if adding:
            # customer_registered — single emission point: portal sign-up,
            # ERP form, DRF and the storefront all create through save().
            from core.plugins import signals
            from core.plugins.dispatch import emit

            # v2 (phase2-party.md 5.1): the party the sync just linked is
            # passed too; receivers written for v1 ignore the extra keywords.
            emit(
                signals.customer_registered, Customer, self, customer=self,
                party=self.party if self.party_id else None,
                account_holder=self.account_holder if self.account_holder_id else None,
                schema_version=2,
            )


class CustomerAddress(BaseModel):
    """Customer address (23 fields)."""

    class AddressType(models.TextChoices):
        BILLING = "billing", "Billing"
        SHIPPING = "shipping", "Shipping"
        BOTH = "both", "Both"

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="addresses")
    company = models.ForeignKey(
        "core.Company", on_delete=models.SET_NULL, null=True, blank=True,
    )
    address_type = models.CharField(
        max_length=10, choices=AddressType.choices, default=AddressType.BOTH,
    )
    is_default = models.BooleanField(default=False)
    label = models.CharField(max_length=100, blank=True)

    # Address fields
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    company_name = models.CharField(max_length=255, blank=True)
    address_line_1 = models.CharField(max_length=255)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state_province = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=20)
    country = models.CharField(max_length=2, default="US")
    phone = models.CharField(max_length=50, blank=True)

    # Shipping flags
    is_residential = models.BooleanField(default=True)
    has_loading_dock = models.BooleanField(default=False)
    has_forklift = models.BooleanField(default=False)
    requires_liftgate = models.BooleanField(default=False)
    delivery_instructions = models.TextField(blank=True)

    # Migration provenance
    bc_address_id = models.CharField(max_length=64, blank=True)

    # Mirror on the customer's party (2.5); written only by ``parties.sync``.
    party_address = models.ForeignKey(
        "parties.PartyAddress", on_delete=models.PROTECT, null=True, blank=True,
        related_name="customer_addresses", editable=False,
    )

    class Meta:
        verbose_name_plural = "customer addresses"
        ordering = ["-is_default", "label"]

    def __str__(self):
        return f"{self.customer} — {self.label or self.address_type}"

    def save(self, *args, **kwargs):
        from parties.sync import ADDRESS_SYNC_FIELDS, run_guarded, sync_from_address, wants_sync

        with transaction.atomic(using=kwargs.get("using")):
            super().save(*args, **kwargs)
            if wants_sync(kwargs.get("update_fields"), ADDRESS_SYNC_FIELDS):
                run_guarded(sync_from_address, self)


class CustomerContact(BaseModel):
    """Contact person at a customer account."""
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contacts")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    job_title = models.CharField(max_length=100, blank=True)
    is_primary = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    # The contact's person party (2.5); written only by ``parties.sync``.
    # NOT NULL since step 11, so ``save()`` resolves it before the INSERT.
    party = models.ForeignKey(
        "parties.Party", on_delete=models.PROTECT, blank=True,
        related_name="customer_contacts", editable=False,
    )

    class Meta:
        ordering = ["-is_primary", "first_name"]

    def save(self, *args, **kwargs):
        from parties.sync import CONTACT_SYNC_FIELDS, run_guarded, sync_from_contact, wants_sync

        with transaction.atomic(using=kwargs.get("using")):
            if self.party_id is None:
                # Step 11: resolved before the INSERT, as for Customer.
                try:
                    sync_from_contact(self)
                    if self.party_id is None:
                        raise IntegrityError("CustomerContact has no party: its customer has none")
                    super().save(*args, **kwargs)
                except BaseException:
                    self.party_id = None
                    raise
            else:
                super().save(*args, **kwargs)
            if wants_sync(kwargs.get("update_fields"), CONTACT_SYNC_FIELDS):
                run_guarded(sync_from_contact, self)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return f"{self.full_name} ({self.customer})"
