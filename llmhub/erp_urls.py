from django.urls import path

from . import erp_views as v

urlpatterns = [
    path("", v.LLMSettingsView.as_view(), name="erp-llm-settings"),
    path("connection/", v.ConnectionUpdateView.as_view(), name="erp-llm-connection"),
    path("lanes/", v.LaneUpdateView.as_view(), name="erp-llm-lanes"),
    path("models/add/", v.ModelCreateView.as_view(), name="erp-llm-model-add"),
    path("models/<int:pk>/", v.ModelUpdateView.as_view(), name="erp-llm-model-edit"),
    path("models/<int:pk>/<str:action>/", v.ModelActionView.as_view(), name="erp-llm-model-action"),
    path("test/<str:purpose>/", v.PurposeTestView.as_view(), name="erp-llm-test"),
    path("models-refresh/", v.ModelsRefreshView.as_view(), name="erp-llm-models-refresh"),
]
