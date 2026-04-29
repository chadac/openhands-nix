"""Tests for the RabbitMQ messaging layer.

These tests use mocked aio_pika connections to verify message publishing
and subscription behavior without a real RabbitMQ instance.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands_common import messaging


@pytest.fixture
def mock_channel():
    """Create a mock aio_pika channel with exchange."""
    channel = AsyncMock()
    exchange = AsyncMock()
    channel.declare_exchange = AsyncMock(return_value=exchange)
    return channel, exchange


@pytest.mark.asyncio
async def test_publish_sends_to_exchange(mock_channel):
    """Publishing a message sends it to the exchange with correct routing key."""
    channel, exchange = mock_channel

    # Set up the module state
    messaging._channel = channel
    messaging._exchange = exchange

    body = {"conversation_id": "conv-123", "status": "RUNNING"}
    await messaging.publish("conversation.status", body)

    exchange.publish.assert_called_once()
    call_args = exchange.publish.call_args
    msg = call_args[0][0]  # First positional arg is the Message
    assert call_args[1]["routing_key"] == "conversation.status"
    # Verify the message body is JSON-encoded
    assert json.loads(msg.body) == body


@pytest.mark.asyncio
async def test_subscribe_creates_queue_and_binds(mock_channel):
    """Subscribing creates a queue, binds it, and starts consuming."""
    channel, exchange = mock_channel
    queue = AsyncMock()
    channel.declare_queue = AsyncMock(return_value=queue)

    messaging._channel = channel
    messaging._exchange = exchange

    handler = AsyncMock()
    await messaging.subscribe(
        queue_name="test-queue",
        routing_keys=["conversation.*", "conversation.pause"],
        handler=handler,
    )

    # Queue was declared
    channel.declare_queue.assert_called_once_with("test-queue", durable=True)

    # Queue was bound to exchange for each routing key
    assert queue.bind.call_count == 2
    bind_keys = [call.kwargs.get("routing_key") or call.args[1] for call in queue.bind.call_args_list]
    assert "conversation.*" in bind_keys
    assert "conversation.pause" in bind_keys

    # Consumer was started
    queue.consume.assert_called_once()


@pytest.mark.asyncio
async def test_publish_without_connection_raises():
    """Publishing without a connection raises an error."""
    messaging._exchange = None
    with pytest.raises((NotImplementedError, AssertionError, AttributeError)):
        await messaging.publish("test.key", {"data": "value"})
