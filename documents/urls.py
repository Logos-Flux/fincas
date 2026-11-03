from rest_framework.routers import DefaultRouter

from .views import ProductDocumentViewSet

router = DefaultRouter()
router.register("product-documents", ProductDocumentViewSet)

urlpatterns = router.urls
