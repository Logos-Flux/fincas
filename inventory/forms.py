from django import forms

from .models import Warehouse


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = [
            # Identification
            "warehouse_code", "name", "warehouse_type",
            "is_active",
            # Ownership (Step 2: required — company-less warehouses are
            # invisible to scoped reads, never shared)
            "company",
            # Capabilities
            "accepts_incoming", "can_fulfill",
            "processing_time_days",
            # Address
            "address_line_1", "address_line_2",
            "city", "state_province", "postal_code", "country",
            "ship_from_label",
            "phone", "email",
            # Company
            "default_for_company", "manager",
            # Notes
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
        }
