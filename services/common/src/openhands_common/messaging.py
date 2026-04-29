"""RabbitMQ messaging layer for inter-service communication.

Exchange topology:
  - ``openhands.events`` (topic exchange)
    - ``conversation.created``    — new conversation was created
    - ``conversation.message``    — message needs to be sent to a conversation
    - ``conversation.status``     — conversation status changed
    - ``conversation.pause``      — request to pause a conversation
    - ``conversation.resumed``    — sandbox was resumed

Each service binds queues with routing key patterns it cares about.
"""

import json
import logging
from typing import Any, Callable, Awaitable

import aio_pika
from aio_pika import ExchangeType, Message as AMQPMessage

from .config import RabbitMQSettings

logger = logging.getLogger(__name__)

EXCHANGE_NAME = "openhands.events"

_connection: aio_pika.abc.AbstractRobustConnection | None = None
_channel: aio_pika.abc.AbstractChannel | None = None
_exchange: aio_pika.abc.AbstractExchange | None = None


async def connect(settings: RabbitMQSettings | None = None) -> None:
    """Connect to RabbitMQ and declare the shared exchange."""
    global _connection, _channel, _exchange
    if settings is None:
        settings = RabbitMQSettings()
    _connection = await aio_pika.connect_robust(settings.url)
    _channel = await _connection.channel()
    _exchange = await _channel.declare_exchange(
        EXCHANGE_NAME, ExchangeType.TOPIC, durable=True
    )
    logger.info("Connected to RabbitMQ: %s", settings.rabbitmq_host)


async def disconnect() -> None:
    """Close the RabbitMQ connection."""
    global _connection, _channel, _exchange
    if _connection:
        await _connection.close()
    _connection = None
    _channel = None
    _exchange = None


async def publish(routing_key: str, body: dict[str, Any]) -> None:
    """Publish a JSON message to the shared exchange.

    Args:
        routing_key: Dot-separated routing key (e.g. ``conversation.created``).
        body: JSON-serializable message payload.
    """
    assert _exchange is not None, "Not connected to RabbitMQ. Call connect() first."
    message = AMQPMessage(
        body=json.dumps(body).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )
    await _exchange.publish(message, routing_key=routing_key)
    logger.debug("Published %s: %s", routing_key, body)


async def subscribe(
    queue_name: str,
    routing_keys: list[str],
    handler: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> None:
    """Bind a durable queue to the exchange and start consuming.

    Args:
        queue_name: Unique name for this consumer's queue.
        routing_keys: List of routing key patterns (e.g. ``conversation.*``).
        handler: Async callback receiving (routing_key, body_dict).
    """
    assert _channel is not None, "Not connected to RabbitMQ. Call connect() first."
    assert _exchange is not None, "Not connected to RabbitMQ. Call connect() first."

    queue = await _channel.declare_queue(queue_name, durable=True)
    for key in routing_keys:
        await queue.bind(_exchange, routing_key=key)

    async def _on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            try:
                body = json.loads(message.body)
                await handler(message.routing_key, body)
            except Exception:
                logger.exception(
                    "Error handling message (routing_key=%s)", message.routing_key
                )

    await queue.consume(_on_message)
    logger.info(
        "Subscribed queue %s to %s with keys %s",
        queue_name, EXCHANGE_NAME, routing_keys,
    )
