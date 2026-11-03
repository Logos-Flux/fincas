"""
Customer Portal API views — all data scoped to request.customer.
"""

from rest_framework import filters, generics, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from core.portal_auth import PortalTokenAuthentication
from core.portal_permissions import IsPortalCustomer
from documents.models import ProductDocument
from orders.models import Order, Quote, SalesInvoice

from .serializers import (
    PortalAddressSerializer,
    PortalConsentSerializer,
    PortalContactSerializer,
    PortalDocumentSerializer,
    PortalInvoiceSerializer,
    PortalMeUpdateSerializer,
    PortalOrderDetailSerializer,
    PortalOrderListSerializer,
    PortalProfileSerializer,
    PortalQuoteDetailSerializer,
    PortalQuoteListSerializer,
)

_AUTH = [PortalTokenAuthentication]
_PERM = [IsPortalCustomer]


class PortalProfileView(generics.RetrieveUpdateAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalProfileSerializer

    def get_object(self):
        return self.request.customer


class PortalAddressListView(generics.ListCreateAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalAddressSerializer

    def get_queryset(self):
        return self.request.customer.addresses.all()

    def perform_create(self, serializer):
        serializer.save(customer=self.request.customer)


class PortalAddressDetailView(generics.UpdateAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalAddressSerializer

    def get_queryset(self):
        return self.request.customer.addresses.all()


class PortalContactListView(generics.ListAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalContactSerializer

    def get_queryset(self):
        return self.request.customer.contacts.all()


class PortalOrderListView(generics.ListAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalOrderListSerializer
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ["order_date", "grand_total", "status"]
    ordering = ["-order_date"]

    def get_queryset(self):
        qs = Order.objects.filter(customer=self.request.customer)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs


class PortalOrderDetailView(generics.RetrieveAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalOrderDetailSerializer

    def get_queryset(self):
        return Order.objects.filter(customer=self.request.customer).prefetch_related(
            "line_items", "sub_orders"
        )


class PortalQuoteListView(generics.ListAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalQuoteListSerializer
    filter_backends = [filters.OrderingFilter]
    ordering = ["-created_at"]

    def get_queryset(self):
        return Quote.objects.filter(customer=self.request.customer)


class PortalQuoteDetailView(generics.RetrieveAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalQuoteDetailSerializer

    def get_queryset(self):
        return Quote.objects.filter(customer=self.request.customer).prefetch_related(
            "line_items"
        )


class PortalInvoiceListView(generics.ListAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalInvoiceSerializer
    filter_backends = [filters.OrderingFilter]
    ordering = ["-invoice_date"]

    def get_queryset(self):
        return SalesInvoice.objects.filter(order__customer=self.request.customer)


class PortalDocumentListView(generics.ListAPIView):
    authentication_classes = _AUTH
    permission_classes = _PERM
    serializer_class = PortalDocumentSerializer

    def get_queryset(self):
        ordered_variant_ids = (
            self.request.customer.orders
            .values_list("line_items__variant_id", flat=True)
            .distinct()
        )
        return ProductDocument.objects.filter(
            product__variants__id__in=ordered_variant_ids,
            is_verified=True,
        ).distinct()


class PortalActivityView(APIView):
    authentication_classes = _AUTH
    permission_classes = _PERM

    def get(self, request):
        customer = request.customer
        events = []

        for order in Order.objects.filter(customer=customer).order_by("-created_at")[:10]:
            events.append({
                "type": "order",
                "id": order.id,
                "reference": order.order_number,
                "status": order.status,
                "date": order.created_at,
            })

        for quote in Quote.objects.filter(customer=customer).order_by("-created_at")[:10]:
            events.append({
                "type": "quote",
                "id": quote.id,
                "reference": quote.quote_number,
                "status": quote.status,
                "date": quote.created_at,
            })

        events.sort(key=lambda e: e["date"], reverse=True)
        return Response(events[:20])


class PortalModuleCountsView(APIView):
    authentication_classes = _AUTH
    permission_classes = _PERM

    def get(self, request):
        customer = request.customer
        orders_qs = Order.objects.filter(customer=customer)
        ordered_variant_ids = (
            orders_qs.values_list("line_items__variant_id", flat=True).distinct()
        )
        return Response({
            "orders": orders_qs.count(),
            "quotes": Quote.objects.filter(customer=customer).count(),
            "invoices": SalesInvoice.objects.filter(order__customer=customer).count(),
            "shared_docs": 0,
            "product_docs": ProductDocument.objects.filter(
                product__variants__id__in=ordered_variant_ids,
                is_verified=True,
            ).distinct().count(),
            "proposals": 0,
        })


# --- Party (CRM Phase 2 step 7; docs/crm/phase2-party.md 5.3) --------------------


def _me_party_or_404(request):
    from parties.portal import me_party

    party = me_party(request.customer, request.user)
    if party is None:
        raise NotFound({"detail": "Your details are not available yet.", "code": "portal_party_unavailable"})
    return party


def _me_payload(customer, party):
    from parties.portal import account_for, phone_view

    emails = party.emails.order_by("-is_primary", "created_at")
    phones = party.phones.order_by("-is_primary", "created_at")
    return {
        "id": str(party.pk),
        "kind": party.kind,
        "display_name": party.display_name,
        "emails": [
            {
                "email": e.email,
                "purpose": e.purpose,
                "is_primary": e.is_primary,
                "verified": e.verified_at is not None,
            }
            for e in emails
        ],
        "phones": [phone_view(p) for p in phones],
        "account_for": account_for(customer, party),
    }


class PortalMeView(APIView):
    """``me/``: the customer's own person party (5.3).

    GET shows it. PATCH changes only phones the customer added here, adds
    one, or picks the primary phone. Names, emails and the commerce phone are
    owned by the Customer (P2) and are refused with 400 naming ``profile/``;
    an email change needs a verification flow (Phase 5b).
    """

    authentication_classes = _AUTH
    permission_classes = _PERM

    def get(self, request):
        party = _me_party_or_404(request)
        return Response(_me_payload(request.customer, party))

    def patch(self, request):
        party = _me_party_or_404(request)
        from parties.sync import _region_for

        serializer = PortalMeUpdateSerializer(
            data=request.data, context={"party": party, "region": _region_for(request.customer)},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        party.refresh_from_db()
        return Response(_me_payload(request.customer, party))


class PortalConsentView(APIView):
    """``me/consents/``: marketing consent for email and SMS (5.3).

    GET: the current state per pair, and whether the primary address is
    suppressed (a message only: reasons are never shown). POST appends a
    record with ``source="portal"`` and the evidence; withdrawing is the
    same single call as granting. Suppressions are read-only here.
    """

    authentication_classes = _AUTH
    permission_classes = _PERM

    def get(self, request):
        from parties.portal import consent_state

        party = _me_party_or_404(request)
        return Response({"consents": consent_state(party)})

    def post(self, request):
        from core.client_ip import client_ip
        from parties.portal import consent_state, record_consent

        party = _me_party_or_404(request)
        serializer = PortalConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        evidence = {
            "ip": client_ip(request),
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:500],
            "wording_version": data["wording_version"],
        }
        record_consent(
            party, request.customer, channel=data["channel"], purpose=data["purpose"],
            state=data["state"], user=request.user, evidence=evidence,
        )
        return Response({"consents": consent_state(party)}, status=status.HTTP_201_CREATED)
