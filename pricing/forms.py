from django import forms

from .models import PriceList, PriceListItem


class PriceListForm(forms.ModelForm):
    class Meta:
        model = PriceList
        fields = [
            # Identification
            "name", "price_list_type", "currency",
            "is_default", "is_active",
            # Company
            "company",
            # Validity
            "valid_from", "valid_to",
            # Controls
            "margin_floor_percent", "discount_cap_percent",
            # Description
            "description", "notes",
        ]
        widgets = {
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "valid_to": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }


class PriceListItemForm(forms.ModelForm):
    class Meta:
        model = PriceListItem
        fields = [
            "price_list", "variant", "price", "min_qty",
            "valid_from", "valid_to", "is_active",
        ]
        widgets = {
            "price_list": forms.HiddenInput(),
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "valid_to": forms.DateInput(attrs={"type": "date"}),
        }
