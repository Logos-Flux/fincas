"""The one LLM client: purpose in, completion out.

Callers name a *purpose* (``"research"``), never a model. The client resolves
mode, base URL, key and route through ``conf`` and speaks the OpenAI
Chat Completions / Embeddings shape over raw ``httpx`` — the same wire shape
``chat/llm/openai_compat.py`` uses, so a LiteLLM gateway and any
OpenAI-compatible provider are interchangeable behind it.

Async core, sync wrapper — on purpose. The ERP serves ASGI and chat is fully
async, while some callers (research jobs, management commands) are plain sync
code. ``complete_async`` never blocks the event loop; ``complete`` is a thin
``async_to_sync`` wrapper for the sync callers.

Every error carries ``purpose``, ``route``, ``key_source`` (db | env |
default | unset) and a ``category`` from one taxonomy shared with the
settings page:

    bad_lane        400/404 — the gateway does not know the route
    auth            401     — key rejected (at the edge or upstream)
    edge_forbidden  403     — the edge refused this path/caller
    rate_limited    429     — key budget or rpm exhausted
    cold_lane       503     — upstream warming (scale-to-zero)
    timeout                 — no response inside the timeout
    unreachable             — connection refused / DNS / TLS
    shape                   — a 200 whose body is not a usable completion
    upstream                — any other 5xx
    config                  — nothing to call: no base URL / key / route
    dimension               — embedding length differs from the pinned size
    no_tool_call            — test probe: the route cannot call tools
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from asgiref.sync import async_to_sync, sync_to_async
from django.utils import timezone

from core import cache_guard

from . import conf
from .purposes import load_purposes

logger = logging.getLogger(__name__)

RESOLVED_MODEL_HEADER = "x-litellm-model-name"
MODELS_CACHE_KEY = "llmhub:models"
OBSERVE_CACHE_PREFIX = "llmhub:observed:"
# Skip the DB write when the same route→model was recorded this recently.
OBSERVE_WRITE_INTERVAL_S = 300

_WARMUP_INITIAL_DELAY = 3.0
_WARMUP_MAX_DELAY = 15.0


class LLMHubError(RuntimeError):
    """A failed LLM call, attributed: which purpose, route and key source."""

    def __init__(
        self,
        message: str,
        *,
        purpose: str = "",
        route: str = "",
        key_source: str = "",
        category: str = "upstream",
        status: int | None = None,
        detail: Any = None,
    ) -> None:
        self.message = message
        self.purpose = purpose
        self.route = route
        self.key_source = key_source
        self.category = category
        self.status = status
        self.detail = detail
        super().__init__(str(self))

    def __str__(self) -> str:
        ctx = ", ".join(
            f"{k}={v}"
            for k, v in (
                ("purpose", self.purpose),
                ("route", self.route),
                ("key_source", self.key_source),
                ("category", self.category),
                ("status", self.status),
            )
            if v not in ("", None)
        )
        return f"{self.message} [{ctx}]" if ctx else self.message


class LLMHubConfigError(LLMHubError):
    """Nothing to call — raised before any request is made."""

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("category", "config")
        super().__init__(message, **kw)


class DimensionMismatch(LLMHubError):
    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("category", "dimension")
        super().__init__(message, **kw)


@dataclass
class Completion:
    text: str
    purpose: str
    route: str
    resolved_model: str
    key_source: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def classify(status: int) -> str:
    if status in (400, 404, 422):
        return "bad_lane"
    if status == 401:
        return "auth"
    if status == 403:
        return "edge_forbidden"
    if status == 429:
        return "rate_limited"
    if status == 503:
        return "cold_lane"
    return "upstream"


def _error_body(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return {"raw": response.text[:500]}
    # Never echo request headers back into logs, even if an upstream does.
    if isinstance(body, dict):
        body.pop("headers", None)
    return body


# ---- endpoint resolution -------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    purpose: str
    mode: str
    base_url: str
    api_key: str = field(repr=False)
    key_source: str = ""
    route: str = ""


def endpoint_for(purpose: str, snap: conf.Snapshot) -> Endpoint:
    """Resolve where a purpose's calls go, or raise a config error naming every empty source."""
    route = conf.route_for(purpose, snap)
    ctx = dict(purpose=purpose, route=route.value, key_source=snap.api_key.source)
    if not route:
        raise LLMHubConfigError(
            f"No route for purpose {purpose!r} in {snap.mode} mode: nothing assigned on the "
            "settings page (DB lane_map) and no default in LLMHUB_PURPOSES.",
            **ctx,
        )
    if not snap.base_url:
        raise LLMHubConfigError(
            "No gateway base URL: LLMSettings.gateway_base_url is blank (db), LLMHUB_GATEWAY_BASE_URL "
            "is empty (env), and there is no built-in default.",
            **ctx,
        )
    if not snap.api_key:
        if snap.gateway:
            db_state = "undecryptable (Fernet key changed — re-enter it)" if snap.key_undecryptable else "empty"
            env_state = "empty" if snap.fallback_to_env else "ignored (fallback_to_env is off)"
            msg = (
                f"No gateway API key: the DB-stored caller token is {db_state} (db), "
                f"LLMHUB_GATEWAY_API_KEY is {env_state} (env), and there is no built-in default."
            )
        else:
            msg = (
                "No direct-mode API key: direct mode never uses the DB-stored gateway token (db), "
                "the provider key setting (LLMHUB_DIRECT_API_KEY_SETTING, default OPENAI_API_KEY) "
                "is empty (env), and there is no built-in default."
            )
        raise LLMHubConfigError(msg, **ctx)
    return Endpoint(
        purpose=purpose,
        mode=snap.mode,
        base_url=snap.base_url.value.rstrip("/"),
        api_key=snap.api_key.value,
        key_source=snap.api_key.source,
        route=route.value,
    )


# ---- lane observations ---------------------------------------------------


def _record_observation_sync(route: str, model: str) -> None:
    from .models import LaneObservation

    LaneObservation.objects.update_or_create(
        route=route, defaults={"model": model, "observed_at": timezone.now()}
    )


async def aobserve(route: str, model: str) -> None:
    """Record that ``route`` just served ``model`` (cache-guarded DB write)."""
    if not route or not model:
        return
    key = OBSERVE_CACHE_PREFIX + route
    if await cache_guard.cache_aget(key, what="llmhub observation cache") == model:
        return
    await cache_guard.cache_aset(key, model, OBSERVE_WRITE_INTERVAL_S, what="llmhub observation cache", fallback_ttl=OBSERVE_WRITE_INTERVAL_S)
    try:
        await sync_to_async(_record_observation_sync)(route, model)
    except Exception:  # noqa: BLE001 — an observation must never fail a completion
        logger.exception("llmhub: could not record lane observation %s → %s", route, model)


def observe(route: str, model: str) -> None:
    if not route or not model:
        return
    key = OBSERVE_CACHE_PREFIX + route
    if cache_guard.cache_get(key, what="llmhub observation cache") == model:
        return
    cache_guard.cache_set(key, model, OBSERVE_WRITE_INTERVAL_S, what="llmhub observation cache", fallback_ttl=OBSERVE_WRITE_INTERVAL_S)
    try:
        _record_observation_sync(route, model)
    except Exception:  # noqa: BLE001
        logger.exception("llmhub: could not record lane observation %s → %s", route, model)


def observations() -> dict[str, dict[str, Any]]:
    """route → {"model", "at"} for every route ever observed."""
    from .models import LaneObservation

    return {o.route: {"model": o.model, "at": o.observed_at} for o in LaneObservation.objects.all()}


# ---- completions -----------------------------------------------------------


async def complete_async(
    purpose: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    extra: dict[str, Any] | None = None,
    timeout: float = 120.0,
    warmup_max_wait_s: float = 0.0,
    snap: conf.Snapshot | None = None,
) -> Completion:
    """One non-streaming chat completion for ``purpose``.

    ``messages`` are OpenAI-shape. ``extra`` is merged into the payload
    verbatim (provider passthrough, e.g. Gemini ``tools=[{"googleSearch": {}}]``
    grounding through a LiteLLM lane).
    """
    snap = snap or await conf.asnapshot()
    ep = endpoint_for(purpose, snap)
    payload: dict[str, Any] = {"model": ep.route, "messages": list(messages), "stream": False}
    if system:
        payload["messages"] = [{"role": "system", "content": system}, *payload["messages"]]
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    if extra:
        payload.update(extra)

    ctx = dict(purpose=purpose, route=ep.route, key_source=ep.key_source)
    url = f"{ep.base_url}/v1/chat/completions"
    headers = {"authorization": f"Bearer {ep.api_key}", "content-type": "application/json"}
    started = time.monotonic()
    deadline = started + warmup_max_wait_s if warmup_max_wait_s > 0 else None
    delay = _WARMUP_INITIAL_DELAY

    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                raise LLMHubError(f"No response within {timeout:.0f}s", category="timeout", **ctx) from exc
            except httpx.HTTPError as exc:
                raise LLMHubError(f"Could not reach {ep.base_url}: {exc}", category="unreachable", **ctx) from exc
            if response.status_code == 503 and deadline is not None and time.monotonic() + delay <= deadline:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _WARMUP_MAX_DELAY)
                continue
            break

    if response.status_code >= 400:
        body = _error_body(response)
        logger.warning(
            "llmhub: %s failed purpose=%s route=%s key_source=%s status=%s",
            classify(response.status_code), purpose, ep.route, ep.key_source, response.status_code,
        )
        raise LLMHubError(
            f"{ep.mode} call returned HTTP {response.status_code}: {json.dumps(body)[:400]}",
            category=classify(response.status_code),
            status=response.status_code,
            detail=body,
            **ctx,
        )

    try:
        body = response.json()
        choice = body["choices"][0]
        message = choice.get("message") or {}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMHubError(f"Response is not a chat completion: {response.text[:300]}", category="shape", **ctx) from exc

    resolved = (response.headers.get(RESOLVED_MODEL_HEADER) or "").strip()
    if resolved and snap.gateway:
        await aobserve(ep.route, resolved)
    return Completion(
        text=(message.get("content") or "").strip() if isinstance(message.get("content"), str) else "",
        purpose=purpose,
        route=ep.route,
        resolved_model=resolved or body.get("model", ""),
        key_source=ep.key_source,
        tool_calls=list(message.get("tool_calls") or []),
        usage=body.get("usage") or {},
        elapsed_ms=int((time.monotonic() - started) * 1000),
        raw=body,
    )


def complete(purpose: str, messages: list[dict[str, Any]], **kwargs: Any) -> Completion:
    """Sync wrapper around :func:`complete_async` for sync callers (jobs, commands)."""
    if "snap" not in kwargs:
        kwargs["snap"] = conf.snapshot()
    return async_to_sync(complete_async)(purpose, messages, **kwargs)


# ---- embeddings (probe-only in the ERP) ------------------------------------


async def embed_async(
    texts: list[str],
    *,
    route: str,
    base_url: str | None = None,
    api_key: str | None = None,
    key_source: str = "",
    dimensions: int | None = None,
    timeout: float = 30.0,
    purpose: str = "",
) -> list[list[float]]:
    """``POST /v1/embeddings``. When ``dimensions`` is set, every vector must match it."""
    if base_url is None or api_key is None:
        snap = await conf.asnapshot()
        base_url = base_url if base_url is not None else snap.base_url.value
        if api_key is None:
            api_key, key_source = snap.api_key.value, snap.api_key.source
    ctx = dict(purpose=purpose, route=route, key_source=key_source)
    if not base_url or not api_key:
        raise LLMHubConfigError("Embedding probe has no base URL or key.", **ctx)
    url = f"{base_url.rstrip('/')}/v1/embeddings"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json={"model": route, "input": texts},
            )
    except httpx.TimeoutException as exc:
        raise LLMHubError(f"No response within {timeout:.0f}s", category="timeout", **ctx) from exc
    except httpx.HTTPError as exc:
        raise LLMHubError(f"Could not reach {base_url}: {exc}", category="unreachable", **ctx) from exc
    if response.status_code >= 400:
        raise LLMHubError(
            f"embeddings returned HTTP {response.status_code}: {json.dumps(_error_body(response))[:300]}",
            category=classify(response.status_code),
            status=response.status_code,
            **ctx,
        )
    try:
        vectors = [row["embedding"] for row in response.json()["data"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise LLMHubError(f"Response is not an embeddings list: {response.text[:200]}", category="shape", **ctx) from exc
    if dimensions is not None:
        bad = sorted({len(v) for v in vectors if len(v) != dimensions})
        if bad:
            raise DimensionMismatch(
                f"route returned {bad[0]}-dim vectors, pinned size is {dimensions} — re-pointing this "
                "route at a different embedder corrupts every stored vector",
                **ctx,
            )
    return vectors


def embed(texts: list[str], **kwargs: Any) -> list[list[float]]:
    return async_to_sync(embed_async)(texts, **kwargs)


# ---- models list -------------------------------------------------------------


@dataclass
class ModelsList:
    ids: list[str]
    as_of: Any = None
    from_cache: bool = False
    error_category: str = ""
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return not self.error_category


def _cached_models(row) -> tuple[list[str], Any]:
    hit = cache_guard.cache_get(MODELS_CACHE_KEY, what="llmhub models cache")
    if hit:
        return list(hit["ids"]), hit["as_of"]
    if row is not None and row.models_cache:
        return list(row.models_cache), row.models_cache_at
    return [], None


def available_models(*, timeout: float = 5.0, snap: conf.Snapshot | None = None) -> ModelsList:
    """``GET /v1/models`` with this deployment's key (the gateway filters it per key).

    Never raises: on failure it returns the last successful list, labelled
    with its timestamp, plus the error — the settings page must render when
    the gateway is down.
    """
    from .models import LLMSettings

    snap = snap or conf.snapshot(fresh=True)
    row = LLMSettings.objects.filter(pk=1).first()
    cached_ids, cached_at = _cached_models(row)

    def fallback(category: str, message: str) -> ModelsList:
        return ModelsList(cached_ids, cached_at, True, category, message)

    if not snap.base_url or not snap.api_key:
        return fallback("config", "No base URL or key resolves in this mode.")
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                f"{snap.base_url.value.rstrip('/')}/v1/models",
                headers={"authorization": f"Bearer {snap.api_key.value}"},
            )
    except httpx.TimeoutException:
        return fallback("timeout", f"No response within {timeout:.0f}s.")
    except httpx.HTTPError as exc:
        return fallback("unreachable", str(exc)[:200])
    if response.status_code >= 400:
        return fallback(classify(response.status_code), f"HTTP {response.status_code}")
    try:
        ids = sorted({m["id"] for m in response.json()["data"]})
    except (ValueError, KeyError, TypeError):
        return fallback("shape", "Response is not a models list.")
    now = timezone.now()
    cache_guard.cache_set(MODELS_CACHE_KEY, {"ids": ids, "as_of": now}, None, what="llmhub models cache")
    if row is not None:
        LLMSettings.objects.filter(pk=1).update(models_cache=ids, models_cache_at=now)
    return ModelsList(ids, now)


# ---- per-purpose test (settings page, llmhub_check) --------------------------

TOOL_PROBE = {
    "type": "function",
    "function": {
        "name": "get_server_time",
        "description": "Return the current server time. Takes no arguments.",
        "parameters": {"type": "object", "properties": {}},
    },
}


@dataclass
class TestResult:
    purpose: str
    route: str = ""
    status: str = "fail"  # ok | fail | skipped
    category: str = ""
    message: str = ""
    resolved_model: str = ""
    key_source: str = ""
    elapsed_ms: int = 0
    tool_call: str = "skipped"  # ok | missing | skipped
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Not a test class — keeps pytest from trying to collect it.
TestResult.__test__ = False  # type: ignore[attr-defined]


def _chat_installed() -> bool:
    from django.apps import apps

    return apps.is_installed("chat")


CHAT_TOOL_PROBE = {
    "name": "get_server_time",
    "description": "Return the current server time. Takes no arguments.",
    "input_schema": {"type": "object", "properties": {}},
}


async def _test_chat_drawer_path(result: TestResult, timeout: float) -> None:
    """Test ``chat_default`` exactly the way the chat drawer calls it.

    The registry's default entry + its adapter: in gateway mode that is the
    llmhub-resolved lane, in direct mode the code registry's default model
    with its own provider key (so a fresh direct-mode install passes with
    just ``ANTHROPIC_API_KEY``). Raises ``LLMHubError`` on failure.
    """
    from chat.llm import build_adapter
    from chat.llm.base import ModelResolved, StreamError, ToolUse
    from chat.llm.registry import default_model

    model = default_model()
    result.route = model.model_id
    if getattr(model, "credentials", "settings") != "llmhub":
        result.key_source = conf.SOURCE_ENV  # the code registry reads provider keys from settings/env
    adapter = build_adapter(model)
    tool_called = False

    async def drive() -> None:
        nonlocal tool_called
        async for event in adapter.stream(
            model=model.model_id,
            system="You are a connectivity probe.",
            messages=[{"role": "user", "content": "Call the get_server_time tool now. Do not answer in text."}],
            tools=[CHAT_TOOL_PROBE] if model.tools_mode == "full" else [],
            max_tokens=model.max_tokens or 1024,
            effort=model.effort,
            thinking=model.thinking,
        ):
            if isinstance(event, ModelResolved):
                result.resolved_model = event.name
            elif isinstance(event, ToolUse):
                tool_called = True
            elif isinstance(event, StreamError):
                status = None
                for code in (400, 401, 403, 404, 429, 503):
                    if f" {code}:" in event.message or f"returned {code}" in event.message:
                        status = code
                        break
                category = classify(status) if status else "upstream"
                if "not configured" in event.message:
                    category = "config"
                raise LLMHubError(
                    event.message[:300],
                    purpose="chat_default",
                    route=model.model_id,
                    key_source=result.key_source,
                    category=category,
                    status=status,
                )

    try:
        await asyncio.wait_for(drive(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise LLMHubError("timeout", purpose="chat_default", route=model.model_id, category="timeout") from exc
    result.resolved_model = result.resolved_model or model.model_id
    if model.tools_mode == "full":
        result.tool_call = "ok" if tool_called else "missing"
    result.message = "tool call" if tool_called else "answered"


async def test_purpose(purpose: str, *, timeout: float | None = None, snap: conf.Snapshot | None = None) -> TestResult:
    """One cheap live call for a purpose, classified for the settings page.

    Reads settings fresh (bypassing the TTL cache) so a just-pasted key is
    tested, not a worker's stale copy. Chat purposes flagged ``tools`` get a
    tool-call probe: a route that answers but cannot call tools would pass a
    plain ping and silently kill the chat's tool loop.
    """
    timeout = timeout or conf.test_timeout()
    spec = load_purposes()[purpose]
    snap = snap or await conf.asnapshot(fresh=True)
    result = TestResult(purpose=purpose, at=timezone.now().isoformat(), key_source=snap.api_key.source)
    started = time.monotonic()
    if not snap.gateway and spec.direct_handler:
        result.status = "skipped"
        result.message = f"Direct mode: handled by {spec.direct_handler}, not llmhub."
        return result
    try:
        if purpose == "chat_default" and _chat_installed():
            await _test_chat_drawer_path(result, timeout)
            if result.tool_call == "missing":
                result.category = "no_tool_call"
                result.message = "Route answered but made no tool call — ERP chat's tool loop would silently die."
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                return result
        elif spec.kind == "embedding":
            ep = endpoint_for(purpose, snap)
            result.route = ep.route
            vectors = await embed_async(
                ["llmhub connectivity probe"],
                route=ep.route,
                base_url=ep.base_url,
                api_key=ep.api_key,
                key_source=ep.key_source,
                dimensions=spec.dimensions,
                timeout=timeout,
                purpose=purpose,
            )
            result.message = f"{len(vectors[0])}-dim vector"
        else:
            result.route = conf.route_for(purpose, snap).value
            if spec.tools:
                done = await complete_async(
                    purpose,
                    [{"role": "user", "content": "Call the get_server_time tool now. Do not answer in text."}],
                    tools=[TOOL_PROBE],
                    max_tokens=1024,
                    timeout=timeout,
                    snap=snap,
                )
                result.tool_call = "ok" if done.tool_calls else "missing"
            else:
                done = await complete_async(
                    purpose,
                    [{"role": "user", "content": "Reply with the single word: pong"}],
                    max_tokens=1024,
                    timeout=timeout,
                    snap=snap,
                )
            result.resolved_model = done.resolved_model
            result.message = (done.text or "(tool call)")[:120]
            if result.tool_call == "missing":
                result.category = "no_tool_call"
                result.message = "Route answered but made no tool call — ERP chat's tool loop would silently die."
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                return result
        result.status = "ok"
    except LLMHubError as exc:
        result.category = exc.category
        result.route = result.route or exc.route
        result.message = exc.message[:300]
        if exc.category == "timeout":
            result.message = f"No response in {timeout:.0f}s — lane may be warming up."
    except Exception as exc:  # noqa: BLE001 — a test must report, never 500 the page
        logger.exception("llmhub: test_purpose(%s) crashed", purpose)
        result.category = "upstream"
        result.message = f"{type(exc).__name__}: {exc}"[:300]
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def save_test_result(result: TestResult) -> None:
    """Persist a test result for the status header (creates the row if needed)."""
    from django.db import transaction

    from .models import LLMSettings

    with transaction.atomic():
        row = LLMSettings.load(for_update=True)
        results = dict(row.last_test or {})
        results[result.purpose] = result.as_dict()
        LLMSettings.objects.filter(pk=1).update(last_test=results)
