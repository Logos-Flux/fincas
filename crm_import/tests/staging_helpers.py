"""Build canonical exports in a temp directory and fake what the appliers
(steps 4-6, not built yet) will do, so staging and re-import can be tested."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.contrib.contenttypes.models import ContentType

from crm_import.canonical import OBJECT_TYPES, make_record, write_manifest, write_records

SOURCE = "examplecrm:4242"


def contact(sid, *, given="Ada", family="Example", emails=(), phones=(), addresses=(), aliases=(),
            archived=False, properties=None, source=SOURCE, job_title=None):
    return make_record("contact", source=source, source_id=str(sid), aliases=aliases, archived=archived, data={
        "given_name": given, "family_name": family,
        "emails": [{"address": e, "primary": i == 0} for i, e in enumerate(emails)],
        "phones": [{"raw": p, "kind": "phone", "primary": i == 0} for i, p in enumerate(phones)],
        "addresses": [dict({"purpose": "mailing", "line_1": None, "line_2": None, "city": None, "region": None,
                            "postal_code": None, "country": None, "primary": i == 0}, **a)
                      for i, a in enumerate(addresses)],
        "owner_ref": None, "company_refs": [], "marketable": None, "job_title": job_title,
    }, properties=properties or {}, created_at="2025-01-15T10:00:00Z", updated_at="2026-09-30T08:00:00Z")


def company(sid, *, name="Acme Widgets", domains=(), legal_name=None, source=SOURCE):
    return make_record("company", source=source, source_id=str(sid), data={
        "name": name, "legal_name": legal_name, "domains": list(domains), "phones": [], "addresses": [],
        "owner_ref": None, "registration_number": None, "tax_id": None,
    })


def owner(sid, *, email, active=True, given="Olive", family="Owner", source=SOURCE):
    return make_record("owner", source=source, source_id=str(sid), archived=not active, data={
        "email": email, "given_name": given, "family_name": family, "display_name": f"{given} {family}",
        "active": active, "teams": [],
    })


def pipeline(sid, *, key="sales", stages=("new", "won"), applies_to="deal", outcomes=None, source=SOURCE):
    outcomes = outcomes or {}
    return make_record("pipeline", source=source, source_id=str(sid), data={
        "applies_to": applies_to, "key": key, "name": key.title(), "sort_order": 0, "active": True,
        "stages": [{"source_id": f"{sid}-{k}", "key": k, "name": k.title(), "sort_order": i,
                    "probability": None, "outcome": outcomes.get(k, "")} for i, k in enumerate(stages)],
    })


def deal(sid, *, pipeline_sid="p1", stage_sid="p1-new", title="Big deal", amount=None, source=SOURCE):
    return make_record("deal", source=source, source_id=str(sid), data={
        "title": title, "pipeline_ref": {"object_type": "pipeline", "source_id": pipeline_sid},
        "stage_ref": {"object_type": "stage", "source_id": stage_sid}, "status": "open", "closed_at": None,
        "amount": amount, "currency": "USD", "expected_close_date": None, "owner_ref": None,
        "company_refs": [], "contact_refs": [], "loss_reason_raw": None, "follow_up": [],
    })


class ExportDir:
    """A temp directory holding one valid canonical export."""

    def __init__(self, testcase, records_by_type: dict, *, source=SOURCE):
        self.path = Path(tempfile.mkdtemp(prefix="crm-stage-"))
        testcase.addCleanup(shutil.rmtree, self.path, True)
        self.write(records_by_type, source=source)

    def write(self, records_by_type: dict, *, source=SOURCE) -> Path:
        for otype in OBJECT_TYPES:
            write_records(self.path, otype, records_by_type.get(otype, []))
        write_manifest(
            self.path, provider=source.split(":", 1)[0], source=source,
            adapter={"id": "test", "version": "0.0.1", "schema_version": 1}, exported_at="2026-10-01T09:00:00Z",
            input={"kind": "crawl", "time_zone": None},
            objects={t: {"coverage": "complete"} for t in OBJECT_TYPES},
        )
        return self.path


def link_ref(company, obj, source_id, *, object_type="contact", source=SOURCE):
    from core.models import ExternalRef

    return ExternalRef.objects.create(
        company=company, source=source, object_type=object_type, source_id=str(source_id),
        content_type=ContentType.objects.get_for_model(type(obj)), object_id=obj.pk,
    )


def make_person(company, *, given="Ada", family="Example", emails=(), phones=(), postcode=None, country="US",
                user=None):
    from parties.models import Party, PartyAddress, PartyEmail, PartyPhone

    party = Party.objects.create(company=company, kind="person", given_name=given, family_name=family,
                                 display_name=f"{given} {family}".strip(), origin="erp", user=user)
    for i, e in enumerate(emails):
        PartyEmail.objects.create(party=party, email=e, purpose="work", is_primary=i == 0)
    for i, p in enumerate(phones):
        PartyPhone.objects.create(party=party, number_raw=p, purpose="work", is_primary=i == 0)
    if postcode:
        PartyAddress.objects.create(party=party, purpose="mailing", address_line_1="1 Main St", city="Town", state_province="",
                                    postal_code=postcode, country=country)
    return party


def make_org(company, name, *, domain=""):
    from parties.models import Party

    return Party.objects.create(company=company, kind="organisation", display_name=name, primary_domain=domain,
                                origin="erp")


def fake_apply_contacts(job):
    """What step 4's contact applier will do, minimally: create or update the
    party from ``mapped_values["values"]``, write the ExternalRef, mark the row
    applied. Lets the re-import compare run against a real previous job."""
    from crm_import.stage import job_rows_filter
    from parties.models import Party, PartyEmail
    from unify.models import StagedRow

    provider = job.source.split(":", 1)[0]
    for row in StagedRow.objects.filter(job_rows_filter([job.pk], "contact"),
                                        mapping_state=StagedRow.MappingState.APPROVED):
        mv = row.mapped_values
        values = mv["values"]
        if mv["decision"] == "create":
            party = Party.objects.create(company=job.company, kind="person", given_name=values["given_name"],
                                         family_name=values["family_name"], origin="import",
                                         display_name=f"{values['given_name']} {values['family_name']}")
            for e in values["emails"]:
                PartyEmail.objects.create(party=party, email=e, purpose="work")
            link_ref(job.company, party, mv["external_ref"]["source_id"])
        else:
            party = Party.objects.get(pk=mv["target"]["id"])
            fields = {f: values[f] for f in ("given_name", "family_name") if f in values}
            Party.objects.filter(pk=party.pk).update(**fields)
            party.refresh_from_db()
            if mv["decision"] == "link":
                link_ref(job.company, party, mv["external_ref"]["source_id"])
        sidecar = {k.removeprefix("properties."): v for k, v in values.items() if k.startswith("properties.")}
        # Phase 4's flat provider keys ("<provider>.<name>"), as core.sidecar.set_fields stores them.
        party.metadata = {f"{provider}.{k}": v for k, v in sidecar.items()}
        party.save(update_fields=["metadata"])
        row.mapping_state = StagedRow.MappingState.APPLIED
        row.applied_ref = f"party:{party.pk}"
        row.save(update_fields=["mapping_state", "applied_ref", "updated_at"])
