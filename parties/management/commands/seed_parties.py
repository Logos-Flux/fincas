"""Seed demo parties with realistic contact points (CRM Phase 2 step 9).

    manage.py seed_parties                  # top up to 40 seeded parties
    manage.py seed_parties --count 10000    # performance rehearsal
    manage.py seed_parties --company <uuid>

Idempotent: it counts the parties it seeded before (``origin=import``,
``metadata.seed == "seed_parties"``) and only creates the difference, so
``seed_demo_data`` can call it on every run. Data is deterministic per
index (no randomness), uses reserved example domains and 555-01xx numbers,
and never touches a customer, so the party sync does not run.

Rows are written with ``bulk_create`` for speed, so the values ``save()``
would derive (normalised email and phone, denormalised primary columns,
the child rows' ``company``) are computed here with the same functions.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import Company
from parties.constants import ORGANISATION, PERSON
from parties.models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
)
from parties.normalize import normalize_email, normalize_phone, person_display_name, phone_line_type
from parties.relationship_registry import EMPLOYMENT
from parties.role_registry import PARTNER_ROLE, REFERRAL_SOURCE_ROLE, SUPPLIER_ROLE

SEED_TAG = "seed_parties"
BATCH = 1000
ORG_EVERY = 5  # one organisation per five parties

GIVEN = [
    "Ada", "Grace", "Alan", "Katherine", "Linus", "Margaret", "Dennis", "Barbara", "Ken",
    "Frances", "Edsger", "Hedy", "Tim", "Radia", "John", "Sophie", "Niklaus", "Anita",
    "Donald", "Mary", "Guido", "Shafi", "Bjarne", "Adele", "Yukihiro", "Lynn", "James",
    "Carol", "Leslie", "Jean",
]
FAMILY = [
    "Lovelace", "Hopper", "Turing", "Johnson", "Torvalds", "Hamilton", "Ritchie", "Liskov",
    "Thompson", "Allen", "Dijkstra", "Lamarr", "Berners-Lee", "Perlman", "Backus", "Wilson",
    "Wirth", "Borg", "Knuth", "Keller", "van Rossum", "Goldwasser", "Stroustrup", "Goldberg",
    "Matsumoto", "Conway", "Gosling", "Shaw", "Lamport", "Sammet",
]
ORG_STEM = [
    "Northwind", "Bluefin", "Copperleaf", "Ironbridge", "Silverline", "Oakridge", "Brightwater",
    "Redstone", "Granite Peak", "Harborview", "Summit", "Fieldstone", "Lakeshore", "Pinecrest",
]
ORG_SUFFIX = ["Supply", "Logistics", "Electric", "Fabrication", "Systems", "Trading", "Works"]
CITIES = [
    ("Springfield", "IL", "627"), ("Portland", "OR", "972"), ("Austin", "TX", "787"),
    ("Madison", "WI", "537"), ("Burlington", "VT", "054"), ("Boulder", "CO", "803"),
    ("Raleigh", "NC", "276"), ("Tucson", "AZ", "857"),
]
AREA_CODES = ["217", "503", "512", "608", "802", "303", "919", "520"]


class Command(BaseCommand):
    help = "Seed demo parties (people and organizations) with contact points; idempotent top-up."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=40,
                            help="Total seeded parties wanted for the company (default 40).")
        parser.add_argument("--company", help="Company pk (default: the only company).")

    def handle(self, *args, **options):
        count = options["count"]
        if count < 0:
            raise CommandError("--count must be zero or more.")
        company = self._company(options.get("company"))
        seeded = Party.objects.filter(company=company, metadata__seed=SEED_TAG)
        existing = seeded.count()
        wanted = max(0, count - existing)
        if not wanted:
            self.stdout.write(f"  Parties: {existing} seeded already for '{company.name}'; nothing to add.")
            return
        # Continue after the highest index used, so deleted rows never make a
        # later run reuse an index (and its email address).
        indexes = seeded.values_list("metadata__seed_index", flat=True)
        start = max((int(i) for i in indexes if i is not None), default=-1) + 1
        done = 0
        while done < wanted:
            size = min(BATCH, wanted - done)
            with transaction.atomic():
                self._seed_batch(company, start + done, size)
            done += size
        self.stdout.write(
            self.style.SUCCESS(f"  Parties: created {wanted} for '{company.name}' ({existing + wanted} seeded).")
        )

    @staticmethod
    def _company(pk):
        if pk:
            try:
                return Company.objects.get(pk=pk)
            except (Company.DoesNotExist, ValueError) as exc:
                raise CommandError(f"No company {pk!r}.") from exc
        companies = list(Company.objects.order_by("created_at")[:2])
        if len(companies) != 1:
            raise CommandError("Pass --company: there is not exactly one company.")
        return companies[0]

    def _seed_batch(self, company, offset, size):
        now = timezone.now()
        today = timezone.localdate()
        parties, emails, phones, addresses, roles, consents = [], [], [], [], [], []
        orgs = {}
        people = []

        for i in range(offset, offset + size):
            city, state, zip3 = CITIES[i % len(CITIES)]
            if i % ORG_EVERY == 0:
                stem = ORG_STEM[(i // ORG_EVERY) % len(ORG_STEM)]
                suffix = ORG_SUFFIX[(i // ORG_EVERY) % len(ORG_SUFFIX)]
                name = f"{stem} {suffix} {i // ORG_EVERY + 1}"
                domain = f"{stem.lower().replace(' ', '')}{i // ORG_EVERY + 1}.example.com"
                party = Party(
                    company=company, kind=ORGANISATION, display_name=name, legal_name=f"{name} LLC",
                    primary_domain=domain, origin=Party.Origin.IMPORT,
                    metadata={"seed": SEED_TAG, "seed_index": i},
                )
                email_raw = f"info@{domain}"
                orgs[i] = party
            else:
                given = GIVEN[i % len(GIVEN)]
                family = FAMILY[(i // len(GIVEN)) % len(FAMILY)]
                party = Party(
                    company=company, kind=PERSON, given_name=given, family_name=family,
                    display_name=person_display_name(given, family), origin=Party.Origin.IMPORT,
                    marketable=i % 3 == 0, marketable_source="import:seed" if i % 3 == 0 else "",
                    marketable_changed_at=now if i % 3 == 0 else None,
                    metadata={"seed": SEED_TAG, "seed_index": i},
                )
                slug = f"{given}.{family}".lower().replace(" ", "").replace("-", "")
                email_raw = f"{slug}{i}@example.org"
                people.append((i, party))
            email_norm = normalize_email(email_raw)
            phone_raw = f"({AREA_CODES[i % len(AREA_CODES)]}) 555-{100 + i % 100:04d}"
            e164, _ext = normalize_phone(phone_raw)
            party.primary_email_normalized = email_norm
            party.primary_phone_e164 = e164
            parties.append(party)
            emails.append(PartyEmail(
                party=party, company=company, email=email_raw, email_normalized=email_norm,
                purpose="work" if party.kind == ORGANISATION else "personal", is_primary=True,
                source="seed",
            ))
            phones.append(PartyPhone(
                party=party, company=company, number_raw=phone_raw, number_e164=e164,
                line_type=phone_line_type(e164), purpose="work", is_primary=bool(e164), source="seed",
            ))
            if i % 2 == 0:
                addresses.append(PartyAddress(
                    party=party, company=company, purpose="mailing", is_primary=True,
                    address_line_1=f"{100 + i % 900} Main St", city=city, state_province=state,
                    postal_code=f"{zip3}{i % 100:02d}", country="US", source="seed",
                ))
            if i % 7 == 0:
                role = SUPPLIER_ROLE if party.kind == ORGANISATION else PARTNER_ROLE
                roles.append(PartyRole(party=party, company=company, role=role,
                                       valid_from=today - datetime.timedelta(days=i % 365), source="seed"))
            if i % 11 == 0:
                roles.append(PartyRole(party=party, company=company, role=REFERRAL_SOURCE_ROLE,
                                       valid_from=today - datetime.timedelta(days=30), source="seed"))
            if party.kind == PERSON and i % 4 == 1:
                consents.append(ConsentRecord(
                    party=party, company=company, channel="email", purpose="marketing",
                    state="granted" if i % 8 == 1 else "withdrawn",
                    occurred_at=now - datetime.timedelta(days=i % 200), source="import",
                    source_detail="seed_parties",
                ))

        Party.objects.bulk_create(parties, batch_size=BATCH)
        PartyEmail.objects.bulk_create(emails, batch_size=BATCH)
        PartyPhone.objects.bulk_create(phones, batch_size=BATCH)
        PartyAddress.objects.bulk_create(addresses, batch_size=BATCH)
        PartyRole.objects.bulk_create(roles, batch_size=BATCH)
        ConsentRecord.objects.bulk_create(consents, batch_size=BATCH)

        # Each person works at the organisation seeded just before it; that
        # organisation may come from an earlier batch or run.
        org_rows = dict(orgs)
        missing = {i - i % ORG_EVERY for i, _p in people} - set(org_rows)
        if missing:
            for org in Party.objects.filter(
                company=company, kind=ORGANISATION, metadata__seed=SEED_TAG,
                metadata__seed_index__in=sorted(missing),
            ):
                org_rows[org.metadata["seed_index"]] = org
        relationships = []
        for i, person in people:
            org = org_rows.get(i - i % ORG_EVERY)
            if org is None:
                continue
            relationships.append(PartyRelationship(
                company=company, from_party=person, to_party=org, kind=EMPLOYMENT,
                is_symmetric=False, is_primary=True, title="Buyer" if i % 2 else "Engineer",
                valid_from=today - datetime.timedelta(days=400), source="seed",
            ))
        PartyRelationship.objects.bulk_create(relationships, batch_size=BATCH)
