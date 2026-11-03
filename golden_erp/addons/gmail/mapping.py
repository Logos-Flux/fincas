"""Gmail message resource <-> golden values. Pure functions, no database, no network.

The Gmail API v1 message resource (``users.messages.get``)::

    {"id", "threadId", "labelIds": [...], "historyId", "internalDate": "<ms>",
     "snippet", "sizeEstimate",
     "payload": {"mimeType", "filename", "headers": [{"name", "value"}],
                 "body": {"size", "data"?: base64url, "attachmentId"?},
                 "parts": [<payload>...]}}

``format=metadata`` answers the same shape with only the asked-for headers
and no bodies; ``format=full`` adds the parts and their bodies (attachment
bodies are never inline: they carry an ``attachmentId``).
"""

from __future__ import annotations

import base64
import binascii
import codecs
import copy
import hashlib
import re
from datetime import datetime
from datetime import timezone as dt_timezone
from email.utils import getaddresses
from html.parser import HTMLParser

from core.plugins.payroll import AdapterError

from .values import BODY_CAP_BYTES, DOCS, GOLDEN_LOG_HEADER

#: Header -> participant role. Bcc is recorded, but never makes mail "known" (3.3).
ROLE_HEADERS = (("From", "from"), ("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"))
#: Roles whose parties qualify a message for ingestion (3.3: From / To / Cc).
QUALIFYING_ROLES = frozenset({"from", "to", "cc"})

_MSGID = re.compile(r"<([^<>\s]+)>")
_SPACE = re.compile(r"[ \t\r\f\v\xa0]+")
_BLANK_LINES = re.compile(r"\n\s*\n(\s*\n)+")


def headers(message: dict) -> list[tuple[str, str]]:
    payload = message.get("payload") if isinstance(message, dict) else None
    rows = payload.get("headers") if isinstance(payload, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and isinstance(row.get("value"), str):
            out.append((row["name"], row["value"]))
    return out


def header(message: dict, name: str) -> str:
    """The first value of header ``name`` (case-insensitive), or ''."""
    wanted = name.lower()
    return next((value for key, value in headers(message) if key.lower() == wanted), "")


def header_values(message: dict, name: str) -> list[str]:
    wanted = name.lower()
    return [value for key, value in headers(message) if key.lower() == wanted]


def normalize_message_id(value: str) -> str:
    """``<Id@Host>`` -> ``id@host``: the first angle-bracketed token, lower-cased.

    A value without brackets is taken whole (trimmed). Lower-casing trades
    RFC 5322's case-sensitive local part for robustness against clients
    that re-case it; two distinct ids differing only by case are not a
    realistic collision.
    """
    value = (value or "").strip()
    found = _MSGID.search(value)
    token = found.group(1) if found else value.split()[0] if value.split() else ""
    return token.strip("<>").lower()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mailbox_hash(address: str) -> str:
    """A mailbox named without its address (fallback keys, logs)."""
    return _sha((address or "").strip().lower())[:16]


def message_id(message: dict) -> str:
    return normalize_message_id(header(message, "Message-ID"))


def external_id(message: dict, mailbox: str) -> str:
    """``mid:<sha256(Message-ID)>``: one email copied into several mailboxes is one key.

    A message without a ``Message-ID`` falls back to the mailbox's own id
    (``gmail:<mailbox hash>:<message id>``): it cannot be matched across
    mailboxes, so it never is.
    """
    mid = message_id(message)
    if mid:
        return "mid:" + _sha(mid)
    gid = message.get("id") if isinstance(message, dict) else None
    if not isinstance(gid, str) or not gid:
        raise AdapterError(f"gmail: message has neither a Message-ID nor an id — cause: a malformed "
                           f"resource. Fix: check the API version the envelope names. {DOCS}")
    return f"gmail:{mailbox_hash(mailbox)}:{gid}"


def thread_root(message: dict) -> str:
    """The root ``Message-ID`` of the thread: first of References, else In-Reply-To, else its own."""
    for name in ("References", "In-Reply-To"):
        ids = [normalize_message_id(m.group(0)) for m in _MSGID.finditer(header(message, name))]
        if ids:
            return ids[0]
    return message_id(message)


def thread_key(message: dict, mailbox: str) -> str:
    """``mid:<sha256(root)[:40]>``: the same thread in every mailbox (Gmail's threadId is per mailbox)."""
    root = thread_root(message)
    if root:
        return "mid:" + _sha(root)[:40]
    tid = message.get("threadId") if isinstance(message, dict) else None
    return f"gmail:{mailbox_hash(mailbox)}:t:{tid}" if isinstance(tid, str) and tid else ""


def addresses(message: dict) -> list[tuple[str, str, str]]:
    """``(role, address, display name)`` for From / To / Cc / Bcc, in header order, deduplicated."""
    out, seen = [], set()
    for name, role in ROLE_HEADERS:
        for display, address in getaddresses(header_values(message, name)):
            address = (address or "").strip()
            if "@" not in address:
                continue
            key = (role, address.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append((role, address, (display or "").strip()[:255]))
    return out


def occurred_at(message: dict) -> datetime:
    raw = message.get("internalDate") if isinstance(message, dict) else None
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        raise AdapterError(f"gmail: message has no internalDate — cause: a malformed resource. "
                           f"Fix: check the API version the envelope names. {DOCS}") from None
    return datetime.fromtimestamp(millis / 1000, tz=dt_timezone.utc)


def label_ids(message: dict) -> set[str]:
    labels = message.get("labelIds") if isinstance(message, dict) else None
    return {label for label in labels if isinstance(label, str)} if isinstance(labels, list) else set()


def golden_log_id(message: dict) -> str:
    """The ``X-Golden-Email-Log`` value (an EmailLog id) when golden sent this message."""
    return header(message, GOLDEN_LOG_HEADER).strip()


def _parts(part: dict):
    yield part
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            yield from _parts(child)


def _is_attachment(part: dict) -> bool:
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    return bool(part.get("filename")) or bool(body.get("attachmentId"))


_CHARSET = re.compile(r"charset\s*=\s*\"?([A-Za-z0-9._:-]+)", re.I)
#: HTML is cut to this before conversion: the text is capped anyway, and the parser's work stays linear.
HTML_INPUT_CAP = 4 * BODY_CAP_BYTES
_BLOCK_TAGS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"})
_LINE_TAGS = frozenset({"br", "li", "tr"})
_SKIPPED_TAGS = frozenset({"script", "style", "head", "title"})


def _charset(part: dict) -> str:
    for name, value in ((h.get("name"), h.get("value")) for h in part.get("headers") or [] if isinstance(h, dict)):
        if isinstance(name, str) and name.lower() == "content-type" and isinstance(value, str):
            found = _CHARSET.search(value)
            if found:
                try:
                    return codecs.lookup(found.group(1)).name
                except LookupError:
                    return "utf-8"
    return "utf-8"


def _raw_bytes(data: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return b""


def _decode(data: str, charset: str = "utf-8") -> str:
    return _raw_bytes(data).decode(charset, errors="replace")


def _cap(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= BODY_CAP_BYTES:
        return text
    return encoded[:BODY_CAP_BYTES].decode("utf-8", errors="ignore")


class _TextExtractor(HTMLParser):
    """HTML to text in one linear pass: script/style/head dropped, block and line breaks kept."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED_TAGS:
            self.skipping += 1
        elif tag in _LINE_TAGS:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _LINE_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIPPED_TAGS:
            self.skipping = max(0, self.skipping - 1)
        elif tag in _BLOCK_TAGS:
            self.out.append("\n\n")
        elif tag in ("li", "tr"):
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(data)


def html_to_text(markup: str) -> str:
    """HTML mail to plain text: block breaks kept, tags dropped, entities decoded."""
    parser = _TextExtractor()
    try:
        parser.feed(markup[:HTML_INPUT_CAP])
        parser.close()
    except Exception:  # noqa: BLE001 — html.parser is lenient; never let markup fail an ingest
        pass
    lines = [_SPACE.sub(" ", line).strip() for line in "".join(parser.out).splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _text_parts(message: dict):
    root = message.get("payload") if isinstance(message, dict) else None
    if not isinstance(root, dict):
        return
    for part in _parts(root):
        if _is_attachment(part):
            continue
        data = (part.get("body") or {}).get("data") if isinstance(part.get("body"), dict) else None
        if isinstance(data, str) and data:
            yield str(part.get("mimeType") or "").lower(), part, data


def body_text(message: dict) -> str:
    """The first inline ``text/plain`` part, else the first ``text/html`` part as text; capped at 100 kB.

    Decoded in the part's own charset (``Content-Type``), UTF-8 when it names none.
    """
    plain = html_part = None
    for mime, part, data in _text_parts(message):
        if mime == "text/plain" and plain is None:
            plain = (part, data)
        elif mime == "text/html" and html_part is None:
            html_part = (part, data)
    if plain is not None:
        return _cap(_decode(plain[1], _charset(plain[0])).replace("\r\n", "\n").strip())
    if html_part is not None:
        raw = _raw_bytes(html_part[1])[:HTML_INPUT_CAP]
        return _cap(html_to_text(raw.decode(_charset(html_part[0]), errors="replace")))
    return ""


def raw_message(message: dict) -> dict:
    """The message resource for ``CommunicationRaw``: no attachment or binary bodies, text capped.

    Every part keeps its headers, name, size and attachment id. Only inline
    ``text/plain`` / ``text/html`` parts keep their data, cut to 100 kB of
    the encoded bytes (``"truncated": true`` when cut); every other part's
    data (attachments, inline images, calendar parts) is dropped.
    """
    out = copy.deepcopy(message)
    root = out.get("payload")
    if isinstance(root, dict):
        for part in _parts(root):
            body = part.get("body")
            if not isinstance(body, dict) or "data" not in body:
                continue
            mime = str(part.get("mimeType") or "").lower()
            if _is_attachment(part) or mime not in ("text/plain", "text/html"):
                part["body"] = {k: body[k] for k in ("attachmentId", "size") if k in body}
                continue
            data = _raw_bytes(str(body.get("data") or ""))
            if len(data) > BODY_CAP_BYTES:
                body["data"] = base64.urlsafe_b64encode(data[:BODY_CAP_BYTES]).decode().rstrip("=")
                body["truncated"] = True
    return out


def attachments(message: dict) -> list[dict]:
    root = message.get("payload") if isinstance(message, dict) else None
    if not isinstance(root, dict):
        return []
    return [{"filename": str(p.get("filename") or "")[:255], "size": (p.get("body") or {}).get("size")}
            for p in _parts(root) if _is_attachment(p)]
