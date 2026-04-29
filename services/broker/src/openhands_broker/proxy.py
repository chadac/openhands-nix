"""Transparent reverse proxy with credential injection.

Receives requests from sandbox agents, injects the appropriate
credentials based on the target provider, and forwards to the
upstream API. The agent sees standard API responses as if it
were calling the real API directly.

Design for future inspection: the proxy pipeline has hooks for
request/response inspection to enforce fine-grained permissions
(e.g. limiting which repos an agent can push to).
"""

import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

from fastapi import Request, Response

import httpx

logger = logging.getLogger(__name__)

# Headers that should not be forwarded to the upstream API
_HOP_BY_HOP_HEADERS = frozenset({
    "host", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization",
    "proxy-authenticate", "authorization",  # We inject our own auth
})


@dataclass
class ProxyResult:
    """Result of a proxied request."""
    status_code: int
    headers: dict[str, str]
    body: bytes


class CredentialInjector:
    """Base class for provider-specific credential injection.

    Subclasses implement ``inject()`` to modify the outbound request
    headers/body before forwarding to the upstream API.
    """

    async def inject(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        conversation_id: str | None,
    ) -> tuple[dict[str, str], bytes | None]:
        """Inject credentials into the request.

        Args:
            method: HTTP method.
            url: Full upstream URL.
            headers: Mutable headers dict (add auth headers here).
            body: Request body (may be None for GET/DELETE).
            conversation_id: The calling conversation ID (for scoped credentials).

        Returns:
            (modified_headers, modified_body)
        """
        raise NotImplementedError

    async def inspect_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        conversation_id: str | None,
    ) -> None:
        """Optional hook to inspect/validate the request before forwarding.

        Raise HTTPException to block the request.
        Override in subclasses for permission enforcement.
        """
        pass

    async def inspect_response(
        self,
        method: str,
        url: str,
        result: ProxyResult,
        conversation_id: str | None,
    ) -> ProxyResult:
        """Optional hook to inspect/modify the response before returning.

        Override in subclasses for response filtering.
        """
        return result


def _extract_headers(request: Request) -> dict[str, str]:
    """Extract forwarded-safe headers from the inbound request."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS
    }


async def _forward_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
) -> ProxyResult:
    """Send the request to the upstream API and return the result."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=body,
        )
        resp_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
        }
        return ProxyResult(
            status_code=resp.status_code,
            headers=resp_headers,
            body=resp.content,
        )


async def proxy_request(
    request: Request,
    upstream_base_url: str,
    path: str,
    injector: CredentialInjector,
    conversation_id: str | None = None,
) -> Response:
    """Forward a request to an upstream API with credential injection.

    This is the core proxy function. It:
    1. Calls ``injector.inspect_request()`` for permission checks
    2. Calls ``injector.inject()`` to add credentials
    3. Forwards the request to ``upstream_base_url/path``
    4. Calls ``injector.inspect_response()`` for response filtering
    5. Returns the upstream response
    """
    method = request.method
    upstream_url = f"{upstream_base_url.rstrip('/')}/{path}"

    # Append query string if present
    if request.query_params:
        upstream_url += f"?{request.query_params}"

    # Read the request body
    body = await request.body() or None

    # Extract safe headers from the inbound request
    headers = _extract_headers(request)

    # 1. Permission check
    await injector.inspect_request(method, upstream_url, headers, body, conversation_id)

    # 2. Credential injection
    headers, body = await injector.inject(method, upstream_url, headers, body, conversation_id)

    # 3. Forward to upstream
    result = await _forward_request(method, upstream_url, headers, body)

    # 4. Response filtering
    result = await injector.inspect_response(method, upstream_url, result, conversation_id)

    # 5. Return upstream response
    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=result.headers,
    )
