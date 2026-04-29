"""OpenHands API client — create conversations and send messages.

Message delivery for paused sandboxes is handled via RabbitMQ
(``conversation.message`` events) instead of the old in-memory
resume queue.
"""

import asyncio
import logging
from uuid import UUID

import httpx

from openhands_common import messaging

from .config import settings

logger = logging.getLogger(__name__)

_BATCH_CHUNK_SIZE = 20


def _normalize_uuid(conversation_id: str) -> str:
    """Ensure conversation_id is in standard hyphenated UUID format."""
    try:
        return str(UUID(conversation_id))
    except (ValueError, AttributeError):
        return conversation_id


async def create_conversation(
    initial_message: str,
    repository: str | None = None,
    git_provider: str = "gitlab",
    title: str | None = None,
) -> dict | None:
    """Create an OpenHands conversation via the V1 API.

    Returns a dict with ``conversation_id`` on success, None on failure.
    Polls the start task until the sandbox is READY.
    """
    payload: dict = {
        "initial_message": {
            "content": [{"type": "text", "text": initial_message}],
        },
        "trigger": "resolver",
        "git_provider": git_provider,
    }
    if repository:
        payload["selected_repository"] = repository
    if title:
        payload["title"] = title

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.openhands_api_url}/api/v1/app-conversations",
                json=payload,
            )
            resp.raise_for_status()
            task = resp.json()
            task_id = task.get("id", "")
            logger.info("App-conversation start task created: %s", task_id)

            conv_id = await _poll_start_task(client, task_id)
            if conv_id:
                task["conversation_id"] = conv_id
                logger.info("Resolved conversation ID: %s (from task %s)", conv_id, task_id)
                return task
            else:
                logger.error("Start task %s failed -- sandbox did not start in time", task_id)
                return None
    except httpx.HTTPError as e:
        logger.error("Failed to create OpenHands conversation: %s", e)
        return None


async def _poll_start_task(
    client: httpx.AsyncClient,
    task_id: str,
    max_attempts: int = 310,
    interval: float = 2.0,
) -> str | None:
    """Poll a start task until it reaches READY and returns the conversation ID."""
    terminal_statuses = {"READY", "ERROR"}

    for attempt in range(max_attempts):
        await asyncio.sleep(interval)
        try:
            resp = await client.get(
                f"{settings.openhands_api_url}/api/v1/app-conversations/start-tasks",
                params={"ids": task_id},
            )
            resp.raise_for_status()
            tasks = resp.json()

            if isinstance(tasks, list) and tasks:
                task = tasks[0]
            elif isinstance(tasks, dict):
                items = tasks.get("items", tasks.get("results", []))
                task = items[0] if items else None
            else:
                task = None

            if not task:
                continue

            status = task.get("status", "")
            conv_id = task.get("app_conversation_id")

            if conv_id:
                return conv_id

            if status in terminal_statuses:
                detail = task.get("detail", "")
                logger.warning("Start task %s reached %s: %s", task_id, status, detail)
                return None

        except httpx.HTTPError as e:
            logger.debug("Poll attempt %d failed: %s", attempt + 1, e)

    logger.warning("Start task %s polling timed out after %d attempts", task_id, max_attempts)
    return None


def _map_execution_status(exec_status: str) -> str:
    """Map V1 execution_status to internal status strings."""
    mapping = {
        "running": "RUNNING",
        "awaiting_user_input": "AWAITING_USER_INPUT",
        "finished": "FINISHED",
        "stopped": "STOPPED",
        "error": "ERROR",
        "paused": "STOPPED",
    }
    return mapping.get(exec_status.lower(), exec_status.upper())


async def send_message(conversation_id: str, message: str) -> bool:
    """Send a follow-up message to an existing conversation.

    For running sandboxes, sends directly to the sandbox agent-server.
    For paused/unreachable sandboxes, publishes a ``conversation.message``
    event to RabbitMQ for deferred delivery.

    Returns False only if the conversation no longer exists.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            conv = await _get_conversation(client, conversation_id)
            if not conv:
                logger.warning("Conversation %s not found", conversation_id)
                return False

            sandbox_id = conv.get("sandbox_id")
            api_key = conv.get("session_api_key")

            if not sandbox_id:
                logger.warning("Conversation %s has no sandbox_id", conversation_id)
                return False

            sandbox_url = f"http://oh-sandbox-{sandbox_id}.openhands.svc.cluster.local:8000"

            if not api_key:
                # Sandbox exists but no API key — likely paused. Resume + queue via MQ.
                logger.info("Conversation %s paused, queuing message via RabbitMQ", conversation_id)
                await messaging.publish("conversation.message", {
                    "conversation_id": conversation_id,
                    "sandbox_id": sandbox_id,
                    "message": message,
                    "action": "resume_and_send",
                })
                return True

            # Try sending directly
            try:
                resp = await client.post(
                    f"{sandbox_url}/api/conversations/{conversation_id}/events",
                    headers={"X-Session-API-Key": api_key},
                    json={
                        "role": "user",
                        "content": [{"type": "text", "text": message}],
                        "run": True,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                logger.info("Sent message to conversation %s via sandbox", conversation_id)
                return True
            except httpx.TimeoutException:
                logger.warning("Timeout sending to %s -- message may have been received", conversation_id)
                return True
            except httpx.ConnectError:
                # Sandbox unreachable — queue via MQ for deferred delivery
                logger.info("Sandbox unreachable for %s, queuing via RabbitMQ", conversation_id)
                await messaging.publish("conversation.message", {
                    "conversation_id": conversation_id,
                    "sandbox_id": sandbox_id,
                    "message": message,
                    "action": "resume_and_send",
                })
                return True
    except httpx.HTTPError as e:
        logger.error("Failed to send message to conversation %s: %s", conversation_id, e)
        return False


async def _get_conversation(client: httpx.AsyncClient, conversation_id: str) -> dict | None:
    """Get a single conversation by ID via the V1 batch-get endpoint."""
    try:
        resp = await client.get(
            f"{settings.openhands_api_url}/api/v1/app-conversations",
            params={"ids": conversation_id},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data and data[0] is not None:
            return data[0]
    except httpx.HTTPError as e:
        logger.debug("Failed to get conversation %s: %s", conversation_id, e)
    return None


async def get_conversation_status(conversation_id: str) -> str | None:
    """Get the status of a single conversation."""
    result = await get_conversation_statuses([conversation_id])
    return result.get(conversation_id)


async def get_conversation_statuses(conversation_ids: list[str]) -> dict[str, str]:
    """Batch-get statuses for multiple conversations."""
    if not conversation_ids:
        return {}

    result: dict[str, str] = {}
    chunks = [
        conversation_ids[i:i + _BATCH_CHUNK_SIZE]
        for i in range(0, len(conversation_ids), _BATCH_CHUNK_SIZE)
    ]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for chunk in chunks:
                try:
                    resp = await client.get(
                        f"{settings.openhands_api_url}/api/v1/app-conversations",
                        params=[("ids", cid) for cid in chunk],
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if not isinstance(data, list):
                        continue

                    for conv in data:
                        if not conv or not isinstance(conv, dict):
                            continue
                        cid = conv.get("conversation_id") or conv.get("id", "")
                        if not cid:
                            continue
                        exec_status = conv.get("execution_status")
                        sandbox_status = (conv.get("sandbox_status") or "").upper()
                        if exec_status:
                            result[cid] = _map_execution_status(exec_status)
                        elif sandbox_status == "PAUSED":
                            result[cid] = "CLOSED"
                        elif sandbox_status in ("MISSING", "ERROR"):
                            result[cid] = "ERROR"
                        else:
                            result[cid] = "RUNNING"
                except httpx.HTTPError as e:
                    logger.error("Failed to batch-get statuses (chunk of %d): %s", len(chunk), e)
    except httpx.HTTPError as e:
        logger.error("Failed to create HTTP client for batch status: %s", e)
    return result


async def list_conversations() -> list[dict]:
    """List all conversations from the OpenHands API."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{settings.openhands_api_url}/api/v1/app-conversations/search",
            params={"limit": 100},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        for item in items:
            if "conversation_id" not in item and "id" in item:
                item["conversation_id"] = item["id"]
        return items


async def stop_conversation(conversation_id: str) -> bool:
    """Pause a conversation's sandbox."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            conv = await _get_conversation(client, conversation_id)
            if not conv:
                return False

            sandbox_id = conv.get("sandbox_id")
            if not sandbox_id:
                return True  # Already paused/stopped

            resp = await client.post(
                f"{settings.openhands_api_url}/api/v1/sandboxes/{sandbox_id}/pause",
                json={},
            )
            resp.raise_for_status()
            logger.info("Paused sandbox %s for conversation %s", sandbox_id, conversation_id)
            return True
    except httpx.HTTPError as e:
        logger.error("Failed to pause conversation %s: %s", conversation_id, e)
        return False


async def set_conversation_title(conversation_id: str, title: str) -> bool:
    """Set the title of a conversation."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{settings.openhands_api_url}/api/conversations/{conversation_id}",
                json={"title": title},
            )
            resp.raise_for_status()
            logger.info("Set title for conversation %s to %r", conversation_id, title)
            return True
    except httpx.HTTPError as e:
        logger.error("Failed to set title for conversation %s: %s", conversation_id, e)
        return False


async def resolve_sandbox_to_conversation(sandbox_or_conv_id: str) -> str | None:
    """Resolve a sandbox ID to the real conversation ID."""
    convs = await list_conversations()
    for conv in convs:
        cid = conv.get("conversation_id") or conv.get("id", "")
        sid = conv.get("sandbox_id", "")
        if cid == sandbox_or_conv_id or sid == sandbox_or_conv_id:
            return cid
    logger.warning("Could not resolve sandbox/conversation ID: %s", sandbox_or_conv_id)
    return None


def conversation_url(conversation_id: str) -> str:
    """Build a user-facing URL for a conversation."""
    base = settings.openhands_url.rstrip("/")
    return f"{base}/conversations/{conversation_id}"
