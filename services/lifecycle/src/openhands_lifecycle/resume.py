"""Handle conversation.message events — resume sandbox and deliver message."""

import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)


async def handle_conversation_message(routing_key: str, body: dict) -> None:
    """Process a conversation.message event from RabbitMQ.

    1. Queue the message via pending-messages API (survives sandbox restarts).
    2. Trigger sandbox resume/recreation so the message gets delivered.
    """
    conversation_id = body.get("conversation_id", "")
    sandbox_id = body.get("sandbox_id", "")
    message = body.get("message", "")

    if not conversation_id or not message:
        logger.warning("Ignoring malformed conversation.message: %s", body)
        return

    logger.info(
        "Processing conversation.message for %s (sandbox %s)",
        conversation_id, sandbox_id,
    )

    async with httpx.AsyncClient(timeout=30) as client:
        # Queue the message for deferred delivery
        try:
            resp = await client.post(
                f"{settings.openhands_api_url}/api/v1/conversations/{conversation_id}/pending-messages",
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": message}],
                    "run": True,
                },
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Queued pending message for conversation %s", conversation_id)
        except httpx.HTTPError as e:
            logger.error("Failed to queue pending message for %s: %s", conversation_id, e)
            return

        # Trigger sandbox resume/recreation
        if sandbox_id:
            try:
                resp = await client.post(
                    f"{settings.openhands_api_url}/api/v1/sandboxes/{sandbox_id}/resume",
                    json={},
                    timeout=60,
                )
                if resp.status_code == 404:
                    logger.warning(
                        "Sandbox %s not found and could not be recreated", sandbox_id,
                    )
                else:
                    resp.raise_for_status()
                    logger.info("Triggered resume for sandbox %s", sandbox_id)
            except httpx.HTTPError as e:
                logger.warning(
                    "Failed to resume sandbox %s (message still queued): %s",
                    sandbox_id, e,
                )
