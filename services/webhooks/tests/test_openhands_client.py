"""Tests for the OpenHands API client."""

from unittest.mock import AsyncMock, patch

import pytest

from openhands_webhooks.openhands_client import conversation_url, _normalize_uuid


# ---------------------------------------------------------------------------
# URL generation
# ---------------------------------------------------------------------------


def test_conversation_url():
    """conversation_url builds a proper URL."""
    with patch("openhands_webhooks.openhands_client.settings") as mock_settings:
        mock_settings.openhands_url = "https://oh.example.com"
        url = conversation_url("conv-123")
        assert url == "https://oh.example.com/conversations/conv-123"


def test_conversation_url_strips_trailing_slash():
    """Trailing slash on base URL is stripped."""
    with patch("openhands_webhooks.openhands_client.settings") as mock_settings:
        mock_settings.openhands_url = "https://oh.example.com/"
        url = conversation_url("conv-123")
        assert url == "https://oh.example.com/conversations/conv-123"


# ---------------------------------------------------------------------------
# UUID normalization
# ---------------------------------------------------------------------------


def test_normalize_uuid_adds_hyphens():
    """Hex UUID is normalized to hyphenated form."""
    result = _normalize_uuid("550e8400e29b41d4a716446655440000")
    assert result == "550e8400-e29b-41d4-a716-446655440000"


def test_normalize_uuid_preserves_hyphenated():
    """Already hyphenated UUID is unchanged."""
    result = _normalize_uuid("550e8400-e29b-41d4-a716-446655440000")
    assert result == "550e8400-e29b-41d4-a716-446655440000"


def test_normalize_uuid_non_uuid_passthrough():
    """Non-UUID strings pass through unchanged."""
    result = _normalize_uuid("not-a-uuid")
    assert result == "not-a-uuid"
