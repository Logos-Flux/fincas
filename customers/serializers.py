from rest_framework import serializers

from .models import Customer, CustomerAddress


class CustomerAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerAddress
        fields = "__all__"
        # party_address is the party-side mirror (phase2-party.md 2.5): sync only.
        read_only_fields = ["id", "created_at", "updated_at", "party_address"]


class CustomerListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            "id", "first_name", "last_name", "email",
            "customer_type", "pricing_tier", "is_active",
        ]


class CustomerDetailSerializer(serializers.ModelSerializer):
    addresses = CustomerAddressSerializer(many=True, read_only=True)

    class Meta:
        model = Customer
        fields = "__all__"
        # user is the portal-login link. Only the identity backfill and the
        # portal invite / auth flows (core/portal_views.py,
        # core/identity/resolve.py) may set it, via the ORM. Never writable
        # through the API: a client could re-point a portal login.
        # party / account_holder link the CRM party (phase2-party.md 2.1, 2.7):
        # set only by parties.sync, the backfill and (step 6) a merge.
        read_only_fields = [
            "id", "created_at", "updated_at", "last_order_at", "user",
            "party", "account_holder",
        ]

    def validate(self, attrs):
        # uniq_customer_email_ci_company (phase2-party.md 2.2, step 11). DRF
        # derives no validator from an expression constraint, so without this
        # a duplicate would be a 500. The tenant is the one the write will get:
        # the body's, else the row's, else the requester's (the viewset pins
        # it), else the only Company. Never the portal tenant: that fallback
        # (``Customer.save``) is the portal's and the storefront's, and a
        # company-less API create on a multi-company instance is refused
        # (phase4-fields.md 6, item 3).
        from core.views import NO_TENANT_API, request_company, sole_company

        instance = self.instance
        email = attrs.get("email", getattr(instance, "email", None))
        company = attrs.get("company")
        company_id = company.pk if company is not None else getattr(instance, "company_id", None)
        if company_id is None:
            requester = request_company(self.context.get("request"))
            company_id = requester.pk if requester is not None else None
        if company_id is None and instance is None:
            only = sole_company()
            if only is not None:
                attrs["company"] = only
                company_id = only.pk
        if company_id is None:
            raise serializers.ValidationError(
                {"company": [serializers.ErrorDetail(NO_TENANT_API, code="no_tenant")]})
        if Customer.email_taken(email, company_id, exclude_pk=getattr(instance, "pk", None)):
            raise serializers.ValidationError(
                {"email": [serializers.ErrorDetail(
                    "A customer with this email already exists.", code="uniq_customer_email_ci_company")]}
            )
        return attrs
