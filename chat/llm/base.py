"""Protocol-agnostic LLM adapter interface.

Every concrete adapter (CF AI Gateway, direct Anthropic, direct OpenAI,
local vLLM) implements the same interface: `stream()` yields a sequence
of small, structured events the chat view can render incrementally.

Events are kept deliberately simple — text deltas, tool-use starts, and
a final "turn done" marker. The view assembles them into SSE messages
for the browser and into ChatMessage rows for persistence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal


@dataclass
class TextDelta:
    """A chunk of assistant text, streamed as the model generates."""

    text: str
    kind: Literal["text_delta"] = "text_delta"


@dataclass
class ToolUse:
    """The model wants to call a tool.

    `id` is the provider-assigned identifier for the tool call — we need
    to pass it back unchanged in the tool result so the provider knows
    which call the result corresponds to. `input` is the parsed JSON
    arguments the model decided on.
    """

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    kind: Literal["tool_use"] = "tool_use"


@dataclass
class TurnEnd:
    """Emitted after the model's final `end_turn` with no more tool calls.

    Carries the final stop reason and any accounting the provider
    reports (input/output tokens) so we can update ChatSession.metadata.
    """

    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    kind: Literal["turn_end"] = "turn_end"


@dataclass
class ModelResolved:
    """The upstream named the concrete model that is answering.

    Only emitted when the provider says so — today that is a LiteLLM
    gateway's `x-litellm-model-name` response header. A gateway lane is a
    logical name, and the model behind it is whatever the lane points at
    right now, fallbacks included, so the request's own `model` field
    cannot tell the user what they are talking to.
    """

    name: str
    kind: Literal["model_resolved"] = "model_resolved"


@dataclass
class StreamError:
    """The provider or gateway returned an error mid-stream.

    We surface this to the view as an event rather than raising so the
    view can translate it into an SSE error frame and still finish the
    chat turn cleanly.
    """

    message: str
    provider_error: dict[str, Any] | None = None
    kind: Literal["error"] = "error"


# Union alias for type hints. Kept as `Any` at runtime to avoid importing
# typing.Union machinery in consumers.
Event = TextDelta | ToolUse | TurnEnd | ModelResolved | StreamError


class LLMAdapter(ABC):
    """Interface every LLM adapter implements.

    Adapters are constructed once (with config), then `stream()` is
    called once per conversation turn. Each call accepts the full
    message history and the tool catalog; the adapter takes care of
    translating to the provider's wire format and parsing the response
    stream back into `Event` objects.
    """

    @abstractmethod
    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
        effort: str | None = None,
        thinking: str | None = None,
    ) -> AsyncIterator[Event]:
        """Yield events as the model generates.

        `messages` and `tools` are in the target provider's native shape
        (for now, Anthropic's). Future non-Anthropic adapters translate
        on their side so the chat view stays provider-agnostic.

        `effort` and `thinking` come straight off the registry entry and
        are Anthropic-specific reasoning controls; adapters that have no
        equivalent ignore them.
        """
        ...
