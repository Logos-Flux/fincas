from core.managers import scope_to_company
from core.views import CompanyScopedViewSet

from .models import Customer, CustomerAddress
from .serializers import (
    CustomerAddressSerializer,
    CustomerDetailSerializer,
    CustomerListSerializer,
)


class CustomerViewSet(CompanyScopedViewSet):
    queryset = Customer.objects.all()
    filterset_fields = ["customer_type", "pricing_tier", "is_active", "company"]
    search_fields = ["first_name", "last_name", "email"]
    ordering_fields = ["last_name", "created_at", "last_order_at"]

    def get_serializer_class(self):
        if self.action == "list":
            return CustomerListSerializer
        return CustomerDetailSerializer


class CustomerAddressViewSet(CompanyScopedViewSet):
    queryset = CustomerAddress.objects.select_related("customer")
    serializer_class = CustomerAddressSerializer
    filterset_fields = ["customer", "address_type", "country"]
    search_fields = ["city", "postal_code"]
    company_field = None
    parent_links = [("customer", "company")]

    def get_queryset(self):
        return scope_to_company(
            super().get_queryset(), self.request, "customer__company"
        )
