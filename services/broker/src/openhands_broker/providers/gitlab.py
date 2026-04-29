"""GitLab API credential injector.

Injects the GitLab personal access token as PRIVATE-TOKEN header.
"""

from ..proxy import CredentialInjector
from ..config import settings


class GitLabInjector(CredentialInjector):
    """Inject GitLab token into proxied requests."""

    async def inject(
        self, method: str, url: str, headers: dict[str, str],
        body: bytes | None, conversation_id: str | None,
    ) -> tuple[dict[str, str], bytes | None]:
        if settings.gitlab_token:
            headers["PRIVATE-TOKEN"] = settings.gitlab_token
        return headers, body
