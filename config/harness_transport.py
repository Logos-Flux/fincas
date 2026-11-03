"""The release harness's offline provider stub (docs/release-testing.md, R10.07).

The fixture provider (``golden_erp/addons/fixture_crm``) talks to
``https://api.fixture.example.com``, which exists only inside the test
runner's ``FixtureTransport``. Outside it, "Test connection" on the
Connected-accounts screen could never succeed. ``config.settings.harness``
installs this transport for the whole server process
(``core.integrations.transport.install_transport``), so the fixture
provider answers locally: ``GET /v1/me`` is the connection test, pushes and
polls get an empty, well-formed answer. Any other host is refused with
``TransportError``: the harness never reaches a real provider.

No Django import here: the settings module imports it before apps load.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from core.integrations.transport import Response, TransportError

FIXTURE_HOST = "api.fixture.example.com"
WORKSPACE = "Release harness (offline stub)"


class HarnessStubTransport:
    """Answers the fixture provider's API locally; every other host is refused."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    @staticmethod
    def _json(payload: Any, status: int = 200) -> Response:
        return Response(status=status, headers={"content-type": "application/json"},
                        body=json.dumps(payload).encode("utf-8"))

    def request(self, method: str, url: str, *, headers: dict, params: dict | None = None,
                json: Any = None, data: Any = None, timeout: float = 10.0) -> Response:
        parts = urlsplit(url)
        self.requests.append((method, parts.path))
        if parts.hostname != FIXTURE_HOST:
            raise TransportError("ConnectError")  # the class name only, as HttpxTransport reports it
        if not headers.get("Authorization", "").startswith("Bearer "):
            return self._json({"error": "unauthenticated"}, 401)
        path = parts.path.rstrip("/")
        if path == "/v1/me":
            return self._json({"workspace": WORKSPACE, "user": "harness"})
        if method == "POST" and path == "/v1/contacts":
            key = headers.get("Idempotency-Key") or (json or {}).get("email") or ""
            return self._json({"id": "c_" + hashlib.sha256(str(key).encode()).hexdigest()[:12]})
        if method in ("GET", "POST") and path.startswith("/v1/"):
            return self._json({"items": [], "next": None})
        return self._json({"error": "not found"}, 404)


def install() -> HarnessStubTransport:
    from core.integrations.transport import install_transport

    stub = HarnessStubTransport()
    install_transport(stub)
    return stub
