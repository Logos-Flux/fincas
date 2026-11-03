from django.urls import path

from .views import RedeemDiscountView, ValidateDiscountView

urlpatterns = [
    path("validate/", ValidateDiscountView.as_view(), name="api-discount-validate"),
    path("redeem/", RedeemDiscountView.as_view(), name="api-discount-redeem"),
]
