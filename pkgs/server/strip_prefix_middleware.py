"""ASGI middleware to strip a path prefix from incoming requests.

AWS ALB does not support path rewriting, so when routing
/sandbox/<id>/health → the agent-server pod, the pod receives the full
path /sandbox/<id>/health. This middleware strips the prefix so the
FastAPI routes see /health.

Activated by setting the OH_WEB_URL environment variable to include a
path component (e.g. https://host/sandbox/abc123). The prefix is
extracted from that URL's path.
"""

import os
from urllib.parse import urlparse

from starlette.types import ASGIApp, Receive, Scope, Send


class StripPrefixMiddleware:
    """Strip a URL path prefix from incoming ASGI requests.

    If the request path starts with the configured prefix, the prefix is
    removed before the inner app sees it. Requests that don't match the
    prefix are passed through unchanged.
    """

    def __init__(self, app: ASGIApp, prefix: str = ""):
        self.app = app
        # Normalise: no trailing slash, must start with /
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and self.prefix:
            path: str = scope.get("path", "")
            if path.startswith(self.prefix + "/") or path == self.prefix:
                scope = dict(scope)
                scope["path"] = path[len(self.prefix):] or "/"
                # root_path is already set by FastAPI via OH_WEB_URL / _get_root_path()
        await self.app(scope, receive, send)


def get_strip_prefix() -> str:
    """Derive the prefix to strip from OH_WEB_URL (if set)."""
    web_url = os.getenv("OH_WEB_URL", "")
    if not web_url:
        return ""
    try:
        parsed = urlparse(web_url)
        return parsed.path.rstrip("/")
    except Exception:
        return ""
