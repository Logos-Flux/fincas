"""Time-zone resolution for due times (docs/crm/phase3-engagement.md 1.5, 7).

``UserProfile.timezone`` and ``Company.timezone`` hold IANA names, validated
against ``zoneinfo.available_timezones()``. ``resolve(profile, company)`` picks
the zone a new due time is set in: the profile's (the assignee's), else the
company's, else ``settings.TIME_ZONE``, else UTC.

Instants are always stored in UTC; the resolved zone only decides what "end of
the local day" means for an all-day task. This module imports no models, so
``core.models`` can use ``validate_timezone`` as a field validator.

``core/migrations/0007_timezones.py`` references ``core.tz.validate_timezone``
by path: never rename or move it without leaving a shim, or a fresh
``migrate`` breaks.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django.conf import settings
from django.core.exceptions import ValidationError

UTC = ZoneInfo("UTC")


# In the tz database but never a zone a person means: ``localtime`` is a
# symlink to the host's /etc/localtime (differs per container), ``Factory``
# a placeholder whose abbreviation is "-00".
_NOT_USER_ZONES = frozenset({"localtime", "Factory"})


@lru_cache(maxsize=1)
def _known_zones():
    # available_timezones() walks the tz database on every call; the set does
    # not change while the process runs.
    return frozenset(available_timezones()) - _NOT_USER_ZONES


def is_valid_timezone(name):
    """True when ``name`` is an IANA zone name ``zoneinfo`` knows."""
    return isinstance(name, str) and name in _known_zones()


def validate_timezone(value):
    """Field validator: refuse anything that is not an IANA zone name.

    Blank is allowed by the fields themselves (``blank=True``); Django skips
    validators for empty values, so this only sees non-empty input.
    """
    if not is_valid_timezone(value):
        raise ValidationError(
            "%(value)s is not a known IANA time zone (for example Europe/Berlin).",
            code="invalid_timezone",
            params={"value": value},
        )


def _zone(name):
    if not name or not is_valid_timezone(name):
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def resolve(profile=None, company=None):
    """Return the ``ZoneInfo`` a new due time is set in.

    Order (spec 1.5): ``profile.timezone`` (pass the *assignee's* profile, so
    an all-day task lands on the right day in the assignee's list), then
    ``company.timezone``, then ``settings.TIME_ZONE``, then UTC. A stored
    value that is no longer a known zone (a tz database update removed it, or
    a row written by ``update()`` skipped validation) is skipped, never
    raised: a bad zone must not make a task unsaveable.

    When ``company`` is None the profile's company is used (one query unless
    the caller ``select_related("company")``; pass ``company`` in loops).
    """
    if company is None and profile is not None:
        company = getattr(profile, "company", None)
    for name in (
        getattr(profile, "timezone", "") if profile is not None else "",
        getattr(company, "timezone", "") if company is not None else "",
        getattr(settings, "TIME_ZONE", None),
    ):
        zone = _zone(name)
        if zone is not None:
            return zone
    return UTC
