"""FastAPI HTTP adapter — NOT YET IMPLEMENTED.

This module is a placeholder for the future web-frontend-facing HTTP API.
It will expose the same core.* operations as the MCP adapter does, but over
HTTP, with Cloudflare Access handling edge auth and the same shared API key
used to talk to Django.

Planned structure when we turn it on:

    import fastapi
    from .core import ERPClient, products as products_core

    app = fastapi.FastAPI(title="erp-assist")

    @app.get("/api/products")
    async def list_products(search: str | None = None, limit: int = 20):
        async with ERPClient.from_env() as client:
            return await products_core.list_products(client, search=search, limit=limit)

Run with: uvicorn erp_assist.http_adapter:app --host 127.0.0.1 --port 8765

Auth sketch: a FastAPI dependency validates the Cf-Access-Jwt-Assertion header
issued by Cloudflare Access for each request. The JWT identifies the human user
(email + access policy). The adapter then uses GOLDEN_ERP_API_KEY server-side
to talk to Django. Later we can replace the shared key with per-user Django
tokens for proper audit attribution.
"""
