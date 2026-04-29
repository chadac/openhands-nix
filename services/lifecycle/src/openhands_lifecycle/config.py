"""Lifecycle service configuration."""

from pydantic_settings import BaseSettings

from openhands_common.config import DatabaseSettings, RabbitMQSettings


class LifecycleSettings(BaseSettings):
    """Settings specific to the lifecycle manager."""

    # OpenHands API
    openhands_api_url: str = "http://openhands.openhands.svc.cluster.local:3000"

    # Auto-close: stop conversations idle for this many minutes (0 = disabled)
    conversation_idle_timeout_minutes: int = 60

    # Minimum pod age before considering it for cleanup
    sandbox_min_pod_age_minutes: int = 40

    # Delete workspace PVCs older than this many days (0 = disabled)
    sandbox_pvc_max_age_days: int = 7

    # K8s namespace for sandboxes
    sandbox_namespace: str = "openhands"

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = LifecycleSettings()
db_settings = DatabaseSettings()
mq_settings = RabbitMQSettings()
