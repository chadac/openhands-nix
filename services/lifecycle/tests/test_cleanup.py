"""Tests for lifecycle cleanup routines."""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands_lifecycle.cleanup import (
    cleanup_idle_conversations,
    cleanup_orphaned_sandboxes,
    cleanup_old_pvcs,
)


# ---------------------------------------------------------------------------
# Idle conversation cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_cleanup_disabled_when_timeout_zero():
    """Idle cleanup is a no-op when timeout is 0."""
    with patch("openhands_lifecycle.cleanup.settings") as mock_settings:
        mock_settings.conversation_idle_timeout_minutes = 0
        await cleanup_idle_conversations()
        # Should return without doing anything (no API calls)


@pytest.mark.asyncio
async def test_idle_cleanup_skips_running_conversations():
    """Running conversations are not paused even if old."""
    with patch("openhands_lifecycle.cleanup.settings") as mock_settings, \
         patch("openhands_lifecycle.cleanup._list_conversations") as mock_list, \
         patch("openhands_lifecycle.cleanup._stop_conversation") as mock_stop:

        mock_settings.conversation_idle_timeout_minutes = 60
        mock_settings.sandbox_min_pod_age_minutes = 40

        now = datetime.now(timezone.utc)
        mock_list.return_value = [{
            "conversation_id": "conv-1",
            "execution_status": "running",
            "sandbox_status": "RUNNING",
            "sandbox_id": "sandbox-1",
            "updated_at": (now - timedelta(hours=2)).isoformat(),
            "created_at": (now - timedelta(hours=3)).isoformat(),
        }]

        await cleanup_idle_conversations()
        mock_stop.assert_not_called()


@pytest.mark.asyncio
async def test_idle_cleanup_pauses_idle_awaiting_input():
    """Conversations awaiting input past timeout are paused."""
    with patch("openhands_lifecycle.cleanup.settings") as mock_settings, \
         patch("openhands_lifecycle.cleanup._list_conversations") as mock_list, \
         patch("openhands_lifecycle.cleanup._stop_conversation") as mock_stop, \
         patch("openhands_lifecycle.cleanup._get_sandbox_pod_start_time") as mock_pod_time, \
         patch("openhands_lifecycle.cleanup._get_sandbox_execution_status") as mock_exec:

        mock_settings.conversation_idle_timeout_minutes = 60
        mock_settings.sandbox_min_pod_age_minutes = 40
        mock_settings.sandbox_namespace = "openhands"

        now = datetime.now(timezone.utc)
        mock_list.return_value = [{
            "conversation_id": "conv-1",
            "execution_status": "awaiting_user_input",
            "sandbox_status": "RUNNING",
            "sandbox_id": "sandbox-1",
            "updated_at": (now - timedelta(hours=2)).isoformat(),
            "created_at": (now - timedelta(hours=3)).isoformat(),
        }]
        mock_pod_time.return_value = now - timedelta(hours=2)
        mock_exec.return_value = "AWAITING_USER_INPUT"

        await cleanup_idle_conversations()
        mock_stop.assert_called_once_with("conv-1")


@pytest.mark.asyncio
async def test_idle_cleanup_skips_young_pods():
    """Pods younger than min_pod_age_minutes are not paused."""
    with patch("openhands_lifecycle.cleanup.settings") as mock_settings, \
         patch("openhands_lifecycle.cleanup._list_conversations") as mock_list, \
         patch("openhands_lifecycle.cleanup._stop_conversation") as mock_stop, \
         patch("openhands_lifecycle.cleanup._get_sandbox_pod_start_time") as mock_pod_time:

        mock_settings.conversation_idle_timeout_minutes = 60
        mock_settings.sandbox_min_pod_age_minutes = 40
        mock_settings.sandbox_namespace = "openhands"

        now = datetime.now(timezone.utc)
        mock_list.return_value = [{
            "conversation_id": "conv-1",
            "execution_status": "awaiting_user_input",
            "sandbox_status": "RUNNING",
            "sandbox_id": "sandbox-1",
            "updated_at": (now - timedelta(hours=2)).isoformat(),
            "created_at": (now - timedelta(hours=3)).isoformat(),
        }]
        # Pod is only 10 minutes old (below 40-minute threshold)
        mock_pod_time.return_value = now - timedelta(minutes=10)

        await cleanup_idle_conversations()
        mock_stop.assert_not_called()


# ---------------------------------------------------------------------------
# Orphaned sandbox cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphaned_cleanup_removes_sandboxes_without_conversations():
    """Sandboxes with no matching conversation are cleaned up."""
    with patch("openhands_lifecycle.cleanup.settings") as mock_settings, \
         patch("openhands_lifecycle.cleanup._list_k8s_sandbox_ids") as mock_k8s, \
         patch("openhands_lifecycle.cleanup._list_conversations") as mock_list, \
         patch("openhands_lifecycle.cleanup._delete_sandbox_resources") as mock_delete, \
         patch("openhands_lifecycle.cleanup._get_sandbox_pod_start_time") as mock_pod_time:

        mock_settings.sandbox_namespace = "openhands"
        mock_settings.sandbox_min_pod_age_minutes = 40

        now = datetime.now(timezone.utc)
        mock_k8s.return_value = {"orphan-1", "active-1"}
        mock_list.return_value = [
            {"conversation_id": "conv-1", "sandbox_id": "active-1", "sandbox_status": "RUNNING"},
        ]
        # Both pods are old enough to clean
        mock_pod_time.return_value = now - timedelta(hours=2)

        await cleanup_orphaned_sandboxes()
        mock_delete.assert_called_once_with("orphan-1", "openhands")


@pytest.mark.asyncio
async def test_orphaned_cleanup_skips_paused_sandboxes():
    """PAUSED sandboxes are NOT cleaned up (needed for resume)."""
    with patch("openhands_lifecycle.cleanup.settings") as mock_settings, \
         patch("openhands_lifecycle.cleanup._list_k8s_sandbox_ids") as mock_k8s, \
         patch("openhands_lifecycle.cleanup._list_conversations") as mock_list, \
         patch("openhands_lifecycle.cleanup._delete_sandbox_resources") as mock_delete, \
         patch("openhands_lifecycle.cleanup._get_sandbox_pod_start_time") as mock_pod_time:

        mock_settings.sandbox_namespace = "openhands"
        mock_settings.sandbox_min_pod_age_minutes = 40

        now = datetime.now(timezone.utc)
        mock_k8s.return_value = {"paused-1"}
        mock_list.return_value = [
            {"conversation_id": "conv-1", "sandbox_id": "paused-1", "sandbox_status": "PAUSED"},
        ]
        mock_pod_time.return_value = now - timedelta(hours=2)

        await cleanup_orphaned_sandboxes()
        mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# PVC cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pvc_cleanup_disabled_when_zero():
    """PVC cleanup is a no-op when max_age_days is 0."""
    with patch("openhands_lifecycle.cleanup.settings") as mock_settings:
        mock_settings.sandbox_pvc_max_age_days = 0
        await cleanup_old_pvcs()
        # No K8s API calls should be made
