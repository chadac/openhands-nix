"""GitHub API credential injector.

Uses GitHub App installation tokens when configured, falling back to
a static PAT. Installation tokens are cached and auto-refreshed.
"""

from ..proxy import CredentialInjector
from ..config import settings
from ..github_app import get_installation_token, is_app_configured


class GitHubInjector(CredentialInjector):
    """Inject GitHub credentials into proxied requests."""

    async def inject(
        self, method: str, url: str, headers: dict[str, str],
        body: bytes | None, conversation_id: str | None,
    ) -> tuple[dict[str, str], bytes | None]:
        headers["Accept"] = "application/vnd.github+json"

        if is_app_configured() and settings.github_app_installation_id:
            token = await get_installation_token(settings.github_app_installation_id)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                return headers, body

        # Fallback to static PAT
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"

        return headers, body
