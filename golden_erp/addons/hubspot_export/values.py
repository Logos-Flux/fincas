"""Value conversions shared by the crawl and CSV readers.

Every function is pure and deterministic: the same input always gives the
same output string, so two adapter runs are byte-identical.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from core.plugins.payroll import AdapterError

DOCS = "See docs/crm/importing.md section 8."

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:?\d{2})?$"
)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_EPOCH_MS_RE = re.compile(r"^\d{12,14}$")
_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def text(value) -> str | None:
    """A nullable string: unset (None or blank) is None, anything else verbatim."""
    if blank(value):
        return None
    return value if isinstance(value, str) else str(value)


def format_instant(moment: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` in UTC, trailing fraction zeros dropped."""
    moment = moment.astimezone(timezone.utc)
    out = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        out += "." + f"{moment.microsecond:06d}".rstrip("0")
    return out + "Z"


def parse_instant(value, *, where: str, local_zone: ZoneInfo | None = None) -> str | None:
    """A HubSpot instant as a canonical UTC string, or None when unset.

    Accepts the API's ISO strings (``2024-03-04T10:15:00.000Z``), epoch
    milliseconds (older endpoints) and, with ``local_zone``, the offset-less
    ``YYYY-MM-DD HH:MM[:SS]`` of a CSV export (resolved with ``fold=0``).
    An offset-less value without a zone is refused: guessing would shift
    every timestamp by the exporting user's offset.
    """
    if blank(value):
        return None
    raw = str(value).strip()
    if _EPOCH_MS_RE.match(raw):
        return format_instant(datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc))
    match = _ISO_RE.match(raw)
    if not match:
        raise AdapterError(
            f"{where}: {raw!r} is not a date-time — cause: the export holds a value in a format the "
            "adapter does not read. Fix: re-export the property with HubSpot's default format. " + DOCS
        )
    year, month, day, hour, minute, second, frac, offset = match.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    naive = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0), micro)
    if offset is None:
        if local_zone is None:
            raise AdapterError(
                f"{where}: {raw!r} has no UTC offset — cause: CSV exports render date-times in the "
                "exporting user's time zone. Fix: pass --time-zone <IANA name> (the exporting user's "
                "zone, e.g. America/New_York). " + DOCS
            )
        return format_instant(naive.replace(tzinfo=local_zone, fold=0))
    if offset == "Z":
        return format_instant(naive.replace(tzinfo=timezone.utc))
    sign = 1 if offset[0] == "+" else -1
    digits = offset[1:].replace(":", "")

    tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    return format_instant(naive.replace(tzinfo=tz))


def parse_date(value, *, where: str) -> str | None:
    """A date-only value (``YYYY-MM-DD``; a midnight-UTC instant or epoch ms
    from older endpoints is accepted and stays a date)."""
    if blank(value):
        return None
    raw = str(value).strip()
    if _DATE_RE.match(raw):
        date.fromisoformat(raw)
        return raw
    if _EPOCH_MS_RE.match(raw):
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).date().isoformat()
    match = _ISO_RE.match(raw)
    if match:
        return raw[:10]
    raise AdapterError(f"{where}: {raw!r} is not a date (YYYY-MM-DD). " + DOCS)


def parse_decimal(value) -> str | None:
    """An amount as a canonical decimal string; blank is None (never "0").

    A value the canonical grammar cannot hold (not a number, more than six
    decimal places after normalising) is None: the original stays in
    ``properties`` and the importer reports the row.
    """
    if blank(value):
        return None
    raw = str(value).strip()
    if "," in raw:
        # Only thousands separators: "1,200.50". "1,5" or "1.200,50" are refused, never guessed.
        if not re.match(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$", raw):
            return None
        raw = raw.replace(",", "")
    if not _DECIMAL_RE.match(raw) or len(raw.lstrip("-").split(".")[0]) > 15:
        return None
    if "." in raw and len(raw.split(".", 1)[1]) > 6:
        return None
    return raw


def parse_bool(value) -> bool | None:
    if blank(value):
        return None
    raw = str(value).strip().lower()
    if raw in ("true", "yes", "1"):
        return True
    if raw in ("false", "no", "0"):
        return False
    return None


def split_multi(value) -> list[str]:
    """HubSpot's multi-value encoding (``a;b;c``), blanks dropped, order kept."""
    if blank(value):
        return []
    seen: list[str] = []
    for part in str(value).split(";"):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return seen


def id_key(value: str) -> tuple:
    """Natural order for HubSpot ids: numeric ids by value, then the rest."""
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


def slug_key(raw: str) -> str:
    """The default pipeline/stage key for a HubSpot id (spec 2.4)."""
    key = re.sub(r"[^a-z0-9_]+", "_", str(raw).lower()).strip("_")
    key = re.sub(r"_+", "_", key)
    if not key or not key[0].isalpha():
        key = "hs_" + key
    return key[:32].rstrip("_")


def is_key(value) -> bool:
    return isinstance(value, str) and bool(_KEY_RE.match(value))


def address_hash(address: str) -> str:
    """A short stable token for an address, so consent source ids hold no address."""
    return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()[:16]


class _TextExtractor(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(value) -> str | None:
    """Plain text from an HTML body: tags removed, block ends as line breaks,
    entities decoded, runs of spaces collapsed. None for an empty body."""
    if blank(value):
        return None
    raw = str(value)
    if "<" not in raw:
        return unescape(raw).strip() or None
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in "".join(parser.parts).split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip() or None


def looks_like_html(value) -> bool:
    return isinstance(value, str) and bool(re.search(r"<[a-zA-Z/][^>]*>", value))
