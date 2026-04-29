"""Jira API credential injector.

Uses Atlassian OAuth 2.0 client credentials for token management.
Injects Bearer token into proxied requests.
"""

import logging
import time

import httpx

from ..proxy import CredentialInjector
from ..config import settings

logger = logging.getLogger(__name__)

TOKEN_URL = "https://auth.atlassian.com/oauth/token"

_access_token: str | None = None
_token_expires_at: float = 0


async def _get_access_token() -> str | None:
    """Get a valid Atlassian OAuth access token, refreshing if needed."""
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    if not settings.atlassian_client_id or not settings.atlassian_client_secret:
        logger.warning("Atlassian OAuth credentials not configured")
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                TOKEN_URL,
                json={
                    "grant_type": "client_credentials",
                    "client_id": settings.atlassian_client_id,
                    "client_secret": settings.atlassian_client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            _access_token = data["access_token"]
            _token_expires_at = time.time() + data.get("expires_in", 3600)
            logger.info("Refreshed Atlassian OAuth access token")
            return _access_token
    except httpx.HTTPError as e:
        logger.error("Failed to get Atlassian access token: %s", e)
        return None


class JiraInjector(CredentialInjector):
    """Inject Atlassian OAuth token into proxied requests."""

    async def inject(
        self, method: str, url: str, headers: dict[str, str],
        body: bytes | None, conversation_id: str | None,
    ) -> tuple[dict[str, str], bytes | None]:
        token = await _get_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["Accept"] = "application/json"
        return headers, body
