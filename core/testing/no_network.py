"""No live sockets in the test suite (docs/plugins.md section 6, CRM Phase 5 5.4).

``docs/plugins.md`` promises that "CI fails on live sockets": adapter contract
tests run on recorded fixtures only. This module is what makes that true.
``config.settings.test`` sets ``TEST_RUNNER`` to :class:`NoNetworkRunner`,
which installs the guard before any test runs (and in every worker process
of a ``--parallel`` run).

While installed:

- ``socket.socket.connect`` / ``connect_ex`` raise :class:`LiveSocketError`
  for any address that is not loopback, a Unix socket, or a configured
  database / cache host. ``requests``, ``httpx``, ``urllib``, ``smtplib`` and
  ``asyncio`` all connect through these two methods, so they are covered
  without patching each library's transport.
- ``socket.getaddrinfo``, ``gethostbyname`` and ``gethostbyname_ex`` raise
  for names other than loopback names, IP literals and the allowed hosts, so
  a test that only *resolves* a provider host fails too.

What stays allowed (the allowlist):

- loopback (``127.0.0.0/8``, ``::1``, ``localhost``): ``LiveServerTestCase``,
  asyncio's self-pipe, and in-process ASGI/WSGI test clients (which never
  open a socket at all, so they need nothing);
- ``AF_UNIX`` sockets;
- the hosts of ``settings.DATABASES`` and ``settings.CACHES`` (a Postgres or
  Redis service container when the suite runs against one);
- extra hosts in ``settings.GOLDEN_TEST_NETWORK_ALLOW`` (a list of names or
  IP literals). Empty in golden.

A test that genuinely needs the network (none today) opts out explicitly with
``@allow_network()`` or ``with allow_network():``. The opt-out is process-wide
(other threads, e.g. a live-server thread, are opened too) for its duration.

**Swallowed refusals still fail the run.** Code under test that catches every
exception (``except Exception``) would hide ``LiveSocketError``, so every
refusal is also recorded; :class:`NoNetworkRunner` lists them after the run
and counts them as failures. Tests that provoke the guard on purpose wrap the
call in :func:`expect_violations`. (Recording is per process: under
``--parallel`` a worker's swallowed refusals are not reported back. CI runs
serially.)

Not covered: UDP ``sendto`` without ``connect``, ``gethostbyaddr`` /
``getnameinfo``, network clients implemented in C that bypass Python's
``socket`` module, and child processes started by a test (they do not inherit
the patch).
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import ContextDecorator
from typing import Any, Iterable

from django.test.runner import DiscoverRunner, ParallelTestSuite

__all__ = [
    "LiveSocketError",
    "NoNetworkRunner",
    "allow_network",
    "expect_violations",
    "install",
    "is_installed",
    "uninstall",
    "violations",
]

LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})

_ORIGINALS: dict[str, Any] = {}
_ALLOWED_HOSTS: set[str] = set()
_ALLOWED_IPS: set[str] = set()
_OPT_OUT_DEPTH = 0
#: Every refusal in this process ("<action> <target>"), swallowed or not.
_VIOLATIONS: list[str] = []
_EXPECTED_DEPTH = 0


class LiveSocketError(RuntimeError):
    """A test tried to reach a non-loopback host. Message = problem + cause + fix + docs."""

    def __init__(self, target: str, action: str = "connect to"):
        if not _EXPECTED_DEPTH:
            _VIOLATIONS.append(f"{action} {target}")
        super().__init__(
            f"Test tried to {action} {target} — cause: the test suite forbids live "
            "network access (core.testing.no_network). Fix: mock the client "
            "(unittest.mock.patch on the call site) or replay a recorded fixture; a "
            "test that truly needs the network wraps the call in "
            "core.testing.no_network.allow_network(). "
            "See docs/plugins.md#6-payroll--source-adapters."
        )
        self.target = target


def _ip_literal(host: str):
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _host_allowed(host: Any) -> bool:
    if _OPT_OUT_DEPTH:
        return True
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip().lower().rstrip(".")
    if host in ("", *LOOPBACK_NAMES) or host in _ALLOWED_HOSTS:
        return True
    ip = _ip_literal(host)
    if ip is None:
        return False
    if ip.is_loopback or ip.is_unspecified or str(ip) in _ALLOWED_IPS:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(mapped and (mapped.is_loopback or str(mapped) in _ALLOWED_IPS))


def _address_allowed(sock: socket.socket, address: Any) -> bool:
    if getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX:
        return True
    if isinstance(address, tuple) and address:
        return _host_allowed(address[0])
    # Any other family (netlink, packet, ...) has no remote host to reach.
    return sock.family not in (socket.AF_INET, socket.AF_INET6)


def _describe(address: Any) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return repr(address)


def _guarded_connect(self, address):
    if not _address_allowed(self, address):
        raise LiveSocketError(_describe(address))
    return _ORIGINALS["connect"](self, address)


def _guarded_connect_ex(self, address):
    if not _address_allowed(self, address):
        raise LiveSocketError(_describe(address))
    return _ORIGINALS["connect_ex"](self, address)


def _resolve_allowed(host: Any) -> bool:
    """Resolving an IP literal never leaves the machine; connecting is what is checked."""
    if isinstance(host, (str, bytes)):
        text = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if _ip_literal(text.strip()) is not None:
            return True
    return _host_allowed(host)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _resolve_allowed(host):
        raise LiveSocketError(repr(host), action="resolve")
    return _ORIGINALS["getaddrinfo"](host, *args, **kwargs)


def _guarded_gethostbyname(host):
    if not _resolve_allowed(host):
        raise LiveSocketError(repr(host), action="resolve")
    return _ORIGINALS["gethostbyname"](host)


def _guarded_gethostbyname_ex(host):
    if not _resolve_allowed(host):
        raise LiveSocketError(repr(host), action="resolve")
    return _ORIGINALS["gethostbyname_ex"](host)


def _settings_hosts() -> set[str]:
    """Database and cache hosts from Django settings (service containers)."""
    from urllib.parse import urlsplit

    from django.conf import settings

    hosts: set[str] = set()
    for db in getattr(settings, "DATABASES", {}).values():
        host = (db.get("HOST") or "").strip()
        if host and not host.startswith("/"):  # a path is a Unix socket dir
            hosts.add(host)
    for cache in getattr(settings, "CACHES", {}).values():
        locations = cache.get("LOCATION") or []
        if isinstance(locations, str):
            locations = [locations]
        for location in locations:
            if "://" in str(location):
                host = urlsplit(str(location)).hostname
                if host:
                    hosts.add(host)
    hosts.update(getattr(settings, "GOLDEN_TEST_NETWORK_ALLOW", []) or [])
    return hosts


def install(extra_hosts: Iterable[str] | None = None) -> None:
    """Patch the socket module. Idempotent. Resolves allowed hosts first."""
    hosts = {h.strip().lower().rstrip(".") for h in (extra_hosts if extra_hosts is not None else _settings_hosts())}
    _ALLOWED_HOSTS.update(hosts)
    getaddrinfo = _ORIGINALS.get("getaddrinfo", socket.getaddrinfo)
    for host in hosts:
        if _ip_literal(host) is not None:
            _ALLOWED_IPS.add(str(_ip_literal(host)))
            continue
        try:  # resolve before the guard is live: a DB host's IPs are allowed
            for info in getaddrinfo(host, None):
                _ALLOWED_IPS.add(info[4][0].split("%", 1)[0])
        except OSError:
            pass
    if _ORIGINALS:
        return
    _ORIGINALS.update(
        connect=socket.socket.connect,
        connect_ex=socket.socket.connect_ex,
        getaddrinfo=socket.getaddrinfo,
        gethostbyname=socket.gethostbyname,
        gethostbyname_ex=socket.gethostbyname_ex,
    )
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.gethostbyname = _guarded_gethostbyname
    socket.gethostbyname_ex = _guarded_gethostbyname_ex


def uninstall() -> None:
    """Restore the socket module (used by the runner's teardown)."""
    if not _ORIGINALS:
        return
    socket.socket.connect = _ORIGINALS["connect"]
    socket.socket.connect_ex = _ORIGINALS["connect_ex"]
    socket.getaddrinfo = _ORIGINALS["getaddrinfo"]
    socket.gethostbyname = _ORIGINALS["gethostbyname"]
    socket.gethostbyname_ex = _ORIGINALS["gethostbyname_ex"]
    _ORIGINALS.clear()
    _ALLOWED_HOSTS.clear()
    _ALLOWED_IPS.clear()


def is_installed() -> bool:
    return bool(_ORIGINALS) and socket.socket.connect is _guarded_connect


class allow_network(ContextDecorator):
    """Explicit opt-out for a test that genuinely needs the network."""

    def __enter__(self):
        global _OPT_OUT_DEPTH
        _OPT_OUT_DEPTH += 1
        return self

    def __exit__(self, *exc):
        global _OPT_OUT_DEPTH
        _OPT_OUT_DEPTH -= 1
        return False


class expect_violations(ContextDecorator):
    """For tests of the guard itself: refusals inside are not reported as violations."""

    def __enter__(self):
        global _EXPECTED_DEPTH
        _EXPECTED_DEPTH += 1
        return self

    def __exit__(self, *exc):
        global _EXPECTED_DEPTH
        _EXPECTED_DEPTH -= 1
        return False


def violations() -> list[str]:
    return list(_VIOLATIONS)


class NoNetworkParallelTestSuite(ParallelTestSuite):
    """``--parallel`` workers install the guard too (spawn/forkserver start fresh)."""

    def process_setup(*args):  # called as a plain function by Django's _init_worker
        install()


class NoNetworkRunner(DiscoverRunner):
    """The test runner ``config.settings.test`` names: DiscoverRunner + the guard."""

    parallel_test_suite = NoNetworkParallelTestSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        install()

    def suite_result(self, suite, result, **kwargs):
        failures = super().suite_result(suite, result, **kwargs)
        if _VIOLATIONS:
            import sys

            sys.stderr.write(
                f"\nLIVE SOCKET VIOLATIONS ({len(_VIOLATIONS)}): code under test tried to reach the "
                "network, even where the error was caught (core.testing.no_network):\n"
                + "".join(f"  - {v}\n" for v in _VIOLATIONS)
            )
            failures += len(_VIOLATIONS)
        return failures

    def teardown_test_environment(self, **kwargs):
        uninstall()
        super().teardown_test_environment(**kwargs)
