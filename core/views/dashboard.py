from django.contrib import messages
from django.db.models import F, Sum
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from core.quick_actions import KEYS, actions_for

from .mixins import ERPViewMixin


class DashboardView(ERPViewMixin, TemplateView):
    template_name = "erp/dashboard.html"
    current_section = "dashboard"
    page_title = "Dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Import models here to avoid circular imports
        from inventory.models import WarehouseStock
        from orders.models import Order, Quote
        from payments.models import Payment

        # KPI: Open orders: not shipped yet (Order.OPEN_STATUSES, F-089).
        # A shipped, unpaid order shows under "unpaid invoices" instead.
        open_order_count = Order.objects.filter(
            status__in=Order.OPEN_STATUSES
        ).count()

        # KPI: Revenue this month — SUM() in the DB, not Python sum() over
        # every amount. Filter via the enum, not a literal: the stored value
        # is "captured", so the old status="CAPTURED" literal never matched
        # anything and the KPI rendered $0.00. SUM returns None on an empty
        # set, so fall back to 0 for the template.
        revenue_total = (
            Payment.objects.filter(
                status=Payment.Status.CAPTURED,
                payment_date__gte=month_start,
            ).aggregate(total=Sum("amount"))["total"]
            or 0
        )

        # KPI: Pending quotes. Enum members, not literals: the stored values
        # are lowercase ("draft"), and the old "DRAFT"/"SENT" literals matched
        # nothing, so the KPI read 0 with real quotes open (same class of bug
        # as the revenue one above).
        pending_quote_count = Quote.objects.filter(
            status__in=[
                Quote.Status.DRAFT, Quote.Status.PENDING_APPROVAL, Quote.Status.SENT,
            ]
        ).count()

        # KPI: Low stock items — COUNT with an F() expression in the DB
        # instead of iterating every WarehouseStock row in Python.
        low_stock_count = WarehouseStock.objects.filter(
            reorder_level__gt=0, quantity__lte=F("reorder_level")
        ).count()

        # Recent orders
        recent_orders = Order.objects.select_related("customer").order_by(
            "-created_at"
        )[:10]

        # Alerts: overdue quotes
        overdue_quotes = Quote.objects.filter(
            status__in=[Quote.Status.SENT, Quote.Status.PENDING_APPROVAL],
            quote_expiration_date__lt=now.date(),
        ).count()

        # Alerts: unpaid invoices
        unpaid_invoices = Order.objects.filter(
            payment_status=Order.PaymentStatus.UNPAID,
            status__in=[
                Order.Status.AWAITING_FULFILLMENT, Order.Status.AWAITING_SHIPMENT,
                Order.Status.SHIPPED, Order.Status.COMPLETED,
            ],
        ).count()

        context.update({
            "open_order_count": open_order_count,
            "revenue_total": revenue_total,
            "pending_quote_count": pending_quote_count,
            "low_stock_count": low_stock_count,
            "recent_orders": recent_orders,
            "overdue_quotes": overdue_quotes,
            "unpaid_invoices": unpaid_invoices,
            "quick_actions": actions_for(self.request.user),
        })
        context["quick_actions_visible"] = sum(
            1 for a in context["quick_actions"] if a["visible"]
        )
        return context


class DashboardQuickActionsView(ERPViewMixin, View):
    """POST the set of quick actions to show; the rest are hidden for this user.

    The form sends one ``show=<key>`` per ticked action. Unknown keys are
    dropped, so a stale form cannot store garbage, and an empty submission
    hides everything (the card then says so and offers the menu back).
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        from core.models import ERPUserSettings

        shown = {k for k in request.POST.getlist("show") if k in KEYS}
        settings_obj, _ = ERPUserSettings.objects.get_or_create(user=request.user)
        settings_obj.hidden_quick_actions = sorted(KEYS - shown)
        settings_obj.save(update_fields=["hidden_quick_actions"])
        messages.success(request, "Quick actions updated.")
        return redirect("erp-dashboard")
