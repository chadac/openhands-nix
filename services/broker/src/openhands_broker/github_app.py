"""GitHub App authentication — JWT generation and installation token management.

Generates short-lived JWTs from the App's private key, then exchanges them
for installation access tokens via the GitHub API. Tokens are cached and
refreshed 10 minutes before expiry.
"""

import logging
import time

import httpx
import jwt

from .config import settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Cache installation tokens (they last 1 hour, we refresh at 50 min)
_token_cache: dict[int, tuple[str, float]] = {}
_TOKEN_TTL = 50 * 60


def _load_private_key() -> str | None:
    """Load the GitHub App private key from settings."""
    key = settings.github_app_private_key
    if not key:
        return None
    if not key.startswith("-----") and "/" in key:
        try:
            with open(key) as f:
                return f.read()
        except OSError as e:
            logger.error("Failed to read GitHub App private key from %s: %s", key, e)
            return None
    return key


def _generate_jwt() -> str | None:
    """Generate a JWT for GitHub App authentication."""
    private_key = _load_private_key()
    if not private_key or not settings.github_app_id:
        return None
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": settings.github_app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str | None:
    """Get an installation access token, using cache when possible."""
    cached = _token_cache.get(installation_id)
    if cached and time.time() < cached[1]:
        return cached[0]

    app_jwt = _generate_jwt()
    if not app_jwt:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
            token = resp.json()["token"]
            _token_cache[installation_id] = (token, time.time() + _TOKEN_TTL)
            return token
    except httpx.HTTPError as e:
        logger.error("Failed to get installation token for %d: %s", installation_id, e)
        return None


def is_app_configured() -> bool:
    """Check if GitHub App authentication is configured."""
    return bool(settings.github_app_id and settings.github_app_private_key)
