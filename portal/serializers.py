"""
Portal serializers — customer-scoped, never expose cost fields.
"""

from rest_framework import serializers

from customers.models import Customer, CustomerAddress, CustomerContact
from documents.models import ProductDocument
from orders.models import Order, OrderLineItem, Quote, QuoteLineItem, SalesInvoice, SubOrder


class PortalProfileSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    # CRM Phase 2 (5.3): read-only links to the countersigning party and the
    # account holder (the customer's own person party, ``me/``).
    party_id = serializers.UUIDField(read_only=True)
    account_holder_party_id = serializers.UUIDField(source="account_holder_id", read_only=True)

    class Meta:
        model = Customer
        fields = [
            "id",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "company_name",
            "customer_type",
            "is_tax_exempt",
            "accepts_marketing",
            "default_currency",
            "created_at",
            "updated_at",
            "party_id",
            "account_holder_party_id",
        ]
        read_only_fields = ["id", "email", "customer_type", "created_at", "updated_at"]


class PortalAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerAddress
        fields = [
            "id",
            "address_type",
            "is_default",
            "label",
            "first_name",
            "last_name",
            "company_name",
            "address_line_1",
            "address_line_2",
            "city",
            "state_province",
            "postal_code",
            "country",
            "phone",
            "is_residential",
            "has_loading_dock",
            "has_forklift",
            "requires_liftgate",
            "delivery_instructions",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class PortalContactSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = CustomerContact
        fields = [
            "id",
            "full_name",
            "first_name",
            "last_name",
            "email",
            "phone",
            "job_title",
            "is_primary",
        ]
        read_only_fields = ["id"]


class PortalOrderListSerializer(serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "status",
            "payment_status",
            "fulfillment_status",
            "grand_total",
            "created_at",
            "item_count",
        ]

    def get_item_count(self, obj):
        return obj.line_items.count()


class PortalOrderLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderLineItem
        fields = [
            "id",
            "variant",
            "sku",
            "item_name",
            "quantity",
            "unit_price_ex_tax",
            "unit_price_inc_tax",
            "line_total_ex_tax",
            "line_total_inc_tax",
            "fulfillment_status",
        ]


class PortalSubOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubOrder
        fields = [
            "id",
            "sub_order_number",
            "status",
            "tracking_number",
            "tracking_url",
            "estimated_ship_date",
            "actual_ship_date",
            "estimated_delivery_date",
            "actual_delivery_date",
        ]


class PortalOrderDetailSerializer(serializers.ModelSerializer):
    line_items = PortalOrderLineItemSerializer(many=True, read_only=True)
    sub_orders = PortalSubOrderSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "status",
            "payment_status",
            "fulfillment_status",
            "order_date",
            "grand_total",
            "subtotal_ex_tax",
            "subtotal_inc_tax",
            "tax_total",
            "shipping_cost_inc_tax",
            "customer_message",
            "created_at",
            "updated_at",
            "line_items",
            "sub_orders",
        ]


class PortalQuoteListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Quote
        fields = [
            "id",
            "quote_number",
            "revision_number",
            "status",
            "grand_total",
            "created_at",
        ]


class PortalQuoteLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteLineItem
        fields = [
            "id",
            "variant",
            "sku",
            "item_name",
            "quantity",
            "unit_price_ex_tax",
            "unit_price_inc_tax",
            "line_total_ex_tax",
            "line_total_inc_tax",
        ]


class PortalQuoteDetailSerializer(serializers.ModelSerializer):
    line_items = PortalQuoteLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Quote
        fields = [
            "id",
            "quote_number",
            "revision_number",
            "status",
            "grand_total",
            "subtotal_ex_tax",
            "tax_total",
            "shipping_cost_ex_tax",
            "customer_notes",
            "created_at",
            "updated_at",
            "line_items",
        ]


class PortalInvoiceSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.order_number", read_only=True)

    class Meta:
        model = SalesInvoice
        fields = [
            "id",
            "invoice_number",
            "order_number",
            "status",
            "invoice_date",
            "due_date",
            "paid_date",
            "payment_terms",
            "grand_total",
            "amount_paid",
            "balance_due",
            "created_at",
        ]


class PortalDocumentSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.title", read_only=True)

    class Meta:
        model = ProductDocument
        fields = [
            "id",
            "product",
            "product_name",
            "document_type",
            "title",
            "r2_url",
            "file_size",
            "is_verified",
        ]


# --- Party (CRM Phase 2 step 7; docs/crm/phase2-party.md 5.3) --------------------

PORTAL_PHONE_PURPOSES = ("personal", "work", "other")
PORTAL_MAX_PHONES = 10  # phone rows a party may carry after a portal add

_NAME_OWNED = "Your name comes from your profile: change first_name / last_name through profile/."
_EMAIL_OWNED = "Email addresses can't be changed here yet. Contact us to change one."
_PHONE_OWNED = "This number comes from your profile or our records: change it through profile/ or contact us."


class PortalPhoneWriteSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False)
    number = serializers.CharField(max_length=50, required=False)
    purpose = serializers.ChoiceField(choices=PORTAL_PHONE_PURPOSES, required=False)
    is_primary = serializers.BooleanField(required=False)


class PortalMeUpdateSerializer(serializers.Serializer):
    """PATCH ``me/``: the customer's own phones (5.3, P2).

    ``phones`` is a list of changes to the customer's own person party (the
    party comes from the request, never from the body):

    - no ``id``: add a phone (``number`` required, ``source="portal"``);
    - ``id`` of a phone the customer added here: change ``number`` / ``purpose``;
    - ``id`` of any of the party's phones: ``is_primary: true`` makes it the
      one primary phone (it needs a valid number).

    Names, emails and the commerce phone belong to the Customer while it
    exists (P2) and are refused with 400 naming ``profile/``. Changing an
    email needs a verification flow (Phase 5b). An ``id`` that is not one of
    the party's phones is "Unknown phone", whoever owns it.
    """

    OWNED = {"display_name": _NAME_OWNED, "given_name": _NAME_OWNED, "family_name": _NAME_OWNED,
             "emails": _EMAIL_OWNED}

    phones = PortalPhoneWriteSerializer(many=True, required=False)

    def validate(self, attrs):
        data = self.initial_data if isinstance(self.initial_data, dict) else {}
        errors = {key: [msg] for key, msg in self.OWNED.items() if key in data}
        errors.update({key: ["This field can't be changed here."]
                       for key in sorted(set(data) - set(self.OWNED) - {"phones"})})
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def validate_phones(self, items):
        from parties.mirror import phone_parts
        from parties.models import PartyPhone
        from parties.portal import PORTAL_SOURCE

        party = self.context["party"]
        region = self.context.get("region")
        if sum(1 for item in items if item.get("is_primary")) > 1:
            raise serializers.ValidationError("Only one phone can be primary.")
        rows = {row.pk: row for row in PartyPhone.objects.filter(party=party)}
        if len(rows) + sum(1 for item in items if "id" not in item) > PORTAL_MAX_PHONES:
            raise serializers.ValidationError(f"At most {PORTAL_MAX_PHONES} phone numbers.")
        numbers = {pk: row.number_e164 for pk, row in rows.items() if row.number_e164}
        seen = set()
        for index, item in enumerate(items):
            row = None
            if "id" in item:
                row = rows.get(item["id"])
                if row is None or row.pk in seen:
                    # Another party's phone looks exactly like a missing one.
                    raise serializers.ValidationError("Unknown phone.")
                seen.add(row.pk)
                if row.source != PORTAL_SOURCE and ("number" in item or "purpose" in item):
                    raise serializers.ValidationError(_PHONE_OWNED)
                if item.get("is_primary") is False and row.is_primary:
                    raise serializers.ValidationError("Choose another phone as primary instead.")
            elif not item.get("number"):
                raise serializers.ValidationError("A new phone needs a number.")
            if "number" in item:
                _raw, e164, _ext = phone_parts(item["number"], region)
                if not e164:
                    raise serializers.ValidationError("That is not a valid phone number.")
                key = row.pk if row is not None else f"new-{index}"
                if any(value == e164 and pk != key for pk, value in numbers.items()):
                    raise serializers.ValidationError("You already have this phone number.")
                numbers[key] = e164
            elif item.get("is_primary") and row is not None and not row.number_e164:
                raise serializers.ValidationError("Only a valid number can be primary.")
        return items

    def save(self):
        from django.db import transaction

        from parties.models import PartyPhone
        from parties.portal import PORTAL_SOURCE
        from parties.sync import _promote_primary

        party = self.context["party"]
        region = self.context.get("region")
        with transaction.atomic():
            primary = None
            for item in self.validated_data.get("phones", []):
                if "id" in item:
                    row = PartyPhone.objects.select_for_update().get(pk=item["id"], party=party)
                else:
                    row = PartyPhone(party=party, source=PORTAL_SOURCE, purpose="personal")
                if "number" in item:
                    row.number_raw = item["number"].strip()
                    row.phone_region = region
                if "purpose" in item:
                    row.purpose = item["purpose"]
                if row._state.adding or "number" in item or "purpose" in item:
                    row.save()
                if item.get("is_primary"):
                    primary = row
            if primary is not None and not primary.is_primary:
                # One primary per party (uniq_partyphone_one_primary): clear the old one first.
                for other in PartyPhone.objects.filter(party=party, is_primary=True):
                    other.is_primary = False
                    other.save(update_fields=["is_primary", "updated_at"])
                primary.is_primary = True
                primary.save(update_fields=["is_primary", "updated_at"])
            _promote_primary(PartyPhone, party)  # a first phone becomes primary
        return party


class PortalConsentSerializer(serializers.Serializer):
    """POST ``me/consents/``: one record for marketing by email or SMS (5.3, 1.6)."""

    channel = serializers.ChoiceField(choices=["email", "sms"])
    purpose = serializers.ChoiceField(choices=["marketing"], default="marketing")
    state = serializers.ChoiceField(choices=["granted", "withdrawn"])
    wording_version = serializers.CharField(max_length=64)
