"""The ClickUp cases' seed: engagements, golden tasks (some already mirrored) and golden-side edits.

``case.json`` ``seed`` keys beyond the runner's own (parties, staff):

- ``engagements``: ``[{key, number, party}]``
- ``tasks``: ``[{key, title, engagement?, assignee?, due?, status?, list?,
  clickup_id?, date_updated?, assignee_clickup?, erased?}]``; a task with a
  ``clickup_id`` is already mirrored (its ``ExternalRef`` is written).

These are written inside ``suppressed()``: they are the state before the
case. ``golden`` then lists edits made live, which is what the outbox
hears: ``{"op": "create" | "complete" | "cancel" | "edit" | "reschedule" |
"assign", "task": key, ...}``.
"""

from __future__ import annotations

import datetime as dt

from django.contrib.contenttypes.models import ContentType

from core.models import ExternalRef, UserProfile
from core.plugins.dispatch import suppressed
from crm import engagements as engagement_services
from crm import tasks as task_services
from crm.models import Engagement, Task
from parties.models import Party


def _key(tenant, key):
    return next(pk for pk, value in tenant.keys.by_id.items() if value == key)


def _due(value):
    if not value:
        return None
    if "T" in value:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.date.fromisoformat(value)


def _profile(tenant, key):
    return UserProfile.objects.get(pk=_key(tenant, f"staff:{key}")) if key else None


def _create_task(tenant, spec):
    engagement = Engagement.objects.get(pk=_key(tenant, f"engagement:{spec['engagement']}")) \
        if spec.get("engagement") else None
    metadata = {"clickup.list_id": spec["list"]} if spec.get("list") else {}
    if spec.get("assignee_clickup"):
        metadata["clickup.assignee"] = spec["assignee_clickup"]
    task = task_services.create(
        company=tenant.company, engagement=engagement, title=spec["title"],
        assignee=_profile(tenant, spec.get("assignee")), due=_due(spec.get("due")),
        kind=spec.get("kind", "todo"), metadata=metadata,
    )
    tenant.keys.add(f"task:{spec['key']}", task)
    return task


def seed_hook(tenant, case):
    seed = case.get("seed") or {}
    with suppressed():
        for spec in seed.get("engagements", []):
            party = Party.objects.get(pk=_key(tenant, f"party:{spec['party']}"))
            engagement = engagement_services.create(company=tenant.company, party=party, title=spec.get("title", ""))
            Engagement._base_manager.filter(pk=engagement.pk).update(number=spec["number"])
            tenant.keys.add(f"engagement:{spec['key']}", engagement)
        for spec in seed.get("tasks", []):
            task = _create_task(tenant, spec)
            status = spec.get("status", "open")
            if status != "open":
                task_services.set_status(task, status)
            if spec.get("clickup_id"):
                ExternalRef.objects.create(
                    company=tenant.company, source="clickup", object_type="task", source_id=spec["clickup_id"],
                    content_type=ContentType.objects.get_for_model(Task), object_id=task.pk,
                    source_url=f"https://app.clickup.com/t/{spec['clickup_id']}",
                    metadata={**({"date_updated": spec["date_updated"]} if spec.get("date_updated") else {}),
                              **({"erased": True} if spec.get("erased") else {})},
                )
    for action in case.get("golden", []):
        op = action["op"]
        if op == "create":
            _create_task(tenant, action)
            continue
        task = Task.objects.get(pk=_key(tenant, f"task:{action['task']}"))
        if op == "complete":
            task_services.complete(task)
        elif op == "cancel":
            task_services.cancel(task)
        elif op == "edit":
            task_services.edit(task, **{k: v for k, v in action.items() if k in task_services.EDITABLE_FIELDS})
        elif op == "reschedule":
            task_services.reschedule(task, _due(action["due"]))
        elif op == "assign":
            task_services.assign(task, _profile(tenant, action.get("assignee")))
        else:
            raise AssertionError(f"unknown golden op {op!r}")
