"""``ClickUpAdapter``: the task portal (docs/crm/phase6-adapters.md 3.1).

Who owns what (A11, confirmed as OP #2247):

- **ClickUp** owns title, description, status, due date, priority and
  assignee of every task it holds. Inbound values win; a golden edit is
  pushed as the change it was (its outbox row pins the written values), and
  ClickUp's next echo settles both sides on what ClickUp holds.
- **Golden** owns the links ClickUp does not know: engagement, party, kind,
  communication. They are never pushed. The engagement custom field is read
  once, when golden first mirrors a task; afterwards a ClickUp-side value
  that disagrees with golden's link is **refused and reported** (flag
  ``refused_engagement``, a warning, the ingest outcome's reason), as is a
  golden-id field that names another task (``refused_golden_id``).

Echo loops: inbound writes run inside ``core.outbox.origin("clickup")``, so
the ``clickup.task`` consumer hears none of them; the push records the
``date_updated`` ClickUp answered in ``ExternalRef.metadata``, and an inbound
copy older than that is a stale delivery and changes nothing.

Erasure: :meth:`ClickUpAdapter.erase` renames the task to ``Erased`` and
clears its description and engagement field. It never deletes the ClickUp
task: it may be someone's real work, and golden only scrubs what it copied
there. ``parties.erasure`` calls :meth:`erasure_refs` and enqueues
``clickup.erase`` (step 8).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.integrations.ratelimit import RateLimit
from core.models import ExternalRef
from core.plugins.crm_adapter import (
    AdapterError,
    BaseCrmAdapter,
    Capability,
    IngestOutcome,
    PollPage,
    ProviderRef,
    Transient,
    WebhookCheck,
    account_handler,
    envelope,
)
from core.provenance import is_erased_source

from . import mapping, values

logger = logging.getLogger("golden_erp.integrations.clickup")

TASK_CONSUMER = f"{values.PROVIDER}.task"
#: Erase-row ref for a create golden sent but never recorded (``<list id>:<golden task id>``).
OBJECT_SEARCH = "golden_task"

# Task.metadata keys (provider namespace, phase4-fields.md 3.1).
KEY_LIST = "clickup.list_id"
KEY_ASSIGNEES = "clickup.assignees"
KEY_ASSIGNEE = "clickup.assignee"
KEY_FLAGS = "clickup.flags"
KEY_DELETED = "clickup.deleted_at"
KEY_LEFT = "clickup.left_at"  # moved in ClickUp to a list golden does not map: sync paused


def _stopped(task) -> bool:
    meta = task.metadata or {}
    return bool(meta.get(KEY_DELETED) or meta.get(KEY_LEFT))

GOLDEN_STATUSES = ("open", "in_progress", "waiting", "done", "cancelled")


def _settings_error(problem: str, fix: str) -> str:
    return f"{problem} — cause: the ClickUp account's settings. Fix: {fix} {values.DOCS}"


class ClickUpAdapter(BaseCrmAdapter):
    """API token + signed webhooks + a poll of the mapped lists + an idempotent task push."""

    provider_id = values.PROVIDER
    schema_version = values.SCHEMA_VERSION
    capabilities = frozenset({Capability.POLL, Capability.WEBHOOK, Capability.PUSH_TASKS, Capability.ERASE})
    rate_limit = RateLimit(requests=values.RATE_REQUESTS, per_seconds=values.RATE_PER_SECONDS)
    docs_url = values.DOCS
    base_url = values.BASE_URL
    settings_keys = frozenset({"lists", "push_rules"})
    credential_kinds = frozenset({"api_key", "webhook_secret"})
    push_dedupe = ("every created task carries golden's task id in the list's golden_id_field custom "
                   "field; a create whose answer was lost searches the list by that field before retrying")

    def __init__(self, account, transport):
        super().__init__(account, transport)
        self._members: dict[str, dict[str, str]] = {}

    # -- configuration ---------------------------------------------------------

    def lists(self) -> dict:
        lists = (self.account.settings or {}).get("lists") or {}
        return lists if isinstance(lists, dict) else {}

    def validate_settings(self, settings: dict) -> list[str]:
        errors = super().validate_settings(settings)
        if not isinstance(settings, dict):
            return errors
        from crm.registries import TASK_KINDS

        lists = settings.get("lists", {})
        if not isinstance(lists, dict):
            return errors + [_settings_error("lists must be an object of ClickUp list id -> list settings",
                                             'enter {"<list id>": {"golden_id_field": "<field id>"}}.')]
        for list_id, cfg in sorted(lists.items()):
            where = f"lists[{list_id!r}]"
            if not isinstance(cfg, dict):
                errors.append(_settings_error(f"{where} must be an object", "enter its settings object."))
                continue
            unknown = sorted(set(cfg) - {"golden_id_field", "engagement_field", "status_map", "push_status", "kind"})
            if unknown:
                errors.append(_settings_error(f"{where} has unknown keys {', '.join(unknown)}",
                                              "remove them."))
            if not isinstance(cfg.get("golden_id_field"), str) or not cfg.get("golden_id_field"):
                errors.append(_settings_error(
                    f"{where} needs golden_id_field (the text custom field that holds golden's task id)",
                    "create that field on the list and enter its id."))
            if "engagement_field" in cfg and not isinstance(cfg["engagement_field"], str):
                errors.append(_settings_error(f"{where}.engagement_field must be a field id", "correct it."))
            status_map = cfg.get("status_map", {})
            if not isinstance(status_map, dict) or any(
                    not isinstance(k, str) or v not in GOLDEN_STATUSES for k, v in status_map.items()):
                errors.append(_settings_error(
                    f"{where}.status_map maps ClickUp status names to {', '.join(GOLDEN_STATUSES)}",
                    "correct the map."))
            push_status = cfg.get("push_status", {})
            if not isinstance(push_status, dict) or any(
                    k not in GOLDEN_STATUSES or not isinstance(v, str) or not v for k, v in push_status.items()):
                errors.append(_settings_error(
                    f"{where}.push_status maps golden statuses to ClickUp status names",
                    "correct the map."))
            elif isinstance(status_map, dict):
                back = {str(k).strip().lower(): v for k, v in status_map.items()}
                for golden, name in sorted(push_status.items()):
                    if back.get(name.strip().lower()) != golden:
                        # The echo of the push must map back, or it reads as another status.
                        errors.append(_settings_error(
                            f"{where}.push_status sends {golden} as {name!r}, which status_map does not map "
                            f"back to {golden}", f"add \"{name}\": \"{golden}\" to status_map."))
            if "kind" in cfg and cfg["kind"] not in TASK_KINDS:
                errors.append(_settings_error(f"{where}.kind {cfg['kind']!r} is not a task kind",
                                              "use a GOLDEN_TASK_KINDS key."))
        rules = settings.get("push_rules", {})
        if not isinstance(rules, dict):
            errors.append(_settings_error("push_rules must be an object of task kind -> list id", "correct it."))
        else:
            for kind, list_id in sorted(rules.items()):
                if kind not in TASK_KINDS:
                    errors.append(_settings_error(f"push_rules names unknown task kind {kind!r}",
                                                  "use a GOLDEN_TASK_KINDS key."))
                if list_id not in lists:
                    errors.append(_settings_error(f"push_rules[{kind!r}] names list {list_id!r}, which is not "
                                                  "in lists", "add the list or correct the rule."))
        return errors

    # -- HTTP --------------------------------------------------------------------

    def _auth(self) -> dict:
        return {values.AUTH_HEADER: self.secret("api_key").get()}

    def fetch_task(self, task_id: str) -> dict | None:
        """The current task, or None when ClickUp no longer has it (404)."""
        response = self.http("GET", f"/api/v2/task/{task_id}", headers=self._auth(),
                             params={"include_markdown_description": "true"}, expect=(404,))
        if response.status == 404:
            return None
        return _object(response.json(), self.provider_id)

    def member_id(self, list_id: str, email: str) -> str | None:
        """The ClickUp user id of the list member with ``email`` (one lookup per list per run)."""
        if not email:
            return None
        if list_id not in self._members:
            body = self.http("GET", f"/api/v2/list/{list_id}/member", headers=self._auth()).json() or {}
            members = body.get("members") if isinstance(body, dict) else None
            self._members[list_id] = {
                str(m.get("email") or "").strip().lower(): str(m["id"])
                for m in (members or []) if isinstance(m, dict) and m.get("id") is not None
            }
        return self._members[list_id].get(email.strip().lower())

    # -- inbound -------------------------------------------------------------------

    def verify_webhook(self, headers, body: bytes) -> WebhookCheck:
        signature = headers.get(values.SIGNATURE_HEADER, "")
        if not signature:
            return WebhookCheck(ok=False, reason="missing_signature")
        secret = self.secret("webhook_secret").get().encode()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.strip().lower()):
            return WebhookCheck(ok=False, reason="bad_signature")
        return WebhookCheck(ok=True)  # no delivery id, no signed timestamp: the body hash dedupes

    def poll(self, cursor: dict, *, credential=None) -> PollPage:
        """The backstop: every mapped list's tasks updated since the cursor, page by page.

        Cursor: ``{"since": <first-sync ISO>, "lists": {list id: ms}, "pos": i,
        "page": n, "max": ms}``. One list per call; the list's watermark moves
        to the newest ``date_updated`` seen when its last page is read. Pages
        are newest first (``reverse``; values.py), so a task updated during the
        run moves to the front and can only be read twice, never skipped; the
        query overlaps the watermark by ``POLL_OVERLAP_MS``.
        """
        list_ids = sorted(self.lists())
        if not list_ids:
            return PollPage(items=[], cursor=dict(cursor or {}), more=False)
        cursor = dict(cursor or {})
        marks = dict(cursor.get("lists") or {})
        pos = int(cursor.get("pos") or 0) % len(list_ids)
        page = int(cursor.get("page") or 0)
        list_id = list_ids[pos]
        since = marks.get(list_id)
        if since is None and cursor.get("since"):
            since = mapping.dt_to_ms(mapping.parse_iso(cursor["since"]))
        params = {"include_closed": "true", "subtasks": "true", "include_markdown_description": "true",
                  "order_by": "updated", "reverse": "true", "page": str(page)}
        if since:
            # Overlap the watermark: date_updated_gt is strict, and a task updated
            # mid-run can shift across a page boundary. Ingest is idempotent.
            params["date_updated_gt"] = str(max(0, int(since) - values.POLL_OVERLAP_MS))
        body = _object(self.http("GET", f"/api/v2/list/{list_id}/task", headers=self._auth(),
                                 params=params).json(), self.provider_id)
        tasks = [t for t in (body.get("tasks") or []) if isinstance(t, dict)]
        newest = max([int(cursor.get("max") or 0)] + [mapping.ms(t.get("date_updated")) for t in tasks])
        items = [envelope(self.provider_id, t, schema_version=self.schema_version) for t in tasks]
        last_page = body.get("last_page")
        if last_page is None:  # absent: a short page is the last one
            last_page = len(tasks) < values.PAGE_SIZE
        if tasks and not last_page:
            nxt = {**cursor, "lists": marks, "pos": pos, "page": page + 1, "max": newest}
            return PollPage(items=items, cursor=nxt, more=True)
        if newest:
            marks[list_id] = newest
        nxt = {k: v for k, v in cursor.items() if k not in ("pos", "page", "max")}
        nxt["lists"] = marks
        if pos + 1 < len(list_ids):
            nxt.update(pos=pos + 1, page=0)
            return PollPage(items=items, cursor=nxt, more=True)
        return PollPage(items=items, cursor=nxt, more=False)

    def ingest(self, item: dict) -> list[IngestOutcome]:
        """One inbound item, any path (rule 3). Payload shapes:

        - a ClickUp task object (a poll page item);
        - a ClickUp webhook body (``event``, ``task_id``, ``history_items``):
          the task is fetched, a ``taskDeleted`` is not;
        - ``{"webhook": <body>, "task": <task>}``: a webhook whose task the
          ``clickup.inbound`` handler fetched before opening a transaction.
        """
        from core import outbox

        payload = self.read_envelope(item)  # rule 2: a newer envelope is refused here
        if not isinstance(payload, dict):
            return [IngestOutcome(changed=False, skip=True, reason="not_an_object")]
        with outbox.origin(self.provider_id):
            if "webhook" in payload and "task" in payload:
                hook = payload["webhook"] if isinstance(payload["webhook"], dict) else {}
                return [self._ingest_task(payload["task"], by_email=_history_email(hook))]
            if "event" in payload:
                return [self._ingest_webhook(payload)]
            return [self._ingest_task(payload)]

    def _ingest_webhook(self, hook: dict) -> IngestOutcome:
        event = str(hook.get("event") or "")
        task_id = str(hook.get("task_id") or "")
        if not task_id:
            return IngestOutcome(changed=False, skip=True, reason="no_task_id")
        if event == values.DELETED_EVENT:
            return self._deleted(task_id, at=_history_at(hook))
        if event not in values.TASK_EVENTS:
            return IngestOutcome(changed=False, skip=True, reason="unsupported_event", external_id=task_id)
        if is_erased_source(self.account.company_id, self.provider_id, values.OBJECT_TASK, task_id):
            return IngestOutcome(changed=False, skip=True, reason="erased_source", external_id=task_id)
        task = self.fetch_task(task_id)
        if task is None:
            return IngestOutcome(changed=False, skip=True, reason="not_found", external_id=task_id)
        return self._ingest_task(task, by_email=_history_email(hook))

    def _ingest_task(self, data, *, by_email: str = "") -> IngestOutcome:
        from crm.models import Task

        if not isinstance(data, dict) or not data.get("id"):
            return IngestOutcome(changed=False, skip=True, reason="no_id")
        company = self.account.company
        cu_id = str(data["id"])
        # Matching (2.4): tombstones first.
        if is_erased_source(company.pk, self.provider_id, values.OBJECT_TASK, cu_id):
            return IngestOutcome(changed=False, skip=True, reason="erased_source", external_id=cu_id)
        list_id = str((data.get("list") or {}).get("id") or "")
        cfg = self.lists().get(list_id)
        ref = _ref(company, cu_id)
        if not isinstance(cfg, dict):
            if ref is not None:
                return self._left(ref, cu_id)
            return IngestOutcome(changed=False, skip=True, reason="list_not_mapped", external_id=cu_id)
        golden_id = mapping.custom_field(data, cfg.get("golden_id_field", ""))
        engagement_no = mapping.custom_field(data, cfg.get("engagement_field", ""))
        refused: list[str] = []
        created = False
        if ref is not None:
            task = Task._base_manager.filter(company=company, pk=ref.object_id).first()
            if task is None:
                return IngestOutcome(changed=False, skip=True, reason="target_missing", external_id=cu_id)
            if golden_id and golden_id != str(task.pk):
                refused.append("golden_id")
        elif golden_id:
            task = Task._base_manager.filter(company=company, pk=_uuid(golden_id)).first() if _uuid(golden_id) else None
            if task is None:
                return IngestOutcome(changed=False, skip=True, reason="golden_id_unknown", external_id=cu_id)
            if _ref_for_task(task) is None and _erased_task(task) and _created_before_erasure(data, task):
                # ClickUp holds a task golden created but never recorded (its
                # answer was lost) and erasure has since scrubbed: adopting it
                # would write the erased text back. Rename it at ClickUp instead.
                from core.integrations.erasure import erase_late

                # The ref is written tombstoned first: ClickUp's own webhook for
                # the rename then stops at the tombstone instead of erasing again.
                _write_ref(company, task, cu_id, "", "")
                ExternalRef._base_manager.filter(
                    company=company, source=values.PROVIDER, object_type=values.OBJECT_TASK, source_id=cu_id,
                ).update(metadata={"erased": True, "erased_at": timezone.now().isoformat()})
                erase_late(self, [ProviderRef(object_type=values.OBJECT_TASK, source_id=cu_id)],
                           party_id=_erased_party_id(task), reason="unrecorded_create")
                return IngestOutcome(changed=False, skip=True, reason="erased_task", external_id=cu_id)
            if _ref_for_task(task) is not None:
                # Another ClickUp task already mirrors it (a copy made in ClickUp).
                logger.warning("[integrations] clickup task %s names golden task %s, already mirrored; refused",
                               cu_id, task.pk)
                return IngestOutcome(changed=False, skip=True, reason="refused:golden_id_taken",
                                     external_id=cu_id)
        elif engagement_no:
            task = None
            if _erased_engagement(company.pk, engagement_no):
                # A task golden never mirrored, naming an erased person's deal:
                # creating it would give them back a task (Task copies the
                # engagement's party). Erasure's tombstones cannot name it.
                return IngestOutcome(changed=False, skip=True, reason="erased_party", external_id=cu_id)
        else:
            # Neither golden's id nor an engagement number: ClickUp's own business.
            return IngestOutcome(changed=False, skip=True, reason="unlinked", external_id=cu_id)
        if task is not None and (task.metadata or {}).get(KEY_DELETED):
            return IngestOutcome(changed=False, skip=True, reason="deleted", external_id=cu_id)
        acked = mapping.ms((ref.metadata or {}).get("date_updated")) if ref is not None else 0
        seen = mapping.ms(data.get("date_updated"))
        if ref is not None and seen and seen < acked:
            # Older than the last edit ClickUp acknowledged: a stale delivery.
            return IngestOutcome(changed=False, skip=True, reason="stale", external_id=cu_id)

        by = _staff(company, by_email)
        at = mapping.ms_to_dt(data.get("date_updated"))
        flags: set[str] = set()
        before_flags = set((task.metadata or {}).get(KEY_FLAGS) or []) if task is not None else set()
        with transaction.atomic():
            if task is not None:
                # Lock, then look again: an erasure that committed after the
                # tombstone check above scrubbed this task and flagged its ref.
                # Writing now would put the erased text back (step 8).
                fresh = Task._base_manager.select_for_update().filter(pk=task.pk).first()
                if fresh is None or is_erased_source(company.pk, values.PROVIDER, values.OBJECT_TASK, cu_id) \
                        or _erased_party_id(fresh) is not None:
                    return IngestOutcome(changed=False, skip=True, reason="erased_source", external_id=cu_id)
            if task is None:
                task, flags = self._create_mirror(data, cfg, list_id, engagement_no, by)
                created = changed = True
            else:
                changed = self._apply(task, data, cfg, by, at, flags)
                changed |= self._engagement_link(task, engagement_no, refused, flags)
            flags |= {f"refused_{name}" for name in refused}
            changed |= self._write_mirror_meta(task, data, list_id, flags)
            changed |= self._ack(company, task, cu_id, data, ref)
        newly = {f"refused_{name}" for name in refused} - before_flags
        if newly:  # once per refusal, not on every later copy
            logger.warning("[integrations] clickup task %s: ClickUp changed golden-owned %s; refused (golden "
                           "keeps its value)", cu_id, ", ".join(sorted(n.removeprefix("refused_") for n in newly)))
        reason = "refused:" + ",".join(sorted(refused)) if refused else ""
        return IngestOutcome(changed=changed, created=created, reason=reason, external_id=cu_id)

    def _engagement_link(self, task, engagement_no: str, refused: list, flags: set) -> bool:
        """Golden owns the link, read once when golden first mirrors a task. A mirror whose
        number did not resolve then is linked when it does; any other change is refused
        (reported, never applied)."""
        from crm.models import Engagement

        if not engagement_no:
            return False
        if task.engagement_id is not None:
            if engagement_no != _engagement_number(task):
                refused.append("engagement")
            return False
        if task.source != values.PROVIDER or "engagement_unresolved" not in ((task.metadata or {}).get(KEY_FLAGS) or []):
            # Only a mirror whose first number did not resolve still waits for its link;
            # any other empty link is golden's choice (left or made unlinked).
            refused.append("engagement")
            return False
        engagement = Engagement._base_manager.filter(company_id=task.company_id, number=engagement_no).first()
        if engagement is None:
            flags.add("engagement_unresolved")
            return False
        if _erased_engagement(task.company_id, engagement_no):
            return False  # never link a mirror to an erased person's deal
        task.engagement = engagement
        fields = {"engagement", "updated_at"}
        if task.party_id is None:
            task.party_id = engagement.party_id
            fields.add("party")
        task.save(update_fields=fields)  # inside origin: task_updated, no ClickUp column, no push
        return True

    def _left(self, ref, cu_id: str) -> IngestOutcome:
        """Moved in ClickUp to a list golden does not map: pause the sync (resumed if it comes back).

        No opposite write: ClickUp owns the task, and archiving someone's work
        item because it changed lists would destroy it (A11 over 2.5).
        """
        from crm.models import Task

        task = Task._base_manager.filter(company=self.account.company, pk=ref.object_id).first()
        if task is None or _stopped(task):
            return IngestOutcome(changed=False, skip=True, reason="list_not_mapped", external_id=cu_id)
        # KEY_LIST stays: erasure still needs the list's engagement field.
        meta = {**(task.metadata or {}), KEY_LEFT: mapping.iso(timezone.now())}
        Task._base_manager.filter(pk=task.pk).update(metadata=meta)
        return IngestOutcome(changed=True, reason="left_mapped_lists", external_id=cu_id)

    def _create_mirror(self, data: dict, cfg: dict, list_id: str, engagement_no: str, by):
        from crm import tasks
        from crm.models import Engagement

        company = self.account.company
        flags: set[str] = set()
        engagement = Engagement._base_manager.filter(company=company, number=engagement_no).first()
        if engagement is None:
            flags.add("engagement_unresolved")
        title, truncated = mapping.title_in(data.get("name"))
        if truncated:
            flags.add("title_truncated")
        assignee, assignee_flag = self._assignee(company, data)
        flags |= assignee_flag
        zone = tasks.zone_for(assignee, company)
        task = tasks.create(
            company=company, engagement=engagement, title=title or values.UNTITLED,
            description=mapping.description_in(data), assignee=assignee, by=by,
            due=mapping.due_in(data, zone), priority=mapping.priority_in(data.get("priority")),
            kind=cfg.get("kind", "todo"), source=values.PROVIDER, metadata={KEY_LIST: list_id},
            at=mapping.ms_to_dt(data.get("date_created")),
        )
        self._apply_status(task, data, cfg, by, mapping.ms_to_dt(data.get("date_updated")), flags)
        return task, flags

    def _assignee(self, company, data: dict):
        """The first ClickUp assignee who is active staff of the company (by email), else None."""
        people = mapping.assignees(data)
        for person in people:
            profile = _staff(company, person["email"])
            if profile is not None:
                return profile, set()
        return None, ({"assignee_unmatched"} if people else set())

    def _apply(self, task, data: dict, cfg: dict, by, at, flags: set) -> bool:
        """Write ClickUp's values through ``crm.tasks`` (TaskEvents, signals); True when anything changed."""
        from zoneinfo import ZoneInfo

        from crm import tasks

        before = (task.title, task.description, task.priority, task.assignee_id, task.due_at, task.all_day,
                  task.status)
        title, truncated = mapping.title_in(data.get("name"))
        if truncated:
            flags.add("title_truncated")
        tasks.edit(task, by=by, at=at, title=title or task.title, description=mapping.description_in(data),
                   priority=mapping.priority_in(data.get("priority")))
        assignee, assignee_flag = self._assignee(task.company, data)
        flags |= assignee_flag
        tasks.assign(task, assignee, by=by, at=at)  # ClickUp owns it: unmatched means unassigned here
        zone = ZoneInfo(task.due_tz) if task.due_tz else tasks.zone_for(task.assignee, task.company)
        due = mapping.due_in(data, zone)
        new_at, new_all_day = tasks.due_instant(due, zone)
        # ClickUp keeps milliseconds: compare there, or every echo of a golden
        # due with microseconds would reschedule (and re-arm task_due).
        if (mapping.dt_to_ms(new_at), new_all_day if new_at else True) != \
                (mapping.dt_to_ms(task.due_at), task.all_day if task.due_at else True):
            tasks.reschedule(task, due, by=by, at=at)
        self._apply_status(task, data, cfg, by, at, flags)
        return before != (task.title, task.description, task.priority, task.assignee_id, task.due_at,
                          task.all_day, task.status)

    def _apply_status(self, task, data: dict, cfg: dict, by, at, flags: set) -> None:
        from crm import tasks

        status = mapping.status_in(data.get("status"), cfg)
        if status is None:
            flags.add("status_unmapped")
            return
        if status == task.status:
            return
        when = mapping.done_at(data) if status == "done" else at
        try:
            tasks.set_status(task, status, by=by, at=when or at)
        except ValidationError:
            # cancel() refuses a done task: its completion is history.
            flags.add("status_refused")

    def _write_mirror_meta(self, task, data: dict, list_id: str, flags: set) -> bool:
        from crm.models import Task

        people = mapping.assignees(data)
        meta = dict(task.metadata or {})
        meta[KEY_LIST] = list_id
        meta.pop(KEY_LEFT, None)  # back in a mapped list: the sync resumes
        meta[KEY_ASSIGNEES] = [p["id"] for p in people]
        mirrored = None
        if task.assignee_id is not None:
            email = (getattr(getattr(task.assignee, "user", None), "email", "") or "").strip().lower()
            mirrored = next((p["id"] for p in people if p["email"] == email), None)
        _set_or_drop(meta, KEY_ASSIGNEE, mirrored)
        _set_or_drop(meta, KEY_FLAGS, sorted(flags) or None)
        if meta == (task.metadata or {}):
            return False
        Task._base_manager.filter(pk=task.pk).update(metadata=meta)  # no signal: not a ClickUp-owned column
        task.metadata = meta
        return True

    def _ack(self, company, task, cu_id: str, data: dict, ref) -> bool:
        """Create or refresh the ref: ``date_updated`` (the newest seen) and the task's URL."""
        seen = str(mapping.ms(data.get("date_updated")) or "")
        url = str(data.get("url") or "")[:200]
        if ref is None:
            _write_ref(company, task, cu_id, url, seen)
            return True
        meta = dict(ref.metadata or {})
        if seen and mapping.ms(seen) > mapping.ms(meta.get("date_updated")):
            meta["date_updated"] = seen
        if meta == (ref.metadata or {}) and (not url or url == ref.source_url):
            return False
        ExternalRef._base_manager.filter(pk=ref.pk).update(
            metadata=meta, source_url=url or ref.source_url, last_synced_at=timezone.now())
        return True

    def _deleted(self, cu_id: str, *, at=None) -> IngestOutcome:
        """Deleted in ClickUp: an open mirror is cancelled, a done one stays done; both stop syncing."""
        from crm import tasks
        from crm.models import Task

        company = self.account.company
        ref = _ref(company, cu_id)
        task = Task._base_manager.filter(company=company, pk=ref.object_id).first() if ref is not None else None
        if task is None:
            return IngestOutcome(changed=False, skip=True, reason="unknown_task", external_id=cu_id)
        if task.metadata.get(KEY_DELETED):
            return IngestOutcome(changed=False, external_id=cu_id)
        when = at or timezone.now()
        with transaction.atomic():
            if task.is_open:
                tasks.cancel(task, at=when)
            meta = {**(task.metadata or {}), KEY_DELETED: mapping.iso(when)}
            Task._base_manager.filter(pk=task.pk).update(metadata=meta)
            task.metadata = meta
        return IngestOutcome(changed=True, external_id=cu_id)

    # -- outbound ------------------------------------------------------------------

    @classmethod
    def outbox_consumers(cls) -> list:
        from core import outbox

        base = [c for c in super().outbox_consumers() if c.name != f"{cls.provider_id}.inbound"]
        return base + [
            # The inbound webhook row: fetch the task with no transaction open,
            # then ingest it inside origin("clickup") (A10).
            outbox.OutboxConsumer(name=f"{cls.provider_id}.inbound", handle=account_handler(cls, handle_inbound),
                                  provider=cls.provider_id),
            outbox.OutboxConsumer(name=TASK_CONSUMER, signals=("task_created", "task_updated"),
                                  build=build_push, handle=account_handler(cls, handle_push),
                                  provider=cls.provider_id, enabled=_any_enabled),
        ]

    # -- erasure (4.1; parties.erasure calls these from step 8) --------------------

    def erasure_refs(self, footprint) -> list[ProviderRef]:
        """The ClickUp tasks mirroring the tasks golden's erasure scrubs (``footprint.tasks``)."""
        from crm.models import Task

        task_ids = list(getattr(footprint, "tasks", None) or [])
        if not task_ids:
            return []
        rows = ExternalRef._base_manager.filter(
            company=self.account.company, source=self.provider_id, object_type=values.OBJECT_TASK,
            content_type=ContentType.objects.get_for_model(Task), object_id__in=task_ids,
        ).order_by("source_id").values_list("object_id", "source_id")
        refs, mirrored = [], set()
        for object_id, sid in rows:
            refs.append(ProviderRef(object_type=values.OBJECT_TASK, source_id=sid))
            mirrored.add(str(object_id))
        # A create that was sent but never recorded (its answer lost, its worker
        # gone): ClickUp may hold the task with no ref in golden. erase_party is
        # about to delete or tombstone that row, the only trace of it, so the
        # erase row searches the list by the golden id field instead.
        from django.db.models import Q

        from core.models import OutboxEvent

        unknown = OutboxEvent.objects.filter(
            Q(attempts__gt=0) | Q(payload__has_key="create_unknown"),
            company=self.account.company, consumer=TASK_CONSUMER, object_type="crm.task",
            object_id__in=task_ids, state__in=("pending", "running", "dead"), payload__op="create",
        ).values_list("object_id", "payload")
        for object_id, payload in unknown.order_by("object_id"):
            list_id = str((payload or {}).get("list_id") or "")
            if list_id and str(object_id) not in mirrored:
                refs.append(ProviderRef(object_type=OBJECT_SEARCH, source_id=f"{list_id}:{object_id}"))
        return refs

    def erase(self, refs: list[ProviderRef], *, done=None):
        """Rename each task to ``Erased``, clear its description and engagement field. No delete.

        Called by the shared ``clickup.erase`` consumer (core.integrations.erasure);
        ``done(ref)`` after each task, so a retry resumes with the rest. A
        ``golden_task`` ref (``<list id>:<golden task id>``) is a create golden
        never recorded: the list is searched by the golden id field first.
        """
        for ref in refs:
            if ref.object_type == values.OBJECT_TASK:
                self.scrub_task(ref.source_id)
            elif ref.object_type == OBJECT_SEARCH:
                list_id, _, golden_id = ref.source_id.partition(":")
                cfg = self.lists().get(list_id)
                if not isinstance(cfg, dict) or not cfg.get("golden_id_field"):
                    raise AdapterError(f"clickup: cannot search list {list_id} for an unrecorded task — cause: "
                                       f"the list left `lists`. Fix: map it again, then requeue. {values.DOCS}")
                found = _search(self, list_id, cfg["golden_id_field"], golden_id)
                if found is not None:
                    self.scrub_task(str(found["id"]))
            if done is not None:
                done(ref)

    def scrub_task(self, cu_id: str) -> bool:
        """The rename-and-clear operation. False when ClickUp no longer has the task."""
        response = self.http("PUT", f"/api/v2/task/{cu_id}", headers=self._auth(), json=mapping.erase_body(),
                             expect=(404,))
        if response.status == 404:
            return False
        field = self._engagement_field_for(cu_id, response)
        if field:
            self.http("DELETE", f"/api/v2/task/{cu_id}/field/{field}", headers=self._auth(), expect=(404,))
        return True

    def _engagement_field_for(self, cu_id: str, response=None) -> str:
        """The list's engagement custom field. The list comes from ClickUp's
        answer to the rename (the task as it is now): after an erasure the
        golden task's ``metadata`` no longer holds it (provider keys leave the
        person's own rows), so the mirror's metadata is only a fallback."""
        from crm.models import Task

        list_id = None
        try:
            body = response.json() if response is not None else None
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("list"), dict):
            list_id = str(body["list"].get("id") or "") or None
        if list_id is None:
            company = self.account.company
            ref = _ref(company, cu_id)
            task = Task._base_manager.filter(company=company, pk=ref.object_id).only("metadata").first() \
                if ref is not None else None
            list_id = (task.metadata or {}).get(KEY_LIST) if task is not None else None
        cfg = self.lists().get(list_id) if list_id else None
        return (cfg or {}).get("engagement_field", "") if isinstance(cfg, dict) else ""

    # -- connection test -------------------------------------------------------------

    def test_connection(self) -> dict:
        from core.integration_checks import result

        response = self.http("GET", "/api/v2/user", headers=self._auth())
        return result(True, "Connected to ClickUp.", http_status=response.status)


# -- outbox: build (inside the business transaction) ----------------------------------


def _any_enabled() -> bool:
    from core.models import IntegrationAccount

    return IntegrationAccount.objects.filter(provider=values.PROVIDER, enabled=True).exists()


def build_push(signal_name, sender, obj, kwargs) -> list:
    """``clickup.task`` rows for a golden task save. Cheap: one query (the account), two for a
    metadata-only save.

    Created: one row keyed ``<task>:created``. Updated: one row per save,
    keyed ``<task>:<updated_at>:<digest of the pinned values>``, pinning the
    ClickUp-owned columns the save changed and their new values (the change,
    not the state; ``changed_fields``, so a full ``save()`` of an untouched
    task resends nothing). A save that changed none of them is no row, except
    a metadata save that gave a never-pushed task its list: that is its
    create. Never runs for inbound writes: ``origin("clickup")`` skips it.
    """
    from core import outbox
    from core.models import IntegrationAccount

    task = kwargs.get("task") or obj
    if _stopped(task):
        return []
    account = IntegrationAccount.objects.filter(company_id=task.company_id, provider=values.PROVIDER,
                                                enabled=True).only("settings").first()
    if account is None:
        return []
    settings = account.settings or {}
    lists = settings.get("lists") or {}
    list_id = (task.metadata or {}).get(KEY_LIST) or (settings.get("push_rules") or {}).get(task.kind)
    if not list_id or list_id not in lists:
        return []
    common = dict(company_id=task.company_id, object_type="crm.task", object_id=task.pk,
                  party_id=task.party_id)
    create = outbox.EventSpec(idempotency_key=f"{task.pk}:created", event="create", occurred_at=task.created_at,
                              payload={"task": str(task.pk), "op": "create", "list_id": list_id}, **common)
    if signal_name == "task_created":
        return [create]
    written = kwargs.get("changed_fields")
    if written is None:
        written = kwargs.get("update_fields") or ()
    columns = [c for c in written if c in mapping.SYNCED_COLUMNS]
    if not columns:
        if "metadata" in written and _ref_for_task(task) is None:
            return [create]
        return []
    fields = mapping.golden_values(task, columns)
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:12]
    return [outbox.EventSpec(
        idempotency_key=f"{task.pk}:{task.updated_at.isoformat()}:{digest}", event="update",
        occurred_at=task.updated_at,
        payload={"task": str(task.pk), "op": "update", "list_id": list_id, "fields": fields}, **common)]


# -- outbox: handlers (no transaction open) -----------------------------------------------


def handle_inbound(adapter: ClickUpAdapter, event):
    """``clickup.inbound``: fetch outside any transaction, then ingest inside origin + atomic."""
    from core import outbox

    item = event.payload or {}
    hook = adapter.read_envelope(item)
    if isinstance(hook, dict) and hook.get("event") in values.TASK_EVENTS and hook.get("task_id"):
        task_id = str(hook["task_id"])
        if is_erased_source(adapter.account.company_id, adapter.provider_id, values.OBJECT_TASK, task_id):
            return outbox.Done()
        task = adapter.fetch_task(task_id)
        if task is None:
            return outbox.Done()
        item = envelope(adapter.provider_id, {"webhook": hook, "task": task}, schema_version=adapter.schema_version)
    with outbox.origin(adapter.provider_id), transaction.atomic():
        adapter.ingest(item)
    return outbox.Done()


class DuplicateTask(AdapterError):
    code = "duplicate_task"


def handle_push(adapter: ClickUpAdapter, event):
    """``clickup.task``: create the task (golden id field, then the ref) or apply one pinned change."""
    from core import outbox
    from core.models import OutboxEvent
    from crm.models import Task

    payload = event.payload or {}
    if payload.get("erased"):
        return outbox.Cancel("erased")
    task_pk = _uuid(payload.get("task"))
    task = Task._base_manager.filter(company=adapter.account.company, pk=task_pk) \
        .select_related("assignee__user").first() if task_pk else None
    if task is None:
        return outbox.Cancel("task_gone")
    if _erased_task(task, event):
        # Queued before erasure scrubbed it (or the erased person's own task):
        # its fields may be what erasure removed; the erase row speaks for it.
        return outbox.Cancel("erased")
    if _stopped(task):
        return outbox.Cancel("stopped_syncing")
    list_id = payload.get("list_id")
    cfg = adapter.lists().get(list_id)
    if not isinstance(cfg, dict):
        return outbox.Dead("list_not_mapped")
    ref = _ref_for_task(task)
    if ref is None:
        if payload.get("op") == "update" and OutboxEvent.objects.filter(
                company=adapter.account.company, consumer=TASK_CONSUMER, idempotency_key=f"{task.pk}:created",
                state="pending").exists():
            # The create sends the task's state as it is when it runs: this change included.
            return outbox.Done()
        if payload.get("op") == "update" and OutboxEvent.objects.filter(
                company=adapter.account.company, consumer=TASK_CONSUMER, idempotency_key=f"{task.pk}:created",
                state="running").exists():
            # A running create may have read the task before this change: wait for its ref.
            return outbox.Retry(after=60, error="awaiting_create")
        return _create(adapter, task, list_id, cfg, event)
    if payload.get("op") == "create":
        return outbox.Done()  # a replay: the task exists
    return _update(adapter, task, ref, list_id, cfg, payload.get("fields") or {}, event)


def _assignee_email(task) -> str:
    user = getattr(task.assignee, "user", None) if task.assignee_id else None
    return (getattr(user, "email", "") or "").strip().lower()


def _create(adapter: ClickUpAdapter, task, list_id: str, cfg: dict, event):
    from core import outbox
    from core.models import OutboxEvent

    golden_field = cfg["golden_id_field"]
    # Any retry may follow a create ClickUp made but golden never recorded (a lost
    # answer, a crashed worker, an expired lease): search the golden id first (rule 4).
    if (event.payload or {}).get("create_unknown") or (getattr(event, "attempts", 1) or 1) > 1:
        found = _search(adapter, list_id, golden_field, str(task.pk))
        if found is not None:
            if _adopt(adapter, task, list_id, found, event=event):
                return outbox.Done()  # erased meanwhile: an erase row renames it (never push values)
            # The adopted task holds the first attempt's values; updates that waited
            # on this create were Done, so send golden's values as they are now.
            values_now = mapping.golden_values(task)
            if task.status == "open" and mapping.status_out("open", cfg) is None:
                values_now.pop("status")  # as the create: ClickUp's default open status
            return _update(adapter, task, _ref_for_task(task), list_id, cfg, values_now, event)
    status_name = mapping.status_out(task.status, cfg)
    if status_name is None and task.status != "open":
        return outbox.Dead("status_unmapped")
    member = adapter.member_id(list_id, _assignee_email(task)) if task.assignee_id else None
    body = mapping.create_body(mapping.golden_values(task), status_name=status_name, golden_id=str(task.pk),
                               golden_id_field=golden_field, assignee_id=member)
    try:
        response = adapter.http("POST", f"/api/v2/list/{list_id}/task", headers=adapter._auth(), json=body)
    except Transient:
        # ClickUp may have created it: the retry searches the golden id field first.
        flagged = {**(event.payload or {}), "create_unknown": True}
        if OutboxEvent.objects.filter(pk=event.pk).exclude(payload__erased=True).update(payload=flagged):
            event.payload = flagged
        raise
    _adopt(adapter, task, list_id, _object(response.json(), adapter.provider_id), mirrored=member, event=event)
    return outbox.Done()


def _search(adapter: ClickUpAdapter, list_id: str, field_id: str, golden_id: str) -> dict | None:
    query = json.dumps([{"field_id": field_id, "operator": "=", "value": golden_id}], separators=(",", ":"))
    body = _object(adapter.http("GET", f"/api/v2/list/{list_id}/task", headers=adapter._auth(),
                                params={"custom_fields": query, "include_closed": "true"}).json(),
                   adapter.provider_id)
    hits = sorted((t for t in body.get("tasks") or [] if isinstance(t, dict) and t.get("id")),
                  key=lambda t: str(t["id"]))
    return hits[0] if hits else None


def _adopt(adapter: ClickUpAdapter, task, list_id: str, data: dict, *, mirrored=None, event=None) -> bool:
    """Write the ref (with ClickUp's acknowledged ``date_updated``) and the list on the task.

    True when the task was erased while this push was in flight (step 8): the
    worker read it before the erasure and ClickUp now holds what it sent. The
    ref is then written already tombstoned and a ``clickup.erase`` row renames
    the task. The task lock serialises this with ``erase_party``, which
    updates the task before it collects the refs: whichever commits first,
    the ClickUp id is never missed.
    """
    from core.integrations.erasure import erase_late, tombstoned
    from crm.models import Task

    cu_id = str(data.get("id") or "")
    if not cu_id:
        raise AdapterError(f"clickup: the task answer has no id — cause: an unexpected response. "
                           f"Fix: check the API version. {values.DOCS}")
    with transaction.atomic():
        # Lock the task: two workers adopting at once must not both pass the check.
        current = Task._base_manager.select_for_update().filter(pk=task.pk).first()
        if current is not None and _erased_task(current, event) or tombstoned(event):
            _write_ref(task.company, task, cu_id, "", "")
            ExternalRef._base_manager.filter(
                company=task.company, source=values.PROVIDER, object_type=values.OBJECT_TASK, source_id=cu_id,
            ).update(metadata={"erased": True, "erased_at": timezone.now().isoformat()})
            erase_late(adapter, [ProviderRef(object_type=values.OBJECT_TASK, source_id=cu_id)],
                       party_id=_erased_party_id(current), reason="push_in_flight")
            return True
        existing = _ref_for_task(task)
        if existing is not None and existing.source_id != cu_id:
            # Two creates raced (two workers): never two refs for one golden task.
            logger.warning("[integrations] clickup: golden task %s is already ClickUp %s; ClickUp %s is a "
                           "duplicate to delete there", task.pk, existing.source_id, cu_id)
            raise DuplicateTask(f"clickup: golden task already mirrored — cause: two creates raced. Fix: delete "
                                f"the duplicate ClickUp task. {values.DOCS}")
        _write_ref(task.company, task, cu_id, str(data.get("url") or "")[:200],
                   str(mapping.ms(data.get("date_updated")) or ""))
        meta = {**(task.metadata or {}), KEY_LIST: list_id}
        if mirrored is None:
            email = _assignee_email(task)
            mirrored = next((p["id"] for p in mapping.assignees(data) if email and p["email"] == email), None)
        _set_or_drop(meta, KEY_ASSIGNEE, mirrored)
        Task._base_manager.filter(pk=task.pk).update(metadata=meta)
        task.metadata = meta
    return False


def _update(adapter: ClickUpAdapter, task, ref, list_id: str, cfg: dict, fields: dict, event):
    """Apply one pinned golden change. A value a newer row already pushed is not resent
    (``ExternalRef.metadata["pushed"]``: per value, the newest change ClickUp acknowledged),
    so a retried older row never rolls a newer edit back."""
    from core import outbox
    from core.integrations.erasure import tombstoned
    from crm.models import Task

    occurred_at = getattr(event, "occurred_at", None)
    occurred = mapping.iso(occurred_at) or ""
    pushed = dict((ref.metadata or {}).get("pushed") or {})

    def already(key):  # a newer change, or this very row, already sent it
        mark = mapping.parse_iso(pushed.get(key))
        return occurred_at is not None and mark is not None and mark >= occurred_at

    fields = {k: v for k, v in fields.items() if not already(k)}
    if not fields:
        return outbox.Done()
    status_unmapped = False
    status_name = None
    if "status" in fields:
        status_name = mapping.status_out(fields["status"], cfg)
        if status_name is None:
            # Push the rest; dead-letter the row so the operator maps the status and requeues.
            status_unmapped = True
            fields = {k: v for k, v in fields.items() if k != "status"}
            if not fields:
                return outbox.Dead("status_unmapped")
    add = None
    remove: list[str] = []
    if "assignee" in fields:
        from core.models import UserProfile

        previous = (task.metadata or {}).get(KEY_ASSIGNEE)
        remove = [previous] if previous else []
        profile = UserProfile.objects.select_related("user").filter(
            company=task.company, pk=_uuid(fields["assignee"])).first() if fields["assignee"] else None
        email = (getattr(getattr(profile, "user", None), "email", "") or "").strip().lower()
        add = adapter.member_id(list_id, email) if email else None
        if email and add is None:
            # Not a member of the list: nothing to assign there; ClickUp keeps its assignees.
            logger.warning("[integrations] clickup: golden assignee of task %s is no member of list %s",
                           task.pk, list_id)
            fields = {k: v for k, v in fields.items() if k != "assignee"}
            remove = []
    body = mapping.update_body(fields, status_name=status_name, add_assignee=add, remove_assignees=remove)
    if body:
        response = adapter.http("PUT", f"/api/v2/task/{ref.source_id}", headers=adapter._auth(), json=body,
                                expect=(404,))
        if response.status == 404:
            # Deleted in ClickUp: the mirror follows the deletion rule (cancel, stop syncing).
            with outbox.origin(adapter.provider_id), transaction.atomic():
                adapter._deleted(ref.source_id)
            return outbox.Cancel("deleted_at_provider")
        data = _object(response.json(), adapter.provider_id)
        with transaction.atomic():
            # The task first, then its ref: erase_party's order, so the two never deadlock.
            current = Task._base_manager.select_for_update().filter(pk=task.pk).first()
            if tombstoned(event) or (current is not None and _erased_task(current, event)):
                # Erased while this change was in flight: ClickUp now holds the
                # values this worker read before the erasure. Rename it again.
                from core.integrations.erasure import erase_late

                erase_late(adapter, [ProviderRef(object_type=values.OBJECT_TASK, source_id=ref.source_id)],
                           party_id=_erased_party_id(current), reason="push_in_flight")
                return outbox.Done()
            meta = dict(ref.metadata or {})
            seen = mapping.ms(data.get("date_updated"))
            if seen > mapping.ms(meta.get("date_updated")):
                meta["date_updated"] = str(seen)
            if occurred:
                meta["pushed"] = {**pushed, **{k: occurred for k in fields}}
            ExternalRef._base_manager.filter(pk=ref.pk).update(metadata=meta, last_synced_at=timezone.now())
            if "assignee" in fields:
                task_meta = dict(task.metadata or {})
                _set_or_drop(task_meta, KEY_ASSIGNEE, add)
                Task._base_manager.filter(pk=task.pk).update(metadata=task_meta)
    return outbox.Dead("status_unmapped") if status_unmapped else outbox.Done()


# -- helpers ----------------------------------------------------------------------------


def _object(body, provider: str) -> dict:
    if not isinstance(body, dict):
        raise AdapterError(f"{provider}: the answer is not a JSON object — cause: an unexpected response. "
                           f"Fix: check the API version. {values.DOCS}")
    return body


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _erased_task(task, event=None) -> bool:
    """Whether erasure scrubbed ``task`` after ``event`` was written (or at all, with no event).

    Its party is erased, or erasure marked it (``metadata["erased_at"]``: a task
    that only named the person) after the row was queued: the row may carry, or
    have read, what erasure removed.
    """
    from parties.models import Party

    if task.party_id and Party._base_manager.filter(pk=task.party_id, erased_at__isnull=False).exists():
        return True
    marked = (task.metadata or {}).get("erased_at") if isinstance(task.metadata, dict) else None
    if not marked:
        return False
    if event is None or getattr(event, "created_at", None) is None:
        return True
    when = mapping.parse_iso(marked)
    return when is None or event.created_at <= when


def _created_before_erasure(data: dict, task) -> bool:
    """Whether ClickUp made the task before erasure scrubbed golden's (True when unknown).

    A task of an erased person is always treated as made before. A task that
    only named them, created at ClickUp after the scrub, holds the scrubbed
    text: it is a normal create whose ref is still to come.
    """
    if _erased_party_id(task) is not None:
        return True
    marked = mapping.parse_iso((task.metadata or {}).get("erased_at")) if isinstance(task.metadata, dict) else None
    created_ms = mapping.ms(data.get("date_created"))
    if marked is None or not created_ms:
        return True
    return created_ms <= int(marked.timestamp() * 1000)


def _erased_party_id(task):
    """The task's party when it is the erased person (the erase row is about them), else None."""
    from parties.models import Party

    if task is not None and task.party_id and Party._base_manager.filter(
            pk=task.party_id, erased_at__isnull=False).exists():
        return task.party_id
    return None


def _erased_engagement(company_id, number: str) -> bool:
    """Whether engagement ``number`` belongs to an erased party (erasure keeps the deal, tombstoned)."""
    from crm.models import Engagement

    return Engagement._base_manager.filter(company_id=company_id, number=number,
                                           party__erased_at__isnull=False).exists()


def _task_ct():
    from crm.models import Task

    return ContentType.objects.get_for_model(Task)


def _ref(company, cu_id: str):
    return ExternalRef._base_manager.filter(company=company, source=values.PROVIDER, object_type=values.OBJECT_TASK,
                                            source_id=cu_id, content_type=_task_ct()).first()


def _ref_for_task(task):
    return ExternalRef._base_manager.filter(company_id=task.company_id, source=values.PROVIDER,
                                            object_type=values.OBJECT_TASK, content_type=_task_ct(),
                                            object_id=task.pk).first()


def _write_ref(company, task, cu_id: str, url: str, date_updated: str) -> None:
    ExternalRef.objects.update_or_create(
        company=company, source=values.PROVIDER, object_type=values.OBJECT_TASK, source_id=cu_id,
        defaults={"content_type": _task_ct(), "object_id": task.pk, "last_synced_at": timezone.now(),
                  "source_url": url, "metadata": {"date_updated": date_updated} if date_updated else {}},
    )


def _engagement_number(task) -> str:
    if not task.engagement_id:
        return ""
    from crm.models import Engagement

    return Engagement._base_manager.filter(pk=task.engagement_id).values_list("number", flat=True).first() or ""


def _staff(company, email: str):
    """The active staff profile of ``company`` with ``email`` (exactly one), else None (Phase 5 2.3)."""
    if not email:
        return None
    from django.db.models import Q

    from core.models import UserProfile

    hits = list(UserProfile.objects.filter(
        Q(user__is_staff=True) | Q(user__is_superuser=True), company=company, user__is_active=True,
        user__email__iexact=email.strip(),
    )[:2])
    return hits[0] if len(hits) == 1 else None


def _history_email(hook: dict) -> str:
    for item in hook.get("history_items") or []:
        user = item.get("user") if isinstance(item, dict) else None
        if isinstance(user, dict) and user.get("email"):
            return str(user["email"]).strip().lower()
    return ""


def _history_at(hook: dict):
    stamps = [mapping.ms(i.get("date")) for i in hook.get("history_items") or [] if isinstance(i, dict)]
    stamps = [s for s in stamps if s]
    return mapping.ms_to_dt(max(stamps)) if stamps else None


def _set_or_drop(meta: dict, key: str, value) -> None:
    if value in (None, "", []):
        meta.pop(key, None)
    else:
        meta[key] = value
