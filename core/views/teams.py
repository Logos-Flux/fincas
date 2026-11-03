"""``/erp/settings/teams/``: owner teams (docs/crm/phase4-fields.md 2.1, 2.7).

Every staff user reads the page, like the teams API; creating or editing a
team (and choosing its members) needs ``core.manage_teams``. Scoped to the
requester's tenant: another tenant's team is a 404. No delete: Party and
Engagement ``owner_team`` are PROTECT, so a team that is no longer used is
archived (``is_active=False``) and stays on the records stamped with it.

Members are ``UserProfile.team``: one primary team per person. A team move
changes no record (owner teams are stamped when the owner changes, never
recomputed); archived people are not offered as new members.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from core.models import Team, UserProfile
from core.staff import picker_profiles, profile_display

from .mixins import ERPViewMixin

SECTION = "settings_teams"
PERMISSION = "core.manage_teams"


def _company_id(request):
    from parties.erp_views import request_company_id

    return request_company_id(request)


class TeamForm(forms.ModelForm):
    members = forms.ModelMultipleChoiceField(
        queryset=UserProfile.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        help_text="A person has one primary team; ticking someone moves them here. Their existing "
                  "records keep the team they were stamped with.",
    )

    class Meta:
        model = Team
        fields = ["name", "key", "sort_order", "is_active"]
        labels = {"is_active": "Active (offered on new records)", "sort_order": "Sort order"}
        help_texts = {"key": "Letters, digits, _ or -; imports map by it, so it cannot change later."}

    def __init__(self, *args, company_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.company_id = company_id
        current = list(self.instance.members.values_list("pk", flat=True)) if not self.instance._state.adding \
            else []
        self.fields["members"].queryset = picker_profiles(company_id, keep=current)
        self.fields["members"].label_from_instance = profile_display
        self.fields["members"].initial = current
        if not self.instance._state.adding:
            self.fields["key"].disabled = True

    def clean(self):
        cleaned = super().clean()
        if self.instance._state.adding:
            self.instance.company_id = self.company_id
            key = cleaned.get("key")
            if key and Team.objects.filter(company_id=self.company_id, key=key).exists():
                self.add_error("key", "A team with this key already exists.")
        if cleaned.get("members") and cleaned.get("is_active") is False:
            self.add_error("members", "An archived team takes no members; clear them or keep it active.")
        return cleaned

    def save(self, commit=True):
        team = super().save(commit=commit)
        if commit:
            chosen = {p.pk for p in self.cleaned_data.get("members") or ()}
            UserProfile.objects.filter(team=team).exclude(pk__in=chosen).update(team=None)
            # update(): a team move touches no record (2.1), so no save() side effects are wanted.
            UserProfile.objects.filter(pk__in=chosen, company_id=team.company_id).update(team=team)
        return team


class TeamListView(ERPViewMixin, TemplateView):
    template_name = "erp/settings/teams.html"
    current_section = SECTION
    page_title = "Teams"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        members = UserProfile.objects.select_related("user").order_by("display_name", "user__username")
        teams = list(
            Team.objects.for_request(self.request).order_by("sort_order", "name")
            .prefetch_related(Prefetch("members", queryset=members, to_attr="member_list"))
        )
        for team in teams:
            team.member_names = [profile_display(p) for p in team.member_list]
        context["teams"] = teams
        context["can_manage"] = self.request.user.has_perm(PERMISSION)
        context["can_create"] = context["can_manage"] and _company_id(self.request) is not None
        return context


class _TeamFormView(ERPViewMixin, View):
    template_name = "erp/settings/team_form.html"
    current_section = SECTION

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.has_perm(PERMISSION):
            raise PermissionDenied("Editing teams needs the core.manage_teams permission.")
        return super().dispatch(request, *args, **kwargs)

    def _render(self, request, form, title, status=200):
        return render(request, self.template_name, {
            "form": form, "page_title": title, "back_url": reverse("erp-settings-teams"),
            "current_section": self.current_section,
        }, status=status)

    def _save(self, request, form, title):
        if not form.is_valid():
            return self._render(request, form, title, status=400)
        with transaction.atomic():
            team = form.save()
        messages.success(request, f"Saved {team.name}.")
        return redirect(reverse("erp-settings-teams") + f"#team-{team.pk}")


class TeamCreateView(_TeamFormView):
    def _company(self, request):
        company_id = _company_id(request)
        if company_id is None:
            raise PermissionDenied("Your account has no company, so a team cannot be created.")
        return company_id

    def get(self, request):
        return self._render(request, TeamForm(company_id=self._company(request)), "New team")

    def post(self, request):
        return self._save(request, TeamForm(request.POST, company_id=self._company(request)), "New team")


class TeamUpdateView(_TeamFormView):
    def _team(self, request, pk):
        return get_object_or_404(Team.objects.for_request(request), pk=pk)

    def get(self, request, pk):
        team = self._team(request, pk)
        return self._render(request, TeamForm(instance=team, company_id=team.company_id), f"Edit {team.name}")

    def post(self, request, pk):
        team = self._team(request, pk)
        form = TeamForm(request.POST, instance=team, company_id=team.company_id)
        return self._save(request, form, f"Edit {team.name}")
