"""Back-office forms for the CRM screens (docs/crm/phase3-engagement.md 6).

Forms only validate input. Every write goes through the services
(``crm.engagements``, ``crm.tasks``, ``communications.ingest.record``), which
own the history rows and the next-action recompute; ``crm/erp_views.py``
calls them with the cleaned data. Each form is bound to one tenant: owner,
assignee, stage and party choices never leave it.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from communications.kinds import communication_kinds
from core.models import UserProfile
from core.sidecar_forms import DefinitionFieldsMixin
from core.staff import picker_profiles, profile_display
from core.teams import TeamChoiceField, team_choices
from parties.constants import PERSON
from parties.models import Party
from parties.normalize import normalize_email
from parties.widgets import PartySearchInput

from .models import Engagement, EngagementContact, Lead, Pipeline, Stage, Task
from .registries import ENGAGEMENT_SOURCES, LOSS_REASONS, TASK_KINDS, UNKNOWN_LOSS_REASON

DATE_WIDGET = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
TIME_WIDGET = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
DATETIME_WIDGET = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")

SNOOZE_CHOICES = (("tomorrow", "Tomorrow"), ("next_week", "Next week"), ("date", "Pick a date"))


def tenant_profiles(company_id, *, keep=()):
    """Staff profiles an owner / assignee picker offers: the tenant's active
    people, plus ``keep`` (the current value) (phase4-fields.md 2.5)."""
    return picker_profiles(company_id, keep=keep)


def profile_label(profile) -> str:
    """Display name, else login; "(archived)" for an archived person."""
    return profile_display(profile)


class ProfileChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return profile_label(obj)


def resolve_party(company_id, text):
    """A live party of the tenant from its id or an exact email it holds.

    Same rule as the party relationship form (Phase 2 P9): an email held by
    several parties is refused rather than guessed.
    """
    text = (text or "").strip()
    if not text:
        return None
    live = Party.objects.filter(company_id=company_id, merged_into__isnull=True, erased_at__isnull=True)
    try:
        pk = uuid.UUID(text)
    except ValueError:
        pk = None
    if pk is not None:
        party = live.filter(pk=pk).first()
        if party is None:
            raise ValidationError("No party with that id.")
        return party
    try:
        email = normalize_email(text)
    except ValidationError:
        raise ValidationError("Enter a party id or an email address.") from None
    matches = list(live.filter(emails__email_normalized=email).distinct()[:3])
    if not matches:
        raise ValidationError("No party holds that email address.")
    if len(matches) > 1:
        raise ValidationError("Several parties hold that email address; enter the id of the one you mean.")
    return matches[0]


def party_field(label, help_text, *, required=False):
    """A party picker: a party id or an exact email without JavaScript, a
    search over the tenant's parties with it (``parties.widgets``). The
    form scopes it with ``scope_party_field``; ``clean_*`` resolves it with
    ``resolve_party``."""
    return forms.CharField(label=label, max_length=254, required=required, help_text=help_text,
                           widget=PartySearchInput())


def scope_party_field(form, name, company_id, *, kind=""):
    widget = form.fields[name].widget
    widget.company_id = company_id
    widget.kind = kind


def snooze_date(choice: str, today: datetime.date, picked=None) -> datetime.date | None:
    """Tomorrow, the Monday after this week, or the picked date."""
    if choice == "tomorrow":
        return today + datetime.timedelta(days=1)
    if choice == "next_week":
        return today + datetime.timedelta(days=7 - today.weekday())
    return picked


# --- engagements -------------------------------------------------------------------


class EngagementForm(DefinitionFieldsMixin, forms.Form):
    """Create or edit an engagement's facts. Stage and outcome are not here:
    they move through the stage stepper and the Win / Lose / Reopen actions.

    ``owner_team`` is offered only to staff with ``crm.reassign_engagement``
    (phase4-fields.md 2.1); otherwise ``save()`` stamps it from the owner.
    The deployment's engagement definitions follow as fieldsets (5.2)."""

    definition_object = "engagement"

    title = forms.CharField(max_length=255, required=False,
                            help_text="Blank: the counterparty's name and the source.")
    party = party_field(
        "Counterparty",
        "Type an email address or a name and pick the party (or enter an exact email address "
        "or the party's id). Blank leaves the engagement in the No counterparty queue.",
    )
    pipeline = forms.ModelChoiceField(queryset=Pipeline.objects.none(), required=False,
                                      empty_label="Default pipeline")
    owner = ProfileChoiceField(queryset=UserProfile.objects.none(), required=False,
                               empty_label="Unassigned")
    expected_value = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"),
                                        required=False, help_text="Blank means unknown, never zero.")
    currency = forms.CharField(max_length=3, initial="USD")
    probability = forms.IntegerField(min_value=0, max_value=100, required=False,
                                     help_text="Percent. Blank uses the stage's default.")
    expected_close_date = forms.DateField(required=False, widget=DATE_WIDGET)
    source = forms.ChoiceField(required=False)
    tags_text = forms.CharField(label="Tags", required=False, help_text="Comma separated.")
    owner_team = TeamChoiceField(label="Owner team", queryset=None, required=False,
                                 empty_label="From the owner",
                                 help_text="Blank: taken from the owner's team when the owner changes.")

    def __init__(self, *args, company_id, instance=None, user=None, **kwargs):
        self.company_id = company_id
        self.instance = instance
        if instance is not None and "initial" not in kwargs:
            kwargs["initial"] = {
                "title": instance.title, "party": str(instance.party_id or ""),
                "owner": instance.owner_id, "owner_team": instance.owner_team_id,
                "expected_value": instance.expected_value,
                "currency": instance.currency, "probability": instance.probability,
                "expected_close_date": instance.expected_close_date, "source": instance.source,
                "tags_text": ", ".join(str(t) for t in instance.tags or []),
            }
        super().__init__(*args, **kwargs)
        scope_party_field(self, "party", company_id)
        current_owner = instance.owner_id if instance is not None else None
        self.fields["owner"].queryset = tenant_profiles(company_id, keep=[current_owner])
        if user is not None and user.has_perm("crm.reassign_engagement"):
            self.fields["owner_team"].queryset = team_choices(
                company_id, keep=instance.owner_team_id if instance is not None else None)
        else:
            del self.fields["owner_team"]
        self.fields["source"].choices = [("", "Unknown")] + [
            (key, entry.label) for key, entry in ENGAGEMENT_SOURCES.items()
        ]
        if instance is None:
            self.fields["pipeline"].queryset = Pipeline.objects.filter(
                company_id=company_id, is_active=True).order_by("sort_order", "name")
        else:
            del self.fields["pipeline"]
        from .models import Engagement

        self.add_definition_fields(instance if instance is not None else Engagement(company_id=company_id),
                                   user=user, company_id=company_id)

    @property
    def fact_fields(self):
        return [self[name] for name in self.fields if name not in self.definition_fields]

    def clean(self):
        cleaned = super().clean()
        self.clean_definition_fields()
        return cleaned

    def clean_party(self):
        return resolve_party(self.company_id, self.cleaned_data.get("party"))

    def clean_currency(self):
        value = (self.cleaned_data.get("currency") or "").strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValidationError("A three-letter currency code (USD, EUR, …).")
        return value

    def clean_tags_text(self):
        raw = self.cleaned_data.get("tags_text") or ""
        return [t.strip() for t in raw.split(",") if t.strip()]


class MoveForm(forms.Form):
    stage = forms.ModelChoiceField(queryset=Stage.objects.none())
    note = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, company_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stage"].queryset = Stage.objects.filter(
            company_id=company_id, is_active=True, pipeline__is_active=True,
        ).select_related("pipeline")


class LoseForm(forms.Form):
    loss_reason = forms.ChoiceField(label="Reason")
    loss_competitor = forms.CharField(label="Competitor", max_length=255, required=False)
    loss_notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, reason_labels=None, **kwargs):
        super().__init__(*args, **kwargs)
        labels = reason_labels or {}
        # ``unknown`` is for backfill and import only (spec 1.1).
        self.fields["loss_reason"].choices = [("", "Pick a reason")] + [
            (key, labels.get(key, entry.label))
            for key, entry in LOSS_REASONS.items() if key != UNKNOWN_LOSS_REASON
        ]


class ReopenForm(forms.Form):
    stage = forms.ModelChoiceField(queryset=Stage.objects.none(), required=False,
                                   empty_label="Where it was")

    def __init__(self, *args, engagement, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stage"].queryset = Stage.objects.filter(
            pipeline_id=engagement.pipeline_id, is_active=True).order_by("sort_order")


class BulkForm(forms.Form):
    """The exception queue's bulk actions (spec 6.1)."""

    ACTIONS = (("assign", "Assign owner"), ("follow_up", "Add a follow-up task (due tomorrow)"),
               ("lose", "Close as lost"))

    ids = forms.MultipleChoiceField(choices=())
    action = forms.ChoiceField(choices=ACTIONS)
    owner = ProfileChoiceField(queryset=UserProfile.objects.none(), required=False)
    loss_reason = forms.ChoiceField(required=False)

    def __init__(self, *args, company_id, id_choices, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ids"].choices = [(str(i), str(i)) for i in id_choices]
        self.fields["owner"].queryset = tenant_profiles(company_id) if company_id else UserProfile.objects.none()
        self.fields["loss_reason"].choices = [("", "")] + [
            (key, entry.label) for key, entry in LOSS_REASONS.items() if key != UNKNOWN_LOSS_REASON
        ]

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get("action")
        if action == "assign" and not cleaned.get("owner"):
            self.add_error("owner", "Pick the owner.")
        if action == "lose" and not cleaned.get("loss_reason"):
            self.add_error("loss_reason", "Pick a reason.")
        return cleaned


# --- tasks ------------------------------------------------------------------------


class TaskForm(DefinitionFieldsMixin, forms.Form):
    """A new task, usually the engagement's next action ("what, due, who").

    The deployment's task definitions follow the Phase 3 fields as
    fieldsets (phase4-fields.md 5.2: tasks have no detail page)."""

    definition_object = "task"

    title = forms.CharField(label="What", max_length=255)
    kind = forms.ChoiceField(initial="follow_up")
    due_date = forms.DateField(label="Due", required=False, widget=DATE_WIDGET)
    due_time = forms.TimeField(label="At", required=False, widget=TIME_WIDGET,
                               help_text="Blank: due by the end of that day.")
    assignee = ProfileChoiceField(label="Who", queryset=UserProfile.objects.none(), required=False,
                                  empty_label="Unassigned")
    priority = forms.ChoiceField(choices=Task.Priority.choices, initial=Task.Priority.NORMAL)

    def __init__(self, *args, company_id, kind_labels=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        labels = kind_labels or {}
        self.fields["kind"].choices = [(k, labels.get(k, e.label)) for k, e in TASK_KINDS.items()]
        self.fields["assignee"].queryset = tenant_profiles(company_id)
        self.add_definition_fields(Task(company_id=company_id), user=user, company_id=company_id)

    @property
    def fact_fields(self):
        return [self[name] for name in self.fields if name not in self.definition_fields]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("due_time") and not cleaned.get("due_date"):
            self.add_error("due_date", "Pick the day as well as the time.")
        self.clean_definition_fields()
        return cleaned

    def due(self):
        """A ``date`` (all-day) or a naive ``datetime`` (wall time in the task's zone)."""
        day, at = self.cleaned_data.get("due_date"), self.cleaned_data.get("due_time")
        if day is None:
            return None
        return datetime.datetime.combine(day, at) if at else day


class EngagementChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.number} · {obj.title}" if obj.title else obj.number


#: Open engagements the New task screen lists; a mature tenant has thousands.
ENGAGEMENT_PICKER_LIMIT = 200


class NewTaskForm(TaskForm):
    """A task from the task list: ``TaskForm`` plus where it belongs, both
    optional (a company-only task is valid). The engagement picker offers
    the tenant's newest open engagements (``ENGAGEMENT_PICKER_LIMIT``, plus
    ``keep``, a prefilled one)."""

    engagement = EngagementChoiceField(queryset=Engagement.objects.none(), required=False, empty_label="None")
    party = party_field("Counterparty", "Optional. Type an email address or a name and pick the party. "
                                        "Blank with an engagement picked: the engagement's counterparty.")

    def __init__(self, *args, company_id, engagement_label="Engagement", keep=None, **kwargs):
        super().__init__(*args, company_id=company_id, **kwargs)
        self.company_id = company_id
        field = self.fields["engagement"]
        field.label = engagement_label
        recent = Engagement.objects.filter(company_id=company_id, status=Engagement.Status.OPEN).order_by(
            "-created_at", "pk").values_list("pk", flat=True)[:ENGAGEMENT_PICKER_LIMIT]
        field.queryset = Engagement.objects.filter(company_id=company_id).filter(
            Q(pk__in=list(recent)) | Q(pk=keep)).order_by("-created_at", "pk")
        field.help_text = f"The {ENGAGEMENT_PICKER_LIMIT} newest open ones."
        scope_party_field(self, "party", company_id)

    def clean_party(self):
        return resolve_party(self.company_id, self.cleaned_data.get("party"))


class RescheduleForm(forms.Form):
    when = forms.ChoiceField(choices=SNOOZE_CHOICES, initial="tomorrow")
    date = forms.DateField(required=False, widget=DATE_WIDGET)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("when") == "date" and not cleaned.get("date"):
            self.add_error("date", "Pick a date.")
        return cleaned


class ReassignForm(forms.Form):
    assignee = ProfileChoiceField(queryset=UserProfile.objects.none(), required=False,
                                  empty_label="Unassigned")

    def __init__(self, *args, company_id, keep=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = tenant_profiles(company_id, keep=[keep])


# --- log a communication -----------------------------------------------------------


class LogCommunicationForm(forms.Form):
    """Log a call, meeting, email or note on the thread (spec 6.5), plus
    "and set the next action" in the same form."""

    kind = forms.ChoiceField()
    direction = forms.ChoiceField(choices=(("outbound", "Outbound (we contacted them)"),
                                           ("inbound", "Inbound (they contacted us)"),
                                           ("internal", "Internal")))
    occurred_at = forms.DateTimeField(label="When", widget=DATETIME_WIDGET,
                                      input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"])
    duration_minutes = forms.IntegerField(label="Duration (minutes)", min_value=0, max_value=24 * 60,
                                          required=False, help_text="Calls and meetings only.")
    subject = forms.CharField(max_length=255, required=False)
    body_text = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 4}))
    participants = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple,
                                             help_text="Contact points of the counterparty and contacts.")
    # A task like any other: it becomes the next action only when it is the
    # open task due first, so the labels say "task", not "next action".
    next_title = forms.CharField(label="Task", max_length=255, required=False,
                                 help_text="Optional: adds a task in the same step.")
    next_due = forms.DateField(label="Task due", required=False, widget=DATE_WIDGET)
    next_assignee = ProfileChoiceField(label="Task for", queryset=UserProfile.objects.none(),
                                       required=False, empty_label="Unassigned")

    def __init__(self, *args, company_id, contact_points=(), zone=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.zone = zone
        self.contact_points = {value: (party, address, label) for value, party, address, label in contact_points}
        self.fields["participants"].choices = [(value, label) for value, _p, _a, label in contact_points]
        self.fields["kind"].choices = [
            (key, entry.label) for key, entry in communication_kinds().items()
            if entry.raw_schema_version is None  # adapter kinds are imported, never typed in
        ]
        self.fields["next_assignee"].queryset = tenant_profiles(company_id)

    def clean_occurred_at(self):
        value = self.cleaned_data["occurred_at"]
        if self.zone is not None:
            value = value.replace(tzinfo=None) if timezone.is_aware(value) else value
            value = value.replace(tzinfo=self.zone)
        if value > timezone.now() + datetime.timedelta(minutes=5):
            raise ValidationError("A logged communication happened already; schedule a task instead.")
        return value

    def clean(self):
        cleaned = super().clean()
        kind = communication_kinds().get(cleaned.get("kind") or "")
        direction = cleaned.get("direction")
        if kind is not None and direction and direction not in kind.directions:
            allowed = ", ".join(kind.directions)
            self.add_error("direction", f"A {kind.label.lower()} is {allowed}.")
        if kind is not None and not kind.has_duration and cleaned.get("duration_minutes"):
            self.add_error("duration_minutes", f"A {kind.label.lower()} has no duration.")
        if not (cleaned.get("subject") or cleaned.get("body_text")):
            self.add_error("subject", "Give a subject or notes.")
        if cleaned.get("next_due") and not cleaned.get("next_title"):
            self.add_error("next_title", "Say what the next action is.")
        return cleaned

    def participant_specs(self):
        role = {"outbound": "to", "inbound": "from"}.get(self.cleaned_data["direction"], "attendee")
        if self.cleaned_data["kind"] == "call":
            role = "callee" if role == "to" else ("caller" if role == "from" else role)
        specs = []
        for value in self.cleaned_data.get("participants") or []:
            party, address, _label = self.contact_points[value]
            specs.append({"role": role, "address": address, "party": party,
                          "display_name": party.display_name})
        return specs


# --- pipelines ---------------------------------------------------------------------


class PipelineForm(forms.ModelForm):
    class Meta:
        model = Pipeline
        fields = ["key", "name", "is_default", "is_active", "sort_order", "stale_after_days"]
        labels = {"is_default": "Default pipeline (new engagements land here)",
                  "is_active": "Active (takes new engagements)",
                  "stale_after_days": "Stale after (days)"}
        help_texts = {"key": "Immutable once saved; imports map by it."}

    def __init__(self, *args, company_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.company_id = company_id
        if not self.instance._state.adding:
            self.fields["key"].disabled = True

    def clean_key(self):
        key = self.cleaned_data["key"]
        if self.instance._state.adding:
            if Pipeline.objects.filter(company_id=self.company_id, key=key).exists():
                raise ValidationError("A pipeline with this key exists.")
            return key
        return self.instance.key

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_default") and not self.instance.is_default and (
            self.instance._state.adding or not self.instance.stages.filter(is_active=True).exists()
        ):
            # New engagements land in the default pipeline at its first active
            # stage; a pipeline without one would break every create.
            self.add_error("is_default", "Add an active stage first, then make it the default.")
        if self.instance.is_default and cleaned.get("is_default") is False:
            self.add_error("is_default", "Make another pipeline the default instead.")
        if cleaned.get("is_default") and cleaned.get("is_active") is False:
            self.add_error("is_active", "The default pipeline must be active.")
        return cleaned

    def _post_clean(self):
        self.instance.company_id = self.company_id
        super()._post_clean()


class StageForm(forms.ModelForm):
    class Meta:
        model = Stage
        fields = ["key", "name", "default_probability", "stale_after_days", "outcome_hint", "is_active"]
        labels = {"default_probability": "Default probability (%)",
                  "stale_after_days": "Stale after (days)",
                  "outcome_hint": "Entering it proposes closing as",
                  "is_active": "Active (archived stages stay on old engagements)"}
        help_texts = {"key": "Immutable once saved; imports map by it.",
                      "stale_after_days": "Blank uses the pipeline's."}

    def __init__(self, *args, pipeline, **kwargs):
        super().__init__(*args, **kwargs)
        self.pipeline = pipeline
        if not self.instance._state.adding:
            self.fields["key"].disabled = True

    def clean_key(self):
        key = self.cleaned_data["key"]
        if self.instance._state.adding:
            if Stage.objects.filter(pipeline=self.pipeline, key=key).exists():
                raise ValidationError("This pipeline has a stage with this key.")
            return key
        return self.instance.key

    def clean_default_probability(self):
        value = self.cleaned_data.get("default_probability")
        if value is not None and value > 100:
            raise ValidationError("At most 100.")
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_active") is False and not self.instance._state.adding:
            others = self.pipeline.stages.filter(is_active=True).exclude(pk=self.instance.pk)
            if not others.exists():
                self.add_error("is_active", "A pipeline needs at least one active stage.")
        return cleaned

    def _post_clean(self):
        self.instance.pipeline = self.pipeline
        self.instance.company_id = self.pipeline.company_id
        if self.instance._state.adding and self.instance.sort_order is None:
            last = self.pipeline.stages.order_by("-sort_order").values_list("sort_order", flat=True).first()
            self.instance.sort_order = (last or 0) + 10
        super()._post_clean()


class ContactForm(forms.Form):
    """Add a person to the deal (``EngagementContact``)."""

    party = party_field("Person", "Type an email address or a name and pick the person (or enter an "
                                  "exact email address or their party id).", required=True)
    role = forms.ChoiceField(choices=EngagementContact._meta.get_field("role").choices)
    is_primary = forms.BooleanField(label="Primary contact", required=False)

    def __init__(self, *args, engagement, **kwargs):
        super().__init__(*args, **kwargs)
        self.engagement = engagement
        scope_party_field(self, "party", engagement.company_id, kind=PERSON)

    def clean_party(self):
        party = resolve_party(self.engagement.company_id, self.cleaned_data.get("party"))
        if party is None:
            raise ValidationError("Pick the person.")
        if party.kind != Party.Kind.PERSON:
            raise ValidationError("Contacts are people; the organization is the counterparty.")
        if party.pk == self.engagement.party_id:
            raise ValidationError("The counterparty is already on the deal.")
        if self.engagement.contacts.filter(party=party).exists():
            raise ValidationError("This person is already a contact.")
        return party


# --- leads -------------------------------------------------------------------------


class LeadForm(forms.ModelForm):
    """A lead typed in by staff (a call, an email, a trade-show card).

    Saving it runs intake like any other lead (``Lead.save()`` ->
    ``crm.intake``): the party sync, then, unless it is a support request,
    an engagement with a "Respond to inquiry" task, and the message as a
    note. ``assigned_to`` becomes that engagement's owner."""

    class Meta:
        model = Lead
        fields = [
            "first_name", "last_name", "email", "phone", "company_name", "job_title",
            "source", "source_detail", "request_type", "message", "estimated_value", "project_timeline",
            "assigned_to", "city", "state_province", "postal_code", "country",
        ]
        field_classes = {"assigned_to": ProfileChoiceField}
        labels = {"company_name": "Company", "source_detail": "Source detail",
                  "request_type": "Request type", "estimated_value": "Estimated value",
                  "project_timeline": "Timeline", "assigned_to": "Owner", "state_province": "State / province"}
        help_texts = {
            "source_detail": "Optional: the campaign, show or person who referred them.",
            "request_type": "A support request opens no engagement.",
            "assigned_to": "Blank: the deployment's default lead owner.",
            "country": "Two-letter code.",
        }
        widgets = {"message": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, company_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.company_id = company_id
        self.fields["assigned_to"].queryset = tenant_profiles(company_id)
        self.fields["assigned_to"].empty_label = "Default owner"
        self.fields["request_type"].choices = [("", "Not given")] + list(Lead.RequestType.choices)
        if not self.is_bound:
            self.initial.setdefault("source", Lead.Source.PHONE)

    def clean_country(self):
        value = (self.cleaned_data.get("country") or "").strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValidationError("A two-letter country code (US, CA, …).")
        return value

    def _post_clean(self):
        self.instance.company_id = self.company_id
        super()._post_clean()
