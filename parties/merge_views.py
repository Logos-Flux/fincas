"""Merge review ``/erp/parties/merge-review/`` (docs/crm/phase2-party.md 6.3).

Staff principals see the queue; acting on it (scan, approve, dismiss, apply,
undo) needs ``parties.merge_party``. Everything is scoped to the requester's
company; a superuser without a profile company picks one with ``?company=``
(or gets the only one).

Rows sort high confidence first, as 6.3 specifies: a wrong merge can be
undone and the high band is mostly volume, so the queue clears it first.
``?order=asc`` flips it. The comparison for a page is built from a fixed
number of queries, whatever the page holds.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import TemplateView

from core.permissions import is_staff_principal
from core.views.mixins import ERPViewMixin

from .dedupe import BLOCK_DIFFERENT_USERS, BLOCK_SELF_PARTY, BLOCK_TWO_ACCOUNT_HOLDERS
from .merge import BLOCK_MESSAGES, MergeError, unmerge_group
from .merge_queue import ENTITY_KIND, approve, bulk_approve, dismiss, is_bulk_approvable, scan

PAGE_SIZE = 25
BANDS = {
    "high": (Decimal("0.950"), None),
    "mid": (Decimal("0.700"), Decimal("0.950")),
    "low": (None, Decimal("0.700")),
}
EVIDENCE_TEXT = {
    "R1": "Same email {value}",
    "R2": "Same phone {value} and family name",
    "R3": "Same phone {value}",
    "R4": "Same name and postal code",
    "R5": "Same organization name “{value}”",
    "R6": "Same web domain {value}",
    "C1": "A lead was converted to this customer",
}
NAME_FIELDS = ("display_name", "given_name", "family_name", "legal_name")
# Phase 4 picks (phase4-fields.md 5.2), beside the name picks.
CLASSIFICATION_FIELDS = ("lifecycle_stage", "contact_type", "owner_team")
PICK_FIELDS = NAME_FIELDS + CLASSIFICATION_FIELDS
MERGE_PERMISSION = "parties.merge_party"


def _company(request):
    user = request.user
    company = getattr(getattr(user, "userprofile", None), "company", None)
    if company is not None:
        return company
    if user.is_superuser:
        from core.models import Company

        wanted = request.GET.get("company") or request.POST.get("company")
        if wanted:
            try:
                return Company.objects.filter(pk=wanted).first()
            except (ValueError, TypeError):
                return None
        if Company.objects.count() == 1:
            return Company.objects.first()
    return None


class MergeReviewView(ERPViewMixin, UserPassesTestMixin, TemplateView):
    template_name = "erp/parties_merge/review.html"
    page_title = "Merge review"
    current_section = "parties"

    def test_func(self):
        return is_staff_principal(self.request.user)

    # --- reads ----------------------------------------------------------------

    def queue(self, company):
        from unify.models import StagedRow

        return StagedRow.objects.filter(batch__company=company, batch__entity_kind=ENTITY_KIND)

    def get_context_data(self, **kwargs):
        from unify.models import ApprovalRow, ApprovalRun, ImportBatch, StagedRow

        from .models import PartyMergeLog

        context = super().get_context_data(**kwargs)
        company = self.company
        context["company"] = company
        context["can_merge"] = self.request.user.has_perm(MERGE_PERMISSION)
        if company is None:
            return context
        queue = self.queue(company)
        counts = dict(queue.values("mapping_state").annotate(n=Count("id")).values_list("mapping_state", "n"))
        S = StagedRow.MappingState
        context["counts"] = {
            "candidates": sum(counts.values()) - counts.get(S.FAILED, 0),
            "approved": counts.get(S.APPROVED, 0),
            "dismissed": counts.get(S.NA, 0),
            "review": counts.get(S.PROPOSED, 0),
            "merged": counts.get(S.APPLIED, 0),
            "failed": counts.get(S.FAILED, 0),
        }
        last = ImportBatch.objects.filter(company=company, entity_kind=ENTITY_KIND).order_by("-created_at").first()
        context["last_scan"] = last.created_at if last else None

        state = self.request.GET.get("state", "queue")
        band = self.request.GET.get("band", "")
        flagged = self.request.GET.get("flagged") == "1"
        order = "asc" if self.request.GET.get("order") == "asc" else "desc"
        rows = queue
        if state == "queue":
            rows = rows.filter(mapping_state__in=(S.PROPOSED, S.APPROVED))
        elif state in S.values:
            rows = rows.filter(mapping_state=state)
        if band in BANDS:
            low, high = BANDS[band]
            if low is not None:
                rows = rows.filter(mapping_confidence__gte=low)
            if high is not None:
                rows = rows.filter(mapping_confidence__lt=high)
        if flagged:
            rows = rows.filter(raw__needs_review=True)
        confidence = "mapping_confidence" if order == "asc" else "-mapping_confidence"
        rows = rows.select_related("batch").order_by(confidence, "-batch__created_at", "row_number")
        page = Paginator(rows, PAGE_SIZE).get_page(self.request.GET.get("page"))
        context.update(
            page=page, state=state, band=band, flagged=flagged, order=order,
            clusters=build_comparisons(company, list(page.object_list), user=self.request.user),
            states=[("queue", "To decide")] + [(v, label) for v, label in S.choices],
            filter_query=self._query(state=state, band=band, flagged="1" if flagged else "", order=order),
        )

        runs = list(
            ApprovalRun.objects.filter(batch__company=company, batch__entity_kind=ENTITY_KIND)
            .annotate(
                total=Count("rows"),
                applied=Count("rows", filter=Q(rows__state=ApprovalRow.State.APPLIED)),
                pending=Count("rows", filter=Q(rows__state=ApprovalRow.State.PENDING)),
                failed=Count("rows", filter=Q(rows__state=ApprovalRow.State.FAILED)),
            ).order_by("-created_at")[:5]
        )
        for run in runs:
            run.percent = round(100 * (run.total - run.pending) / run.total) if run.total else 100
            run.undoable = run.applied > 0
        context["runs"] = runs
        context["last_undoable"] = next((r for r in runs if r.undoable), None)

        logs = list(
            PartyMergeLog.objects.filter(company=company, undone_at__isnull=True)
            .select_related("survivor", "loser").order_by("-created_at")[:60]
        )
        merges: dict = {}
        for log in logs:
            entry = merges.setdefault(log.merge_id, {"merge_id": log.merge_id, "survivor": log.survivor,
                                                     "losers": [], "at": log.created_at, "actor": log.actor})
            entry["losers"].append(log.loser)
        context["recent_merges"] = list(merges.values())[:20]
        return context

    def _query(self, **params) -> str:
        from urllib.parse import urlencode

        if self.request.GET.get("company"):
            params["company"] = self.request.GET["company"]
        return urlencode({k: v for k, v in params.items() if v})

    def dispatch(self, request, *args, **kwargs):
        self.company = None
        if request.user.is_authenticated:
            self.company = _company(request)
        return super().dispatch(request, *args, **kwargs)

    # --- writes ---------------------------------------------------------------

    def post(self, request, *args, **kwargs):
        if not request.user.has_perm(MERGE_PERMISSION):
            raise PermissionDenied("Merging parties needs the parties.merge_party permission.")
        company = self.company
        if company is None:
            raise Http404("No company resolved for this merge queue.")
        action = request.POST.get("action", "")
        actor = request.user.get_username()
        anchor = ""
        try:
            anchor = self._act(company, action, request, actor) or ""
        except MergeError as exc:
            messages.error(request, str(exc))
        url = reverse("erp-party-merge-review")
        query = request.POST.get("return_query", "")
        return redirect(f"{url}{'?' + query if query else ''}{anchor}")

    def _row(self, company, pk):
        try:
            return self.queue(company).select_related("batch").get(pk=uuid.UUID(str(pk)))
        except (ValueError, TypeError) as exc:
            raise Http404("No such candidate.") from exc
        except self.queue(company).model.DoesNotExist as exc:
            raise Http404("No such candidate.") from exc

    def _act(self, company, action, request, actor):
        from unify.apply import apply_chunk, cancel_run, start_apply_run, undo_run
        from unify.models import ApprovalRow, ApprovalRun, StagedRow

        if action == "scan":
            out = scan(company, actor=actor)
            messages.success(
                request, f"Scan: {out['clusters']} clusters, {out['new']} new, "
                         f"{out['suppressed']} already known, {out['stale']} stale.")
        elif action == "approve":
            row = self._row(company, request.POST.get("row"))
            picks = {}
            for field in PICK_FIELDS:
                if field == "owner_team" and not request.user.has_perm("crm.reassign_engagement"):
                    continue  # a direct team edit (2.7)
                if request.POST.get(f"pick_{field}"):
                    picks[field] = request.POST[f"pick_{field}"]
            # Sidecar picks name a defined key of this tenant (5.2): "keep this party's value".
            from core import sidecar
            from core.sidecar_forms import can_view_pii, field_name

            show_pii = can_view_pii(request.user)
            for definition in sidecar.definitions("party", str(company.pk)):
                if definition.pii and not show_pii:
                    continue  # never offered; a crafted pick is ignored
                value = request.POST.get("pick_" + field_name(definition.key))
                if value and not definition.read_only:
                    picks[definition.key] = value
            approve(row, actor=actor, survivor=request.POST.get("survivor"), field_picks=picks)
            messages.success(request, "Merge approved. Apply approved merges to carry it out.")
            return f"#cluster-{row.pk}"
        elif action == "dismiss":
            row = self._row(company, request.POST.get("row"))
            dismiss(row, actor=actor)
            messages.success(request, "Marked not a duplicate. This cluster will not be proposed again.")
        elif action == "bulk-approve":
            ids = []
            for pk in request.POST.getlist("rows"):
                try:
                    ids.append(uuid.UUID(pk))
                except ValueError:
                    continue
            rows = list(self.queue(company).filter(pk__in=ids, mapping_state=StagedRow.MappingState.PROPOSED))
            out = bulk_approve(rows, actor=f"{actor} (bulk ≥0.95)")
            messages.success(request, f"Approved {out['approved']}; skipped {out['skipped']} that need "
                                      "individual review.")
        elif action == "apply":
            opened = 0
            batches = self.queue(company).filter(mapping_state=StagedRow.MappingState.APPROVED) \
                .values_list("batch_id", flat=True).distinct()
            for batch_id in batches:
                active = ApprovalRun.objects.filter(
                    batch_id=batch_id, status__in=(ApprovalRun.Status.OPEN, ApprovalRun.Status.APPLYING),
                    rows__state=ApprovalRow.State.PENDING,
                ).exists()
                if not active:
                    from unify.models import ImportBatch

                    start_apply_run(ImportBatch.objects.get(pk=batch_id), actor=actor)
                    opened += 1
            run = self._next_run(company)
            if run is None:
                messages.info(request, "Nothing approved to apply.")
                return ""
            out = apply_chunk(str(run.pk), [])
            self._chunk_message(request, out, opened)
        elif action == "apply-chunk":
            run = self._run(company, request.POST.get("run"))
            self._chunk_message(request, apply_chunk(str(run.pk), []), 0)
        elif action == "cancel-run":
            run = self._run(company, request.POST.get("run"))
            cancel_run(str(run.pk))
            messages.success(request, "Run canceled. Merges already applied stay applied.")
        elif action == "undo-run":
            run = self._run(company, request.POST.get("run"))
            try:
                out = undo_run(str(run.pk), request.POST.get("undo_token", ""))
            except ValueError as exc:
                raise MergeError(str(exc)) from exc
            messages.success(request, f"Undone: {out['undone']} parties restored.")
            for refused in out["refused"]:
                messages.error(request, f"Candidate {refused['row']} not undone: {refused['error']}")
        elif action == "undo-merge":
            from .models import PartyMergeLog

            try:
                merge_id = uuid.UUID(request.POST.get("merge_id", ""))
            except ValueError as exc:
                raise Http404("No such merge.") from exc
            if not PartyMergeLog.objects.filter(company=company, merge_id=merge_id).exists():
                raise Http404("No such merge.")
            logs = unmerge_group(merge_id, actor=actor)
            self._reopen_rows(logs)
            messages.success(request, f"Merge undone: {len(logs)} parties restored.")
        else:
            messages.error(request, f"Unknown action: {action or '(none)'}.")
        return ""

    def _reopen_rows(self, logs):
        """A per-merge undo sends its queue row back for a new decision, like a run undo."""
        from unify.appliers import get_applier
        from unify.apply import reopen
        from unify.models import ApprovalRow

        applier = get_applier(ENTITY_KIND)
        row_ids = {log.approval_row_id for log in logs if log.approval_row_id}
        for approval_row in ApprovalRow.objects.filter(pk__in=row_ids).select_related("staged_row"):
            approval_row.state = ApprovalRow.State.SKIPPED
            approval_row.save(update_fields=["state", "updated_at"])
            reopen(approval_row.staged_row, applier)

    def _run(self, company, pk):
        from unify.models import ApprovalRun

        try:
            return ApprovalRun.objects.get(pk=uuid.UUID(str(pk)), batch__company=company,
                                           batch__entity_kind=ENTITY_KIND)
        except (ValueError, TypeError, ApprovalRun.DoesNotExist) as exc:
            raise Http404("No such run.") from exc

    def _next_run(self, company):
        from unify.models import ApprovalRow, ApprovalRun

        return (
            ApprovalRun.objects.filter(
                batch__company=company, batch__entity_kind=ENTITY_KIND,
                status__in=(ApprovalRun.Status.OPEN, ApprovalRun.Status.APPLYING),
                rows__state=ApprovalRow.State.PENDING,
            ).distinct().order_by("created_at").first()
        )

    def _chunk_message(self, request, out, opened):
        from unify.models import ApprovalRow, ApprovalRun

        run_id = out.get("run")
        if run_id and not ApprovalRow.objects.filter(run_id=run_id, state=ApprovalRow.State.PENDING).exists():
            ApprovalRun.objects.filter(pk=run_id, status=ApprovalRun.Status.APPLYING).update(
                status=ApprovalRun.Status.DONE)
        applied = sum(1 for r in out["results"] if r["state"] == "applied")
        failed = [r for r in out["results"] if r["state"] == "failed"]
        prefix = f"Opened {opened} run(s). " if opened else ""
        messages.success(request, f"{prefix}Chunk: {applied} merged, {len(failed)} failed.")
        for r in failed[:5]:
            messages.error(request, f"Candidate {r['row']}: {r['error']}")


# --- comparison --------------------------------------------------------------------


def build_comparisons(company, rows, user=None) -> list[dict]:
    """Side-by-side data for one page of clusters, in a fixed number of queries.

    Phase 4 rows (phase4-fields.md 5.2): lifecycle stage, contact type, owner,
    owner team and the tenant's party definitions. A ``pii`` definition's
    values show only with ``parties.export_pii`` and are then pickable."""
    from django.contrib.contenttypes.models import ContentType

    from core import sidecar
    from core.models import ExternalRef
    from core.sidecar_forms import can_view_pii, display_value, field_name
    from core.staff import profile_display
    from core.teams import team_display
    from crm.models import Lead
    from customers.models import Customer

    from .display import contact_type_label, lifecycle_label
    from .models import Party, PartyAddress, PartyEmail, PartyPhone, PartyRole

    ids = set()
    for row in rows:
        for pk in (row.raw or {}).get("party_ids", []):
            try:
                ids.add(uuid.UUID(pk))
            except ValueError:
                continue
    parties = {p.pk: p for p in Party.objects.filter(company=company, pk__in=ids)
               .select_related("owner__user", "owner_team")}
    definitions = list(sidecar.definitions("party", str(company.pk)))
    show_pii = can_view_pii(user)
    emails, phones, addresses, roles, accounts, leads, refs = (defaultdict(list) for _ in range(7))
    for e in PartyEmail.objects.filter(party_id__in=parties).order_by("-is_primary", "email"):
        emails[e.party_id].append(e)
    for p in PartyPhone.objects.filter(party_id__in=parties).order_by("-is_primary", "number_raw"):
        phones[p.party_id].append(p)
    for a in PartyAddress.objects.filter(party_id__in=parties).order_by("-is_primary", "address_line_1"):
        addresses[a.party_id].append(a)
    for r in PartyRole.objects.filter(party_id__in=parties).order_by("role", "valid_from"):
        roles[r.party_id].append(r)
    holders = set()
    for c in (Customer.objects.filter(Q(party_id__in=parties) | Q(account_holder_id__in=parties))
              .annotate(order_count=Count("orders"))):
        for pid in {c.party_id, c.account_holder_id} - {None}:
            if pid in parties:
                accounts[pid].append(c)
        if c.account_holder_id in parties:
            holders.add(c.account_holder_id)
    for lead in Lead.objects.filter(party_id__in=parties).order_by("lead_number"):
        leads[lead.party_id].append(lead)
    party_ct = ContentType.objects.get_for_model(Party)
    for ref in ExternalRef.objects.filter(content_type=party_ct, object_id__in=parties):
        refs[ref.object_id].append(ref)

    out = []
    for row in rows:
        raw = row.raw or {}
        members = [parties[uuid.UUID(pk)] for pk in raw.get("party_ids", []) if uuid.UUID(pk) in parties]
        live_blocks = list(raw.get("blocked") or [])
        if len({p.pk for p in members if p.pk in holders}) > 1:
            live_blocks.append(BLOCK_TWO_ACCOUNT_HOLDERS)
        if len({p.user_id for p in members if p.user_id}) > 1:
            live_blocks.append(BLOCK_DIFFERENT_USERS)
        if any(p.is_self for p in members):
            live_blocks.append(BLOCK_SELF_PARTY)
        live_blocks = list(dict.fromkeys(live_blocks))
        stale = len(members) != len(raw.get("party_ids", [])) or any(p.merged_into_id for p in members)
        survivor = (row.mapped_values or {}).get("survivor") or (row.proposed_mapping or {}).get("survivor")

        def line(label, values, members=members):
            values = list(values)
            return {"label": label, "values": values, "differs": len({v for v in values if v}) > 1}

        table = [
            line("Name", (p.display_name for p in members)),
            line("Kind", (p.get_kind_display() for p in members)),
            line("Emails", (", ".join(e.email for e in emails[p.pk]) for p in members)),
            line("Phones", (", ".join(ph.number_e164 or ph.number_raw for ph in phones[p.pk]) for p in members)),
            line("Addresses", ("; ".join(f"{a.address_line_1}, {a.city} {a.postal_code} {a.country}"
                                         for a in addresses[p.pk]) for p in members)),
            line("Roles", (", ".join(_role_text(r) for r in roles[p.pk]) for p in members)),
            line("Customer accounts", ("; ".join(f"{c} ({c.order_count} orders)" for c in accounts[p.pk])
                                       for p in members)),
            line("Leads", (", ".join(lead.lead_number for lead in leads[p.pk]) for p in members)),
            line("Login", ("yes" if p.user_id else "" for p in members)),
            line("Origin", (p.get_origin_display() for p in members)),
            line("Created", (p.created_at.date().isoformat() for p in members)),
            line("External ids", (", ".join(str(r) for r in refs[p.pk]) for p in members)),
            line("Lifecycle stage", (lifecycle_label(p.lifecycle_stage, company.pk) for p in members)),
            line("Contact type", (contact_type_label(p.contact_type, company.pk) for p in members)),
            line("Owner", (profile_display(p.owner, "") for p in members)),
            line("Owner team", (team_display(p.owner_team) for p in members)),
        ]
        shown = {}  # sidecar key -> [(party id, display)] for picks
        for definition in definitions:
            hidden = definition.pii and not show_pii
            texts = []
            for p in members:
                value = sidecar.read(p, definition.key)
                texts.append("Hidden: personal data" if hidden and value is not None
                             else display_value(definition, value))
            table.append(line(definition.label, texts))
            if not hidden and not definition.read_only:
                shown[definition] = [(str(p.pk), t) for p, t in zip(members, texts, strict=True) if t]
        picks = []
        for field in NAME_FIELDS:
            values = [(str(p.pk), getattr(p, field)) for p in members if getattr(p, field)]
            if len({v for _, v in values}) > 1:
                picks.append({"field": field, "name": f"pick_{field}", "label": field.replace("_", " "),
                              "options": values})
        classification = [
            ("lifecycle_stage", lambda p: lifecycle_label(p.lifecycle_stage, company.pk)),
            ("contact_type", lambda p: contact_type_label(p.contact_type, company.pk)),
        ]
        if user is not None and user.has_perm("crm.reassign_engagement"):
            classification.append(("owner_team", lambda p: team_display(p.owner_team)))
        for field, display in classification:
            values = [(str(p.pk), display(p)) for p in members if display(p)]
            if len({v for _, v in values}) > 1:
                picks.append({"field": field, "name": f"pick_{field}", "label": field.replace("_", " "),
                              "options": values})
        for definition, values in shown.items():
            if len({v for _, v in values}) > 1:
                picks.append({"field": field_name(definition.key), "name": "pick_" + field_name(definition.key),
                              "label": definition.label, "options": values})
        out.append({
            "row": row,
            "members": members,
            "survivor": survivor,
            "table": table,
            "picks": picks,
            "evidence": [EVIDENCE_TEXT.get(e.get("rule"), e.get("rule", "")).format(value=e.get("value", ""))
                         for e in raw.get("evidence", [])],
            "flags": raw.get("flags") or [],
            "blocked": [BLOCK_MESSAGES.get(b, b) for b in live_blocks],
            "stale": stale,
            "bulk": is_bulk_approvable(row) and not live_blocks and not stale,
            "next_id": None,
        })
    for current, following in zip(out, out[1:], strict=False):
        current["next_id"] = following["row"].pk
    return out


def _role_text(role) -> str:
    dates = role.valid_from.isoformat()
    dates += f"–{role.valid_to.isoformat()}" if role.valid_to else "–"
    return f"{role.role} ({dates})"
