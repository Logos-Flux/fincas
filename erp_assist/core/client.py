"""HTTP client for the golden ERP DRF API.

Wraps httpx with authentication, consistent error handling, and pagination helpers.
All `core.*` domain modules call into this client — no other module talks HTTP.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import httpx

from ._cache import TTLCache

DEFAULT_BASE = os.environ.get("ERP_BASE_URL", "http://localhost:8000")
DEFAULT_TIMEOUT = 20.0


def _debug_enabled() -> bool:
    """True if ERP_ASSIST_DEBUG env var is set to a truthy value."""
    val = os.environ.get("ERP_ASSIST_DEBUG", "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


def _debug_log(method: str, url: str, status: int | str, duration_ms: float) -> None:
    """Write a single request trace line to stderr.

    Stderr is used deliberately — the MCP server speaks JSON-RPC over stdout
    and anything printed there would corrupt the protocol. Format is one line
    per request so it's easy to grep:

        erp-assist GET /api/v1/products/products/?page_size=20 200 187ms
    """
    # Strip the scheme/host so the line is compact; DRF paths are the useful bit
    split = urlsplit(url)
    path = split.path + (f"?{split.query}" if split.query else "")
    print(
        f"erp-assist {method} {path} {status} {duration_ms:.0f}ms",
        file=sys.stderr,
        flush=True,
    )


class ERPError(Exception):
    """Base class for ERP client errors."""


class ERPAuthError(ERPError):
    """401/403 from the ERP — credentials missing or rejected."""


class ERPNotFoundError(ERPError):
    """404 from the ERP — resource doesn't exist."""


class ERPValidationError(ERPError):
    """400 from the ERP — payload was rejected. `.details` holds DRF's field errors."""

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


class ERPAmbiguousError(ERPError):
    """A free-text reference did not resolve to exactly one record.

    Raised instead of guessing (docs/crm/phase2-party.md 2.7): when a lookup
    is ``ambiguous`` (several exact or several search hits), and by write
    tools when the only hit is ``fuzzy`` (it needs the user's confirmation
    first). ``.envelope`` is the server's match envelope
    ``{match_quality, item, candidates, message}``; the MCP adapter returns
    it as the tool result so the assistant can ask the user which one.
    """

    def __init__(self, envelope: dict[str, Any], message: str | None = None) -> None:
        self.envelope = dict(envelope)
        if message:
            self.envelope["message"] = message
        names = ", ".join(
            f"{c.get('display_name')} <{c.get('primary_email') or '-'}> ({c.get('id')})"
            for c in self.candidates
        )
        super().__init__(f"{self.envelope.get('message', 'Ambiguous match.')} Candidates: {names or 'none'}")

    @property
    def candidates(self) -> list[dict[str, Any]]:
        return list(self.envelope.get("candidates") or [])


@dataclass(frozen=True)
class ERPConfig:
    base_url: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "ERPConfig":
        api_key = os.environ.get("GOLDEN_ERP_API_KEY")
        if not api_key:
            raise ERPAuthError(
                "GOLDEN_ERP_API_KEY is not set. Add it to ~/.bashrc or pass it explicitly."
            )
        base = os.environ.get("GOLDEN_ERP_BASE", DEFAULT_BASE).rstrip("/")
        return cls(base_url=base, api_key=api_key)


class ERPClient:
    """Async HTTP client for the ERP DRF API.

    Use as an async context manager:

        async with ERPClient.from_env() as client:
            products = await client.list("/api/v1/products/products/")
    """

    def __init__(self, config: ERPConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout,
            # Follow 3xx redirects automatically. Without this, when Django
            # has SECURE_SSL_REDIRECT=True and the base_url is plain http
            # (e.g. an in-process loopback to localhost:8000), the API call
            # gets a 301 with an empty body and downstream pagination
            # crashes on a None payload. With redirects enabled we follow
            # to https://<host>/... transparently.
            follow_redirects=True,
            headers={
                "Authorization": f"Api-Key {config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        # Shared TTL cache for near-immutable lookups (brands, categories,
        # warehouses, price lists). Populated lazily by the cached lookup
        # functions in each domain module.
        self.lookup_cache = TTLCache()

    @classmethod
    def from_env(cls) -> "ERPClient":
        return cls(ERPConfig.from_env())

    async def __aenter__(self) -> "ERPClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._http.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- Raw verbs -------------------------------------------------------

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, *, json: dict[str, Any]) -> Any:
        return await self._request("POST", path, json=json)

    async def patch(self, path: str, *, json: dict[str, Any]) -> Any:
        return await self._request("PATCH", path, json=json)

    async def put(self, path: str, *, json: dict[str, Any]) -> Any:
        return await self._request("PUT", path, json=json)

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path, expect_json=False)

    # ---- Helpers ---------------------------------------------------------

    async def list(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch a DRF list endpoint, following `next` pages until exhausted or max_items hit."""
        results: list[dict[str, Any]] = []
        async for item in self.iter_list(path, params=params):
            results.append(item)
            if max_items is not None and len(results) >= max_items:
                break
        return results

    async def iter_list(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over a paginated DRF list endpoint.

        DRF's `next` URL already contains the encoded query string (page number,
        page_size, filters). We pass it to httpx as an absolute URL with
        `params=None`, NOT `params={}`, because httpx silently strips the URL's
        existing query string when given an empty-dict `params` alongside a
        base_url — an unexpected but documented interaction.
        """
        next_url: str | None = path
        next_params: dict[str, Any] | None = dict(params or {})
        while next_url:
            payload = await self._request("GET", next_url, params=next_params)
            # DRF can return either a plain list or a paginated object
            if isinstance(payload, list):
                for item in payload:
                    yield item
                return
            for item in payload.get("results", []):
                yield item
            next_url = payload.get("next")
            next_params = None  # next URL already encodes query params; empty dict would strip them

    # ---- Internals -------------------------------------------------------

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        debug = _debug_enabled()
        start = time.perf_counter() if debug else 0.0
        try:
            response = await self._http.request(
                method,
                path_or_url,
                params=params,
                json=json,
            )
        except httpx.HTTPError as exc:
            if debug:
                _debug_log(method, path_or_url, f"ERR:{type(exc).__name__}", (time.perf_counter() - start) * 1000)
            raise ERPError(f"HTTP transport error: {exc}") from exc
        if debug:
            # The final URL (with merged base_url + params) is on response.request.url
            final_url = str(response.request.url) if response.request else path_or_url
            _debug_log(method, final_url, response.status_code, (time.perf_counter() - start) * 1000)

        if response.status_code == 401 or response.status_code == 403:
            raise ERPAuthError(f"{response.status_code} from {method} {path_or_url}")
        if response.status_code == 404:
            raise ERPNotFoundError(f"{method} {path_or_url} returned 404")
        if response.status_code == 400:
            detail: Any
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise ERPValidationError(
                f"{method} {path_or_url} returned 400: {detail}", details=detail
            )
        if response.status_code >= 500:
            raise ERPError(f"{response.status_code} server error from {path_or_url}: {response.text[:300]}")
        if response.status_code >= 400:
            raise ERPError(
                f"{response.status_code} from {method} {path_or_url}: {response.text[:300]}"
            )

        if not expect_json or response.status_code == 204 or not response.content:
            return None
        return response.json()
