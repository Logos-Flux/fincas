import json
import logging

from django.contrib import admin, messages
from django.http import HttpResponse
from django.urls import path
from django.utils.html import format_html

from .label_service import create_label_for_pick_ticket, void_label
from .models import PickTicket, PickTicketLine

logger = logging.getLogger(__name__)


class PickTicketLineInline(admin.TabularInline):
    model = PickTicketLine
    extra = 0
    fields = ["variant", "sku", "item_name", "quantity", "bin_location", "picked_qty", "status"]
    readonly_fields = ["sku", "item_name"]


@admin.register(PickTicket)
class PickTicketAdmin(admin.ModelAdmin):
    list_display = [
        "ticket_number", "sub_order", "warehouse", "status",
        "assigned_to", "label_status_badge", "created_at",
    ]
    list_filter = ["status", "warehouse"]
    search_fields = ["ticket_number", "sub_order__sub_order_number"]
    readonly_fields = ["id", "created_at", "updated_at", "tracking_summary"]
    inlines = [PickTicketLineInline]
    actions = ["action_print_labels", "action_void_labels"]

    # ------------------------------------------------------------------
    # Custom URLs — print label endpoint
    # ------------------------------------------------------------------

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<pk>/print-label/",
                self.admin_site.admin_view(self.print_label_view),
                name="fulfillment_pickticket_print_label",
            ),
            path(
                "<pk>/void-label/",
                self.admin_site.admin_view(self.void_label_view),
                name="fulfillment_pickticket_void_label",
            ),
        ]
        return custom + urls

    # ------------------------------------------------------------------
    # Change form buttons
    # ------------------------------------------------------------------

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        try:
            obj = PickTicket.objects.get(pk=object_id)
            extra_context["pick_ticket"] = obj
            sub = obj.sub_order
            extra_context["has_label"] = bool(sub and sub.tracking_number)
            extra_context["tracking_number"] = sub.tracking_number if sub else ""
        except PickTicket.DoesNotExist:
            pass
        return super().change_view(request, object_id, form_url, extra_context)

    # ------------------------------------------------------------------
    # Print label view (called from button or action)
    # ------------------------------------------------------------------

    def print_label_view(self, request, pk):
        try:
            pick_ticket = PickTicket.objects.select_related(
                "sub_order__order__shipping_address",
                "sub_order__order__customer",
                "warehouse",
            ).prefetch_related("lines__variant__product").get(pk=pk)
        except PickTicket.DoesNotExist:
            return HttpResponse("Pick ticket not found", status=404)

        result = create_label_for_pick_ticket(pick_ticket)

        if result["success"]:
            # Return ZPL URL as JSON so the admin JS can send to printer
            return HttpResponse(
                json.dumps({
                    "success": True,
                    "tracking_number": result["tracking_number"],
                    "zpl_url": result.get("zpl_url"),
                    "label_url": result["label_url"],
                    "postage_cost": result["postage_cost"],
                    "carrier": result["carrier"],
                    "service": result["service"],
                }),
                content_type="application/json",
            )
        else:
            return HttpResponse(
                json.dumps({"success": False, "error": result["error"]}),
                content_type="application/json",
                status=400,
            )

    def void_label_view(self, request, pk):
        try:
            pick_ticket = PickTicket.objects.select_related("sub_order").get(pk=pk)
        except PickTicket.DoesNotExist:
            return HttpResponse("Pick ticket not found", status=404)

        if not pick_ticket.sub_order:
            return HttpResponse(
                json.dumps({"success": False, "error": "No sub_order"}),
                content_type="application/json",
                status=400,
            )

        result = void_label(pick_ticket.sub_order)
        status_code = 200 if result["success"] else 400
        return HttpResponse(json.dumps(result), content_type="application/json", status=status_code)

    # ------------------------------------------------------------------
    # Bulk admin actions
    # ------------------------------------------------------------------

    @admin.action(description="🖨️ Print shipping labels (EasyPost)")
    def action_print_labels(self, request, queryset):
        success_count = 0
        fail_count = 0
        for pt in queryset.select_related(
            "sub_order__order__shipping_address",
            "sub_order__order__customer",
            "warehouse",
        ).prefetch_related("lines__variant__product"):
            if pt.sub_order and pt.sub_order.tracking_number:
                self.message_user(
                    request,
                    f"{pt.ticket_number}: already has tracking {pt.sub_order.tracking_number} — skipped",
                    messages.WARNING,
                )
                continue
            result = create_label_for_pick_ticket(pt)
            if result["success"]:
                success_count += 1
                self.message_user(
                    request,
                    f"{pt.ticket_number}: label created ✓ — {result['tracking_number']} (${result['postage_cost']:.2f})",
                    messages.SUCCESS,
                )
            else:
                fail_count += 1
                self.message_user(request, f"{pt.ticket_number}: FAILED — {result['error']}", messages.ERROR)

    @admin.action(description="🚫 Void shipping labels (EasyPost refund)")
    def action_void_labels(self, request, queryset):
        for pt in queryset.select_related("sub_order"):
            if not pt.sub_order or not pt.sub_order.tracking_number:
                self.message_user(request, f"{pt.ticket_number}: no label to void — skipped", messages.WARNING)
                continue
            result = void_label(pt.sub_order)
            level = messages.SUCCESS if result["success"] else messages.ERROR
            msg = result.get("message") or result.get("error", "Unknown error")
            self.message_user(request, f"{pt.ticket_number}: {msg}", level)

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def label_status_badge(self, obj):
        sub = obj.sub_order
        if sub and sub.tracking_number:
            return format_html(
                '<span style="color:green;font-weight:bold">✓ {}</span>',
                sub.tracking_number,
            )
        return format_html('<span style="color:#999">No label</span>')
    label_status_badge.short_description = "Label / Tracking"

    def tracking_summary(self, obj):
        sub = obj.sub_order
        if not sub:
            return "—"
        parts = []
        if sub.tracking_number:
            parts.append(f"Tracking: {sub.tracking_number}")
        if sub.carrier:
            parts.append(f"Carrier: {sub.carrier} {sub.service_level}")
        if sub.shipping_cost_ex_tax:
            parts.append(f"Postage: ${sub.shipping_cost_ex_tax}")
        if sub.shipping_label_url:
            parts.append(f'<a href="{sub.shipping_label_url}" target="_blank">View Label PDF</a>')
        return format_html("<br>".join(parts)) if parts else "No tracking info"
    tracking_summary.short_description = "Shipping Info"
    tracking_summary.allow_tags = True


@admin.register(PickTicketLine)
class PickTicketLineAdmin(admin.ModelAdmin):
    list_display = ["pick_ticket", "variant", "quantity", "picked_qty", "status", "bin_location"]
    list_filter = ["status"]
    search_fields = ["variant__sku", "pick_ticket__ticket_number"]
    readonly_fields = ["id", "created_at", "updated_at"]
