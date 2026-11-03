"""``/erp/crm/`` routes (docs/crm/phase3-engagement.md 6; Phase 3 step 8).

Mounted by ``core/erp_urls.py``. State changes are POST-only and go through
the CRM services (``crm.engagements``, ``crm.tasks``, ``communications.ingest``).
"""

from django.urls import path

from . import erp_views as v

urlpatterns = [
    path("my-day/", v.MyDayView.as_view(), name="erp-crm-my-day"),
    # Engagements (6.1, 6.2, 6.3)
    path("engagements/", v.EngagementListView.as_view(), name="erp-crm-engagement-list"),
    path("engagements/new/", v.EngagementCreateView.as_view(), name="erp-crm-engagement-create"),
    path("engagements/bulk/", v.EngagementBulkView.as_view(), name="erp-crm-engagement-bulk"),
    path("engagements/<uuid:pk>/", v.EngagementDetailView.as_view(), name="erp-crm-engagement-detail"),
    path("engagements/<uuid:pk>/edit/", v.EngagementUpdateView.as_view(), name="erp-crm-engagement-edit"),
    path("engagements/<uuid:pk>/timeline/", v.TimelineView.as_view(), name="erp-crm-engagement-timeline"),
    path("engagements/<uuid:pk>/move/", v.EngagementMoveView.as_view(), name="erp-crm-engagement-move"),
    path("engagements/<uuid:pk>/win/", v.EngagementWinView.as_view(), name="erp-crm-engagement-win"),
    path("engagements/<uuid:pk>/lose/", v.EngagementLoseView.as_view(), name="erp-crm-engagement-lose"),
    path("engagements/<uuid:pk>/reopen/", v.EngagementReopenView.as_view(), name="erp-crm-engagement-reopen"),
    path("engagements/<uuid:pk>/close-remaining/", v.EngagementCloseRemainingView.as_view(),
         name="erp-crm-engagement-close-remaining"),
    path("engagements/<uuid:pk>/tasks/new/", v.TaskCreateView.as_view(), name="erp-crm-engagement-task-create"),
    path("engagements/<uuid:pk>/contacts/new/", v.ContactCreateView.as_view(),
         name="erp-crm-engagement-contact-create"),
    path("engagements/<uuid:pk>/log/", v.LogCommunicationView.as_view(), name="erp-crm-engagement-log"),
    path("contacts/<uuid:pk>/remove/", v.ContactRemoveView.as_view(), name="erp-crm-contact-remove"),
    # Tasks (6.4, 6.5)
    path("tasks/", v.TaskListView.as_view(), name="erp-crm-task-list"),
    path("tasks/new/", v.TaskNewView.as_view(), name="erp-crm-task-create"),
    path("tasks/<uuid:pk>/complete/", v.TaskCompleteView.as_view(), name="erp-crm-task-complete"),
    path("tasks/<uuid:pk>/reopen/", v.TaskReopenView.as_view(), name="erp-crm-task-reopen"),
    path("tasks/<uuid:pk>/cancel/", v.TaskCancelView.as_view(), name="erp-crm-task-cancel"),
    path("tasks/<uuid:pk>/reschedule/", v.TaskRescheduleView.as_view(), name="erp-crm-task-reschedule"),
    path("tasks/<uuid:pk>/reassign/", v.TaskReassignView.as_view(), name="erp-crm-task-reassign"),
    # Leads (6.5)
    path("leads/", v.LeadListView.as_view(), name="erp-crm-lead-list"),
    path("leads/new/", v.LeadCreateView.as_view(), name="erp-crm-lead-create"),
    # Pipelines (6.5, crm.manage_pipelines)
    path("pipelines/", v.PipelineListView.as_view(), name="erp-crm-pipeline-list"),
    path("pipelines/new/", v.PipelineCreateView.as_view(), name="erp-crm-pipeline-create"),
    path("pipelines/<uuid:pk>/edit/", v.PipelineUpdateView.as_view(), name="erp-crm-pipeline-edit"),
    path("pipelines/<uuid:pipeline_pk>/stages/new/", v.StageCreateView.as_view(), name="erp-crm-stage-create"),
    path("stages/<uuid:pk>/edit/", v.StageUpdateView.as_view(), name="erp-crm-stage-edit"),
    path("stages/<uuid:pk>/reorder/", v.StageReorderView.as_view(), name="erp-crm-stage-reorder"),
]
