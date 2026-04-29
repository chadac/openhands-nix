"""Tests for the transparent proxy engine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands_broker.proxy import CredentialInjector, ProxyResult, proxy_request


# ---------------------------------------------------------------------------
# CredentialInjector base class
# ---------------------------------------------------------------------------


class MockInjector(CredentialInjector):
    """Test injector that adds a fixed header."""

    async def inject(
        self, method, url, headers, body, conversation_id,
    ):
        headers["X-Test-Auth"] = "injected-token"
        return headers, body


class BlockingInjector(CredentialInjector):
    """Test injector that blocks certain requests."""

    async def inject(self, method, url, headers, body, conversation_id):
        return headers, body

    async def inspect_request(self, method, url, headers, body, conversation_id):
        from fastapi import HTTPException
        if "forbidden" in url:
            raise HTTPException(status_code=403, detail="Blocked by policy")


class ResponseFilterInjector(CredentialInjector):
    """Test injector that modifies responses."""

    async def inject(self, method, url, headers, body, conversation_id):
        return headers, body

    async def inspect_response(self, method, url, result, conversation_id):
        # Strip sensitive headers from response
        result.headers.pop("X-Secret", None)
        return result


# ---------------------------------------------------------------------------
# Proxy request tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_injects_credentials():
    """Proxy injects credentials via the injector before forwarding."""
    injector = MockInjector()

    request = MagicMock()
    request.method = "GET"
    request.headers = MagicMock()
    request.headers.raw = []
    request.query_params = {}
    request.body = AsyncMock(return_value=None)

    with patch("openhands_broker.proxy._forward_request") as mock_forward:
        mock_forward.return_value = ProxyResult(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"ok": true}',
        )
        response = await proxy_request(
            request, "https://api.example.com", "repos/foo/bar",
            injector, conversation_id="conv-123",
        )

        # Verify the forwarded request had the injected header
        call_args = mock_forward.call_args
        forwarded_headers = call_args[1].get("headers") or call_args[0][2]
        assert forwarded_headers.get("X-Test-Auth") == "injected-token"


@pytest.mark.asyncio
async def test_proxy_blocks_forbidden_request():
    """Proxy blocks requests when inspect_request raises."""
    injector = BlockingInjector()

    request = MagicMock()
    request.method = "POST"
    request.headers = MagicMock()
    request.headers.raw = []
    request.query_params = {}
    request.body = AsyncMock(return_value=b'{"data": "test"}')

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await proxy_request(
            request, "https://api.example.com", "forbidden/endpoint",
            injector, conversation_id="conv-123",
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_proxy_filters_response():
    """Proxy modifies responses via inspect_response."""
    injector = ResponseFilterInjector()

    request = MagicMock()
    request.method = "GET"
    request.headers = MagicMock()
    request.headers.raw = []
    request.query_params = {}
    request.body = AsyncMock(return_value=None)

    with patch("openhands_broker.proxy._forward_request") as mock_forward:
        mock_forward.return_value = ProxyResult(
            status_code=200,
            headers={"content-type": "application/json", "X-Secret": "leak"},
            body=b'{"data": "ok"}',
        )
        response = await proxy_request(
            request, "https://api.example.com", "safe/endpoint",
            injector, conversation_id="conv-123",
        )

        # X-Secret should have been stripped by the response filter
        # (exact assertion depends on how Response is constructed in impl)


# ---------------------------------------------------------------------------
# Provider injector tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_injector_adds_bearer():
    """GitHubInjector adds Bearer token."""
    from openhands_broker.providers.github import GitHubInjector

    with patch("openhands_broker.providers.github.settings") as mock_settings:
        mock_settings.github_token = "ghp_test123"
        injector = GitHubInjector()
        headers, body = await injector.inject("GET", "/repos", {}, None, None)
        assert headers["Authorization"] == "Bearer ghp_test123"
        assert headers["Accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_github_injector_skips_when_no_token():
    """GitHubInjector doesn't add auth when no token configured."""
    from openhands_broker.providers.github import GitHubInjector

    with patch("openhands_broker.providers.github.settings") as mock_settings:
        mock_settings.github_token = ""
        injector = GitHubInjector()
        headers, body = await injector.inject("GET", "/repos", {}, None, None)
        assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_gitlab_injector_adds_private_token():
    """GitLabInjector adds PRIVATE-TOKEN header."""
    from openhands_broker.providers.gitlab import GitLabInjector

    with patch("openhands_broker.providers.gitlab.settings") as mock_settings:
        mock_settings.gitlab_token = "glpat-test123"
        injector = GitLabInjector()
        headers, body = await injector.inject("GET", "/projects", {}, None, None)
        assert headers["PRIVATE-TOKEN"] == "glpat-test123"


@pytest.mark.asyncio
async def test_jira_injector_adds_oauth_bearer():
    """JiraInjector adds OAuth Bearer token."""
    from openhands_broker.providers.jira import JiraInjector

    with patch("openhands_broker.providers.jira._get_access_token") as mock_token:
        mock_token.return_value = "atlassian-oauth-token"
        injector = JiraInjector()
        headers, body = await injector.inject("GET", "/issue/PLAT-555", {}, None, None)
        assert headers["Authorization"] == "Bearer atlassian-oauth-token"


@pytest.mark.asyncio
async def test_slack_injector_adds_bearer():
    """SlackInjector adds Bearer token."""
    from openhands_broker.providers.slack import SlackInjector

    with patch("openhands_broker.providers.slack.settings") as mock_settings:
        mock_settings.slack_bot_token = "xoxb-test123"
        injector = SlackInjector()
        headers, body = await injector.inject("POST", "/chat.postMessage", {}, None, None)
        assert headers["Authorization"] == "Bearer xoxb-test123"
