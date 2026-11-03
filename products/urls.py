from rest_framework.routers import DefaultRouter

from .views import (
    BrandViewSet,
    CategoryViewSet,
    ProductOptionValueViewSet,
    ProductOptionViewSet,
    ProductVariantViewSet,
    ProductViewSet,
    SupplierProductViewSet,
    VariantOptionValueViewSet,
)

router = DefaultRouter()
router.register("brands", BrandViewSet)
router.register("categories", CategoryViewSet)
router.register("products", ProductViewSet)
router.register("variants", ProductVariantViewSet)
router.register("supplier-products", SupplierProductViewSet)
router.register("product-options", ProductOptionViewSet)
router.register("product-option-values", ProductOptionValueViewSet)
router.register("variant-option-values", VariantOptionValueViewSet)

urlpatterns = router.urls
