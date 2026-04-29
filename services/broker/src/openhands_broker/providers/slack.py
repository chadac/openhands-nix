"""Slack API credential injector.

Injects the Slack bot token as a Bearer token.
"""

from ..proxy import CredentialInjector
from ..config import settings


class SlackInjector(CredentialInjector):
    """Inject Slack bot token into proxied requests."""

    async def inject(
        self, method: str, url: str, headers: dict[str, str],
        body: bytes | None, conversation_id: str | None,
    ) -> tuple[dict[str, str], bytes | None]:
        if settings.slack_bot_token:
            headers["Authorization"] = f"Bearer {settings.slack_bot_token}"
        return headers, body
