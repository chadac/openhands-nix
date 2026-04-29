"""Cleanup routines for idle conversations and orphaned sandbox resources.

Runs as a background loop, publishing ``conversation.pause`` events
via RabbitMQ when conversations need to be stopped.
"""

import logging
from datetime import datetime, timezone

import httpx

from openhands_common import messaging

from .config import settings
from .k8s import SANDBOX_PREFIX, get_k8s_clients, get_sandbox_pod_start_time, delete_sandbox_resources, cleanup_old_pvcs as k8s_cleanup_old_pvcs

logger = logging.getLogger(__name__)


def _parse_utc(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


async def _list_conversations() -> list[dict]:
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


async def _stop_conversation(conversation_id: str) -> bool:
    """Pause a conversation's sandbox."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{settings.openhands_api_url}/api/v1/app-conversations",
                params={"ids": conversation_id},
            )
            resp.raise_for_status()
            data = resp.json()
            conv = data[0] if isinstance(data, list) and data and data[0] else None
            if not conv:
                return False

            sandbox_id = conv.get("sandbox_id")
            if not sandbox_id:
                return True

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


async def _get_sandbox_execution_status(
    sandbox_id: str, conversation_id: str, session_api_key: str | None = None,
) -> str | None:
    """Query the sandbox agent-server directly for real-time execution status."""
    url = f"http://oh-sandbox-{sandbox_id}.openhands.svc.cluster.local:8000"
    try:
        headers = {}
        if session_api_key:
            headers["X-Session-API-Key"] = session_api_key
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{url}/api/conversations",
                params={"ids": conversation_id},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data if isinstance(data, list) else data.get("items", [])
            for item in items:
                if isinstance(item, dict):
                    status = item.get("execution_status", "")
                    if status:
                        return status.upper()
    except Exception:
        pass
    return None


def _get_sandbox_pod_start_time(sandbox_id: str) -> datetime | None:
    """Get the start time of the sandbox's Pod."""
    return get_sandbox_pod_start_time(sandbox_id, settings.sandbox_namespace)


def _list_k8s_sandbox_ids() -> set[str]:
    """Collect sandbox IDs from all K8s resource types."""
    core_v1, batch_v1, networking_v1 = get_k8s_clients()
    if core_v1 is None:
        return set()

    ns = settings.sandbox_namespace
    ids: set[str] = set()

    for svc in core_v1.list_namespaced_service(ns).items:
        if svc.metadata.name.startswith(SANDBOX_PREFIX):
            ids.add(svc.metadata.name[len(SANDBOX_PREFIX):])

    for job in batch_v1.list_namespaced_job(ns).items:
        if job.metadata.name.startswith(SANDBOX_PREFIX):
            ids.add(job.metadata.name[len(SANDBOX_PREFIX):])

    for ing in networking_v1.list_namespaced_ingress(ns).items:
        if ing.metadata.name.startswith(SANDBOX_PREFIX):
            ids.add(ing.metadata.name[len(SANDBOX_PREFIX):])

    return ids


def _delete_sandbox_resources(sandbox_id: str, namespace: str) -> None:
    """Delete orphaned sandbox compute resources."""
    delete_sandbox_resources(sandbox_id, namespace)


async def cleanup_idle_conversations() -> None:
    """Pause conversations idle longer than the configured timeout."""
    timeout_minutes = settings.conversation_idle_timeout_minutes
    if timeout_minutes <= 0:
        return

    try:
        convs = await _list_conversations()
        now = datetime.now(timezone.utc)

        for conv in convs:
            cid = conv.get("conversation_id") or conv.get("id", "")
            if not cid:
                continue

            exec_status = (conv.get("execution_status") or "").upper()
            sandbox_status = (conv.get("sandbox_status") or "").upper()
            sandbox_id = conv.get("sandbox_id", "")

            if exec_status in ("STOPPED",) or sandbox_status in ("PAUSED", "MISSING"):
                continue

            if exec_status not in ("AWAITING_USER_INPUT", "FINISHED"):
                continue

            # Check pod age
            pod_started = _get_sandbox_pod_start_time(sandbox_id) if sandbox_id else None
            if pod_started:
                pod_age_minutes = (now - pod_started).total_seconds() / 60
                if pod_age_minutes < settings.sandbox_min_pod_age_minutes:
                    continue

            # Collect timestamps
            timestamps: list[datetime] = []
            updated_at = _parse_utc(conv.get("updated_at", ""))
            if updated_at:
                timestamps.append(updated_at)
            created_at = _parse_utc(conv.get("created_at", ""))
            if created_at:
                timestamps.append(created_at)
            if pod_started:
                timestamps.append(pod_started)

            if not timestamps:
                continue

            last_activity = max(timestamps)
            idle_minutes = (now - last_activity).total_seconds() / 60

            if idle_minutes < timeout_minutes:
                continue

            # Safety check: ask sandbox directly
            if sandbox_id:
                session_key = conv.get("session_api_key", "")
                live_status = await _get_sandbox_execution_status(sandbox_id, cid, session_key)
                if live_status is None:
                    continue
                if live_status not in ("AWAITING_USER_INPUT", "FINISHED", "STOPPED", "PAUSED"):
                    continue

            logger.info(
                "Pausing idle conversation %s (idle=%.0fm, timeout=%dm)",
                cid, idle_minutes, timeout_minutes,
            )
            await _stop_conversation(cid)
            await messaging.publish("conversation.pause", {
                "conversation_id": cid,
                "reason": "idle_timeout",
            })

    except Exception:
        logger.exception("Error during idle conversation cleanup")


async def cleanup_orphaned_sandboxes() -> None:
    """Delete orphaned sandbox compute resources."""
    try:
        k8s_sandbox_ids = _list_k8s_sandbox_ids()
        if not k8s_sandbox_ids:
            return

        convs = await _list_conversations()
        sandbox_conv_map: dict[str, dict] = {}
        for conv in convs:
            sid = conv.get("sandbox_id", "")
            if sid:
                sandbox_conv_map[sid] = conv

        now = datetime.now(timezone.utc)
        cleanup_ids: set[str] = set()

        for sandbox_id in k8s_sandbox_ids:
            pod_started = _get_sandbox_pod_start_time(sandbox_id)
            if pod_started:
                pod_age_minutes = (now - pod_started).total_seconds() / 60
                if pod_age_minutes < settings.sandbox_min_pod_age_minutes:
                    continue

            conv = sandbox_conv_map.get(sandbox_id)
            if conv is None:
                cleanup_ids.add(sandbox_id)
                continue

            sandbox_status = (conv.get("sandbox_status") or "").upper()
            exec_status = (conv.get("execution_status") or "").lower()

            if sandbox_status in ("MISSING", "ERROR", "STOPPED"):
                cleanup_ids.add(sandbox_id)
            elif exec_status in ("stopped", "error"):
                cleanup_ids.add(sandbox_id)

        if not cleanup_ids:
            return

        logger.info("Found %d sandbox resources to clean up", len(cleanup_ids))
        for sandbox_id in cleanup_ids:
            _delete_sandbox_resources(sandbox_id, settings.sandbox_namespace)

    except Exception:
        logger.exception("Error during orphaned sandbox cleanup")


async def cleanup_old_pvcs() -> None:
    """Delete sandbox workspace PVCs older than the configured max age."""
    if settings.sandbox_pvc_max_age_days <= 0:
        return
    k8s_cleanup_old_pvcs(settings.sandbox_namespace, settings.sandbox_pvc_max_age_days)
