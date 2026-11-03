"""The ClickUp task portal adapter (docs/crm/phase6-adapters.md 3.1, step 2).

ClickUp is the source of truth for the tasks it holds: title, description,
status, due date, priority and assignee. Golden creates tasks there (a task
whose ``clickup.list_id`` is set, or a ``push_rules`` kind) and mirrors them
back, so engagement threads, "My day" and the follow-up report see them.
Golden owns only the links ClickUp does not know: engagement, party, kind,
communication. Enable it with::

    GOLDEN_PLUGINS = ["golden_erp.addons.clickup.plugin.ClickUpPlugin"]

then add a ClickUp account under Connected accounts (docs/crm/adapters.md,
"ClickUp").
"""
