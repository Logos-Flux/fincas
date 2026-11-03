"""Vocabulary shared by the parties registries, normaliser and (step 3) models.

docs/crm/phase2-party.md section 1. Values are persisted, so never rename one.
"""

PERSON = "person"
ORGANISATION = "organisation"
PARTY_KINDS = (PERSON, ORGANISATION)

# PartyPhone.line_type (1.4). `fax` is never inferred: phonenumbers has no fax
# type, so only staff set it.
LINE_MOBILE = "mobile"
LINE_LANDLINE = "landline"
LINE_FAX = "fax"
LINE_UNKNOWN = "unknown"
LINE_TYPES = (LINE_MOBILE, LINE_LANDLINE, LINE_FAX, LINE_UNKNOWN)
