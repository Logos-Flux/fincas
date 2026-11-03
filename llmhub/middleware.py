"""Request-scoped actor for the llmhub audit backstop."""

from __future__ import annotations

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from .audit import reset_actor, set_actor


class LLMHubActorMiddleware:
    """Set the audit actor for the duration of one request, then reset it.

    Hybrid sync/async so adding it never forces the async chat stream
    through an extra thread hop.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self._async = iscoroutinefunction(get_response)
        if self._async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self._async:
            return self.__acall__(request)
        token = set_actor(getattr(request, "user", None))
        try:
            return self.get_response(request)
        finally:
            reset_actor(token)

    async def __acall__(self, request):
        token = set_actor(getattr(request, "user", None))
        try:
            return await self.get_response(request)
        finally:
            reset_actor(token)
