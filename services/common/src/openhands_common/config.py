"""Shared configuration for all OpenHands services."""

from pydantic_settings import BaseSettings


class DatabaseSettings(BaseSettings):
    """PostgreSQL connection settings."""

    db_host: str = "postgres.openhands.svc.cluster.local"
    db_port: int = 5432
    db_name: str = "openhands"
    db_user: str = "openhands"
    db_password: str = ""

    @property
    def dsn(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    model_config = {"env_prefix": "", "case_sensitive": False}


class RabbitMQSettings(BaseSettings):
    """RabbitMQ connection settings."""

    rabbitmq_host: str = "rabbitmq.openhands.svc.cluster.local"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "openhands"
    rabbitmq_password: str = ""
    rabbitmq_vhost: str = "/"

    @property
    def url(self) -> str:
        return f"amqp://{self.rabbitmq_user}:{self.rabbitmq_password}@{self.rabbitmq_host}:{self.rabbitmq_port}/{self.rabbitmq_vhost}"

    model_config = {"env_prefix": "", "case_sensitive": False}
