"""ClickUp facts golden cannot verify offline, isolated here (phase6-adapters.md 7).

Every value below comes from ClickUp's public API v2 documentation as golden
read it; the downstream confirms each against the live API before enabling
an account (docs/crm/phase6-downstream.md, step 9).
"""

from __future__ import annotations

PROVIDER = "clickup"
#: Newest envelope version the readers accept (ClickUp API v2 task shape).
SCHEMA_VERSION = 1
BASE_URL = "https://api.clickup.com"
DOCS = "See docs/crm/adapters.md#clickup."
APP_URL = "https://app.clickup.com"

#: ExternalRef.object_type for a mirrored task.
OBJECT_TASK = "task"

# -- auth and webhooks ----------------------------------------------------------
# A personal or workspace API token goes in the Authorization header as is
# (no "Bearer" prefix).
AUTH_HEADER = "Authorization"
# Webhooks: HMAC-SHA256 (hex) over the raw body, keyed by the secret ClickUp
# returns when the webhook is created. ClickUp signs no timestamp and sends no
# delivery id, so a replay is caught by the body hash (outbox idempotency)
# and, after that row is pruned, by ingest's own idempotency.
SIGNATURE_HEADER = "x-signature"

#: Webhook events that change a task golden mirrors (fetched, then ingested).
TASK_EVENTS = frozenset({
    "taskCreated", "taskUpdated", "taskStatusUpdated", "taskAssigneeUpdated",
    "taskDueDateUpdated", "taskPriorityUpdated", "taskMoved",
})
DELETED_EVENT = "taskDeleted"

# -- rate limit -------------------------------------------------------------------
# 100 requests per minute per token on the lower plans; an account on a
# bigger plan raises it with settings["rate_limit"].
RATE_REQUESTS = 100
RATE_PER_SECONDS = 60

# -- poll -------------------------------------------------------------------------
#: Tasks per page of GET /api/v2/list/{id}/task. With order_by=updated,
#: reverse=true is read as newest first: confirm the direction against the
#: live API (the poll's no-skip argument depends on it).
PAGE_SIZE = 100
#: How far each poll re-reads behind a list's watermark (ingest is idempotent).
POLL_OVERLAP_MS = 60_000

# -- descriptions -------------------------------------------------------------------
# Golden pushes plain ``description`` and reads ``markdown_description`` first.
# Confirm ClickUp returns the pushed text unchanged in markdown_description;
# if it normalises it, golden's copy is rewritten on the next echo (no loop:
# inbound never pushes).

# -- mapping ----------------------------------------------------------------------
#: ClickUp priority id -> golden priority. 1 urgent, 2 high, 3 normal, 4 low.
PRIORITY_IN = {1: "high", 2: "high", 3: "normal", 4: "low"}
PRIORITY_OUT = {"high": 2, "normal": 3, "low": 4}
#: Default golden status by ClickUp status *type* when the list's status_map
#: does not name the status.
STATUS_BY_TYPE = {"open": "open", "custom": "in_progress", "done": "done", "closed": "done"}
#: What the erase handler writes over a scrubbed task's name (4.1).
ERASED_NAME = "Erased"
#: Golden's title for a ClickUp task with an empty name.
UNTITLED = "Untitled"
#: Golden's title column.
TITLE_MAX = 255
