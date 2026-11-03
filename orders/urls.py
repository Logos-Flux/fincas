from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    OrderLineItemViewSet,
    OrderViewSet,
    QuoteLineItemViewSet,
    QuoteViewSet,
    SalesInvoiceViewSet,
    SubOrderViewSet,
    sales_invoice_by_token,
    sales_invoice_reconcile,
)

router = DefaultRouter()
router.register("quotes", QuoteViewSet)
router.register("quote-lines", QuoteLineItemViewSet)
router.register("orders", OrderViewSet)
router.register("order-lines", OrderLineItemViewSet)
router.register("sub-orders", SubOrderViewSet)
router.register("sales-invoices", SalesInvoiceViewSet)

urlpatterns = [
    # Public endpoint — no auth. Placed before `router.urls` so DRF's
    # authentication middleware on the viewset doesn't intercept it.
    path(
        "sales-invoices/by-token/<uuid:token>/",
        sales_invoice_by_token,
        name="sales-invoice-by-token",
    ),
    path(
        "sales-invoices/by-token/<uuid:token>/reconcile/",
        sales_invoice_reconcile,
        name="sales-invoice-reconcile",
    ),
    *router.urls,
]
