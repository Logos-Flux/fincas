from django import forms

from .models import DiscountCode


class DiscountCodeForm(forms.ModelForm):
    allowed_skus = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Enter one SKU per line"}),
        help_text="One SKU per line (only used when Applies To is 'Specific SKUs')",
    )

    class Meta:
        model = DiscountCode
        exclude = ["id", "created_at", "updated_at", "times_used", "company"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "valid_from": forms.DateTimeInput(
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "valid_to": forms.DateTimeInput(
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Convert JSON list to newline-separated text for display
        if self.instance and self.instance.pk and self.instance.allowed_skus:
            self.initial["allowed_skus"] = "\n".join(self.instance.allowed_skus)

    def clean_allowed_skus(self):
        raw = self.cleaned_data.get("allowed_skus", "")
        if not raw.strip():
            return []
        # Split by newlines, commas, or spaces — strip whitespace, remove empties
        skus = []
        for line in raw.replace(",", "\n").split("\n"):
            sku = line.strip()
            if sku:
                skus.append(sku)
        return skus


class GenerateCodesForm(forms.Form):
    """Form for batch code generation."""

    prefix = forms.CharField(
        max_length=20,
        help_text='e.g. "SPRING" → SPRING-A7K2M9X4',
    )
    count = forms.IntegerField(min_value=1, max_value=1000, initial=10)
    code_length = forms.IntegerField(min_value=4, max_value=16, initial=8)
    discount_type = forms.ChoiceField(choices=DiscountCode.DiscountType.choices)
    value = forms.DecimalField(max_digits=10, decimal_places=2)
    max_uses = forms.IntegerField(min_value=1, initial=1)
    applies_to = forms.ChoiceField(choices=DiscountCode.AppliesTo.choices)
    allowed_skus = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text="One SKU per line (only used when applies_to is 'Specific SKUs')",
    )
    valid_from = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format="%Y-%m-%dT%H:%M"),
    )
    valid_to = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format="%Y-%m-%dT%H:%M"),
    )
    minimum_order = forms.DecimalField(
        max_digits=10, decimal_places=2, required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if not field.widget.attrs.get("class"):
                field.widget.attrs["class"] = "form-control"
