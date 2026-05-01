"""Broker service configuration."""

from pydantic_settings import BaseSettings

from openhands_common.config import DatabaseSettings


class BrokerSettings(BaseSettings):
    """Settings specific to the broker service."""

    # Upstream API base URLs (transparent proxy targets)
    github_api_url: str = "https://api.github.com"
    gitlab_api_url: str = "https://gitlab.com/api/v4"
    jira_api_url: str = ""  # Set via atlassian_cloud_id
    slack_api_url: str = "https://slack.com/api"

    # Global credentials (injected into proxied requests)
    github_token: str = ""  # PAT fallback (used if github_app_id is empty)
    gitlab_token: str = ""
    slack_bot_token: str = ""

    # GitHub App authentication (preferred over PAT)
    github_app_id: str = ""
    github_app_private_key: str = ""  # PEM content or path to .pem file
    github_app_installation_id: int = 0  # Default installation ID

    # Atlassian OAuth 2.0
    atlassian_client_id: str = ""
    atlassian_client_secret: str = ""
    atlassian_cloud_id: str = ""

    # K8s namespace for sandbox SA validation
    sandbox_namespace: str = "openhands"

    # Token audience for SA token validation
    token_audience: str = "openhands-broker"

    model_config = {"env_prefix": "", "case_sensitive": False}

    @property
    def jira_base_url(self) -> str:
        if self.atlassian_cloud_id:
            return f"https://api.atlassian.com/ex/jira/{self.atlassian_cloud_id}"
        return self.jira_api_url


settings = BrokerSettings()
db_settings = DatabaseSettings()
