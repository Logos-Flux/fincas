from django import forms
from django.forms import inlineformset_factory

from orders.forms import _limit_fk_queryset

from .models import PickTicket, PickTicketLine


class PickTicketForm(forms.ModelForm):
    class Meta:
        model = PickTicket
        fields = [
            "warehouse", "order", "shipment_group", "sub_order",
            "status", "assigned_to",
            "ship_to_address", "ship_to_label", "ship_to_attention",
            "notes",
        ]
        widgets = {
            "ship_to_address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].required = False
        for fk in ("order", "sub_order", "shipment_group"):
            if fk in self.fields:
                _limit_fk_queryset(self, fk)


class PickTicketLineForm(forms.ModelForm):
    class Meta:
        model = PickTicketLine
        fields = [
            "variant", "item_name", "sku",
            "quantity", "unit_cost", "line_total",
            "bin_location", "order_line_item",
        ]
        widgets = {
            "item_name": forms.Textarea(attrs={"rows": 1}),
            "order_line_item": forms.HiddenInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["quantity"].initial = 1
        self.fields["quantity"].widget.attrs.update({"class": "form-control form-control-sm text-center"})
        self.fields["unit_cost"].widget.attrs.update({"class": "form-control form-control-sm text-end"})
        if self.instance.pk and self.instance.variant_id:
            self.fields["variant"].queryset = self.fields["variant"].queryset.filter(
                pk=self.instance.variant_id
            )
        else:
            self.fields["variant"].queryset = self.fields["variant"].queryset.none()


PickTicketLineFormSet = inlineformset_factory(
    PickTicket, PickTicketLine,
    form=PickTicketLineForm,
    extra=1,
    can_delete=True,
)
