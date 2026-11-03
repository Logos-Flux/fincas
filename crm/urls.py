from rest_framework.routers import DefaultRouter

from .api import (
    EngagementContactViewSet,
    EngagementViewSet,
    MyQueueViewSet,
    PipelineViewSet,
    StaffDirectoryViewSet,
    StaffQueueViewSet,
    StageChangeViewSet,
    StageViewSet,
    TaskViewSet,
    TouchpointViewSet,
)
from .views import ActivityLogViewSet, LeadViewSet

router = DefaultRouter()
router.register("leads", LeadViewSet)
router.register("activities", ActivityLogViewSet, basename="activitylog")
# CRM Phase 3 step 7 (docs/crm/phase3-engagement.md 5.2).
router.register("pipelines", PipelineViewSet, basename="pipeline")
router.register("stages", StageViewSet, basename="stage")
router.register("engagements", EngagementViewSet, basename="engagement")
router.register("engagement-contacts", EngagementContactViewSet, basename="engagementcontact")
router.register("stage-changes", StageChangeViewSet, basename="stagechange")
router.register("tasks", TaskViewSet, basename="task")
router.register("touchpoints", TouchpointViewSet, basename="touchpoint")
router.register("me", MyQueueViewSet, basename="crm-me")
# Step 10 (spec 5.3): the assistant names the person it acts for.
router.register("staff", StaffDirectoryViewSet, basename="crm-staff")
router.register("queues", StaffQueueViewSet, basename="crm-queue")

urlpatterns = router.urls
