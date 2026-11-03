from django.urls import path

from .erp_views import (
    BulkInvoicePDFView,
    InvoiceDetailView,
    InvoiceListView,
    InvoicePDFView,
    InvoicePreviewView,
    RecordPaymentView,
    RefundView,
    SendInvoiceEmailView,
)

urlpatterns = [
    path("", InvoiceListView.as_view(), name="erp-invoice-list"),
    path("bulk-pdf/", BulkInvoicePDFView.as_view(), name="erp-invoice-bulk-pdf"),
    path("<uuid:pk>/", InvoiceDetailView.as_view(), name="erp-invoice-detail"),
    path("<uuid:pk>/preview/", InvoicePreviewView.as_view(), name="erp-invoice-preview"),
    path("<uuid:pk>/pdf/", InvoicePDFView.as_view(), name="erp-invoice-pdf"),
    path("<uuid:pk>/send/", SendInvoiceEmailView.as_view(), name="erp-invoice-send"),
    path("<uuid:pk>/record-payment/", RecordPaymentView.as_view(), name="erp-invoice-record-payment"),
    path("<uuid:pk>/refund/", RefundView.as_view(), name="erp-invoice-refund"),
]
