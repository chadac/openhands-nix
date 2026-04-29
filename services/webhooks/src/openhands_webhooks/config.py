"""Webhooks service configuration."""

import hmac

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic_settings import BaseSettings

from openhands_common.config import DatabaseSettings, RabbitMQSettings

_bearer_scheme = HTTPBearer(auto_error=False)


class WebhooksSettings(BaseSettings):
    """Settings specific to the webhooks service."""

    # OpenHands API
    openhands_api_url: str = "http://openhands.openhands.svc.cluster.local:3000"

    # Integration toggles
    gitlab_enabled: bool = True
    github_enabled: bool = False
    slack_enabled: bool = False
    jira_enabled: bool = False

    # Webhook secrets (signature validation)
    gitlab_webhook_secret: str = ""
    github_webhook_secret: str = ""
    slack_signing_secret: str = ""
    jira_webhook_secret: str = ""

    # Tokens for posting responses back to services
    gitlab_token: str = ""
    github_token: str = ""
    slack_bot_token: str = ""

    # Atlassian OAuth 2.0 client credentials
    atlassian_client_id: str = ""
    atlassian_client_secret: str = ""
    atlassian_cloud_id: str = ""
    jira_bot_account_id: str = ""

    # Shared API key for internal relay endpoints
    relay_api_key: str = ""

    # Trigger pattern
    mention_pattern: str = "@openhands"

    # Bot usernames to ignore
    ignore_usernames: str = ""

    # OpenHands conversation URL base
    openhands_url: str = ""

    # Default repo
    default_gitlab_repo: str = ""

    # Pipe-separated repo descriptions
    repo_descriptions: str = ""

    model_config = {"env_prefix": "", "case_sensitive": False}

    def get_repo_descriptions(self) -> dict[str, str]:
        if not self.repo_descriptions:
            return {}
        result = {}
        for entry in self.repo_descriptions.split("|"):
            entry = entry.strip()
            if "=" in entry:
                repo, desc = entry.split("=", 1)
                result[repo.strip()] = desc.strip()
        return result


settings = WebhooksSettings()
db_settings = DatabaseSettings()
mq_settings = RabbitMQSettings()


def require_relay_key(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    """FastAPI dependency that enforces Bearer token auth on internal endpoints."""
    key = settings.relay_api_key
    if not key:
        return
    if credentials is None or not hmac.compare_digest(credentials.credentials, key):
        raise HTTPException(status_code=401, detail="Invalid or missing relay API key")
