from django import forms
from django.test import SimpleTestCase

from core.templatetags.erp_tags import add_class, aria_label


class _Form(forms.Form):
    quantity = forms.IntegerField(label="Quantity")
    sku = forms.CharField(label="SKU", widget=forms.TextInput(attrs={"aria-label": "Variant SKU"}))


class AddClassTests(SimpleTestCase):
    def test_adds_class_and_label_as_accessible_name(self):
        html = add_class(_Form()["quantity"], "form-control")
        self.assertIn('class="form-control"', html)
        self.assertIn('aria-label="Quantity"', html)

    def test_explicit_aria_label_wins(self):
        html = add_class(_Form()["sku"], "form-control")
        self.assertIn('aria-label="Variant SKU"', html)
        self.assertNotIn('aria-label="SKU"', html)


class AriaLabelTests(SimpleTestCase):
    def test_sets_name_without_touching_class(self):
        html = aria_label(_Form()["quantity"])
        self.assertIn('aria-label="Quantity"', html)
        self.assertNotIn("class=", html)

    def test_explicit_name(self):
        self.assertIn('aria-label="Remove line"', aria_label(_Form()["quantity"], "Remove line"))
