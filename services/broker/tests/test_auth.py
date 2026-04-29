"""Tests for broker SA token authentication."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from openhands_broker.auth import verify_sandbox_token, _SA_PATTERN


# ---------------------------------------------------------------------------
# SA name pattern matching
# ---------------------------------------------------------------------------


def test_sa_pattern_matches_valid():
    """The regex extracts namespace and sandbox_id from a valid SA username."""
    m = _SA_PATTERN.match("system:serviceaccount:openhands:sandbox-abc123")
    assert m is not None
    assert m.group(1) == "openhands"
    assert m.group(2) == "abc123"


def test_sa_pattern_matches_uuid():
    """Sandbox IDs can be UUIDs."""
    m = _SA_PATTERN.match(
        "system:serviceaccount:openhands:sandbox-550e8400-e29b-41d4-a716-446655440000"
    )
    assert m is not None
    assert m.group(2) == "550e8400-e29b-41d4-a716-446655440000"


def test_sa_pattern_rejects_non_sandbox():
    """Non-sandbox service accounts don't match."""
    assert _SA_PATTERN.match("system:serviceaccount:openhands:openhands") is None
    assert _SA_PATTERN.match("system:serviceaccount:openhands:default") is None


def test_sa_pattern_rejects_other_namespace_format():
    """Malformed usernames don't match."""
    assert _SA_PATTERN.match("sandbox-abc123") is None
    assert _SA_PATTERN.match("system:serviceaccount:sandbox-abc123") is None


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_missing_token_returns_401():
    """Missing Authorization header returns 401."""
    request = MagicMock()
    request.headers = {}

    with pytest.raises(HTTPException) as exc_info:
        await verify_sandbox_token(request)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_invalid_token_returns_401():
    """Invalid token (TokenReview rejects) returns 401."""
    request = MagicMock()
    request.headers = {"authorization": "Bearer bad-token"}

    with patch("openhands_broker.auth._token_review") as mock_review:
        mock_review.return_value = None  # Token review failed
        with pytest.raises(HTTPException) as exc_info:
            await verify_sandbox_token(request)
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_non_sandbox_sa_returns_403():
    """Valid token but non-sandbox SA returns 403."""
    request = MagicMock()
    request.headers = {"authorization": "Bearer valid-token"}

    with patch("openhands_broker.auth._token_review") as mock_review:
        mock_review.return_value = "system:serviceaccount:openhands:default"
        with pytest.raises(HTTPException) as exc_info:
            await verify_sandbox_token(request)
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_valid_sandbox_token():
    """Valid sandbox SA token returns the sandbox_id."""
    request = MagicMock()
    request.headers = {"authorization": "Bearer valid-token"}

    with patch("openhands_broker.auth._token_review") as mock_review:
        mock_review.return_value = "system:serviceaccount:openhands:sandbox-abc123"
        result = await verify_sandbox_token(request)
        assert result == "abc123"
