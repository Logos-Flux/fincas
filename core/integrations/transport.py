"""The one way an adapter talks HTTP (docs/crm/phase6-adapters.md 2.3).

An adapter never imports ``httpx`` or ``requests``: it is constructed with a
:class:`Transport` and calls ``transport.request``. The downstream runs
:class:`HttpxTransport`; golden's tests run ``FixtureTransport``
(``core.testing.integration_fixtures``), and test ``HttpxTransport`` itself
only with ``httpx.MockTransport``, which opens no socket.

:func:`get_transport` is what core code (the poller, the outbox handlers, the
OAuth views) calls to build an adapter. Tests swap it with
:func:`use_transport`; there is no setting for it.
"""

from __future__ import annotations

import contextlib
import json as _json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Protocol

__all__ = [
    "HttpxTransport",
    "Response",
    "Transport",
    "TransportError",
    "get_transport",
    "install_transport",
    "use_transport",
]


@dataclass(frozen=True)
class Response:
    """What every transport returns. ``headers`` keys are lower-case."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        if not self.body:
            return None
        return _json.loads(self.body.decode("utf-8"))

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class TransportError(Exception):
    """The request never got an HTTP answer (DNS, connect, timeout, TLS).

    The message is the exception class only: an httpx message can quote the
    URL, and a URL can carry a token in its query.
    """


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: dict, params: dict | None = None,
                json: Any = None, data: bytes | dict | None = None, timeout: float = 10.0) -> Response: ...


class HttpxTransport:
    """``Transport`` over one ``httpx.Client``. No retries here: waiting is the outbox's job.

    httpx logs every request URL at INFO, so a credential never goes in a URL
    or a query string: headers or a form body only (``FixtureTransport``
    fails a request that breaks this).
    """

    def __init__(self, client=None):
        import httpx

        self._client = client or httpx.Client(follow_redirects=False)

    def request(self, method, url, *, headers, params=None, json=None, data=None, timeout=10.0):

        try:
            resp = self._client.request(
                method, url, headers=headers, params=params, json=json, data=data, timeout=timeout,
            )
        except Exception as exc:  # httpx.InvalidURL etc. too: never let a URL or header escape
            raise TransportError(type(exc).__name__) from None
        return Response(
            status=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            body=resp.content,
        )

    def close(self) -> None:
        self._client.close()


_factory: Callable[[Any], Transport] | None = None
_shared: HttpxTransport | None = None


class RefusingTransport:
    """What tests get when they forgot :func:`use_transport`: any request fails loudly.

    It raises ``LiveSocketError``, which the no-network runner records as a
    violation even when adapter code swallows the exception.
    """

    def request(self, method, url, **kwargs):
        from core.testing.no_network import LiveSocketError

        raise LiveSocketError(f"{method} {url.split('?', 1)[0]} (no use_transport() in this test)")


def get_transport(account=None) -> Transport:
    """The transport for ``account``'s adapter: one shared ``HttpxTransport`` unless a test swapped it."""
    global _shared
    if _factory is not None:
        return _factory(account)
    from core.testing.no_network import is_installed

    if is_installed():
        return RefusingTransport()
    if _shared is None:
        _shared = HttpxTransport()
    return _shared


def install_transport(factory: Callable[[Any], Transport] | Transport | None) -> None:
    """Make :func:`get_transport` return ``factory(account)`` (or the instance)
    for the rest of the process; ``None`` restores the default.

    Not a setting (the docstring above holds): the one caller is
    ``config.settings.harness``, which installs the release harness's
    offline stub so "Test connection" on the fixture provider answers
    without a network. Tests use :func:`use_transport`.
    """
    global _factory
    if factory is None:
        _factory = None
    else:
        _factory = factory if callable(factory) and not hasattr(factory, "request") else (lambda _a: factory)


@contextlib.contextmanager
def use_transport(factory: Callable[[Any], Transport] | Transport) -> Iterator[None]:
    """Inside the block, :func:`get_transport` returns ``factory(account)`` (or the instance)."""
    global _factory
    previous = _factory
    _factory = factory if callable(factory) and not hasattr(factory, "request") else (lambda _a: factory)
    try:
        yield
    finally:
        _factory = previous
