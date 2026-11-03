"""The test suite's socket guard (core.testing.no_network).

docs/plugins.md section 6 promises that CI fails on live sockets. These tests
prove the guard is on for the whole suite (via config.settings.test's
TEST_RUNNER), that it fires for real clients, and that loopback, Unix sockets
and the explicit opt-out still work. No test here reaches the network: every
refused call raises before a packet is sent.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import threading
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from core.testing import no_network
from core.testing.no_network import LiveSocketError, allow_network, expect_violations

TEST_NET = "203.0.113.10"  # RFC 5737 documentation range: never routable


class GuardIsOnTests(SimpleTestCase):
    def test_test_settings_name_the_guarded_runner(self):
        self.assertEqual(settings.TEST_RUNNER, "core.testing.no_network.NoNetworkRunner")

    def test_guard_is_installed_for_this_run(self):
        self.assertTrue(no_network.is_installed())


class ExpectsViolations:
    """These tests provoke the guard on purpose; keep them out of the run's report."""

    def setUp(self):
        super().setUp()
        ctx = expect_violations()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)


class GuardFiresTests(ExpectsViolations, SimpleTestCase):
    def test_raw_connect_to_a_public_address(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with self.assertRaises(LiveSocketError) as cm:
                sock.connect((TEST_NET, 80))
        self.assertIn(f"{TEST_NET}:80", str(cm.exception))
        self.assertIn("allow_network", str(cm.exception))

    def test_connect_ex_too(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with self.assertRaises(LiveSocketError):
                sock.connect_ex((TEST_NET, 443))

    def test_connect_by_name(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with self.assertRaises(LiveSocketError):
                sock.connect(("example.com", 80))

    def test_ipv6_public_address(self):
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            with self.assertRaises(LiveSocketError):
                sock.connect(("2001:db8::1", 80, 0, 0))

    def test_name_resolution(self):
        with self.assertRaises(LiveSocketError):
            socket.getaddrinfo("example.com", 443)
        with self.assertRaises(LiveSocketError):
            socket.gethostbyname("example.com")
        with self.assertRaises(LiveSocketError):
            socket.gethostbyname_ex("example.com")

    def test_create_connection(self):
        with self.assertRaises(LiveSocketError):
            socket.create_connection(("api.example.com", 443), timeout=1)

    def test_requests(self):
        import requests

        with self.assertRaises(LiveSocketError):
            requests.get("https://api.example.com/v1/ping", timeout=1)

    def test_httpx(self):
        import httpx

        with self.assertRaises(LiveSocketError):
            httpx.get("https://api.example.com/v1/ping", timeout=1)

    def test_urllib(self):
        import urllib.request

        with self.assertRaises(LiveSocketError):
            urllib.request.urlopen(f"http://{TEST_NET}/", timeout=1)


class AllowlistTests(ExpectsViolations, SimpleTestCase):
    def test_loopback_round_trip(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            with socket.create_connection(("localhost", port), timeout=2) as client:
                conn, _ = server.accept()
                with conn:
                    client.sendall(b"ping")
                    self.assertEqual(conn.recv(4), b"ping")

    def test_unix_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Linux abstract namespace: no filesystem path, so no 108-byte limit
            path = f"\0golden-no-network-{os.getpid()}" if sys.platform == "linux" else os.path.join(tmp, "s")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen(1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(path)

    def test_asyncio_loop_runs(self):
        """asyncio's self-pipe and in-process ASGI tests need no network."""

        async def ping():
            await asyncio.sleep(0)
            return "pong"

        self.assertEqual(asyncio.run(ping()), "pong")

    def test_loopback_and_literals_resolve(self):
        socket.getaddrinfo("localhost", 80)
        socket.getaddrinfo("127.0.0.1", 80)
        socket.getaddrinfo(TEST_NET, 80)  # a literal resolves locally; connecting is what is refused

    @override_settings(
        DATABASES={"default": {"HOST": "db.internal.example"}, "other": {"HOST": "/var/run/postgresql"}},
        CACHES={"default": {"LOCATION": "redis://cache.internal.example:6379/0"}},
        GOLDEN_TEST_NETWORK_ALLOW=["10.9.8.7"],
    )
    def test_settings_hosts(self):
        self.assertEqual(
            no_network._settings_hosts(),
            {"db.internal.example", "cache.internal.example", "10.9.8.7"},
        )

    def test_allowed_ip_literal(self):
        try:
            no_network.install(extra_hosts=["10.9.8.7"])
            self.assertTrue(no_network._host_allowed("10.9.8.7"))
            self.assertFalse(no_network._host_allowed("10.9.8.8"))
        finally:
            no_network._ALLOWED_HOSTS.discard("10.9.8.7")
            no_network._ALLOWED_IPS.discard("10.9.8.7")

    def test_explicit_opt_out(self):
        fake_connect = mock.Mock(return_value=None)
        with mock.patch.dict(no_network._ORIGINALS, {"connect": fake_connect}):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                with allow_network():
                    sock.connect((TEST_NET, 80))
                with self.assertRaises(LiveSocketError):
                    sock.connect((TEST_NET, 80))
        fake_connect.assert_called_once()

    def test_guard_applies_in_threads(self):
        errors = []

        def worker():
            try:
                socket.create_connection((TEST_NET, 80), timeout=1)
            except LiveSocketError as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        self.assertEqual(len(errors), 1)


class InstallUninstallTests(ExpectsViolations, SimpleTestCase):
    def test_uninstall_restores_and_install_is_idempotent(self):
        guarded = socket.socket.connect
        no_network.uninstall()
        try:
            self.assertFalse(no_network.is_installed())
            self.assertIsNot(socket.socket.connect, guarded)
        finally:
            no_network.install()
            no_network.install()
        self.assertTrue(no_network.is_installed())
        with self.assertRaises(LiveSocketError):
            socket.create_connection((TEST_NET, 80), timeout=1)

    def test_parallel_workers_install_the_guard(self):
        with mock.patch.object(no_network, "install") as install:
            no_network.NoNetworkParallelTestSuite.process_setup()
        install.assert_called_once_with()


class SwallowedViolationTests(SimpleTestCase):
    def test_a_caught_refusal_is_still_recorded(self):
        before = len(no_network._VIOLATIONS)
        try:
            try:
                socket.create_connection((TEST_NET, 80), timeout=1)
            except Exception:  # what `except Exception` in product code does
                pass
            self.assertEqual(no_network.violations()[before:], [f"connect to {TEST_NET}:80"])
        finally:
            del no_network._VIOLATIONS[before:]  # keep this deliberate one out of the run's report

    def test_expected_refusals_are_not_recorded(self):
        before = len(no_network._VIOLATIONS)
        with expect_violations(), self.assertRaises(LiveSocketError):
            socket.getaddrinfo("example.com", 80)
        self.assertEqual(len(no_network._VIOLATIONS), before)

    def test_runner_counts_violations_as_failures(self):
        runner = no_network.NoNetworkRunner(verbosity=0)
        result = mock.Mock(failures=[], errors=[], unexpectedSuccesses=[])
        with mock.patch.object(no_network, "_VIOLATIONS", ["connect to x:1"]), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(runner.suite_result(mock.Mock(), result), 1)
        self.assertIn("connect to x:1", "".join(c.args[0] for c in err.write.call_args_list))
