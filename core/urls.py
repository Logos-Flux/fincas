from django.urls import path
from rest_framework.routers import DefaultRouter

from . import auth_views
from .views import AuditLogViewSet, CompanyViewSet, TeamViewSet, UserProfileViewSet

router = DefaultRouter()
router.register("companies", CompanyViewSet)
router.register("users", UserProfileViewSet)
router.register("teams", TeamViewSet)
router.register("audit-logs", AuditLogViewSet)

urlpatterns = router.urls + [
    path("auth/token/", auth_views.TokenObtainView.as_view(), name="api-token-obtain"),
    path(
        "auth/storefront-token/",
        auth_views.StorefrontTokenView.as_view(),
        name="api-storefront-token",
    ),
]
