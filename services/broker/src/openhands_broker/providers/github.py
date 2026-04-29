"""GitHub API credential injector.

Injects the GitHub token as a Bearer token in the Authorization header.
Future: per-conversation GitHub App installation tokens.
"""

from ..proxy import CredentialInjector
from ..config import settings


class GitHubInjector(CredentialInjector):
    """Inject GitHub token into proxied requests."""

    async def inject(
        self, method: str, url: str, headers: dict[str, str],
        body: bytes | None, conversation_id: str | None,
    ) -> tuple[dict[str, str], bytes | None]:
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
            headers["Accept"] = "application/vnd.github+json"
        return headers, body
