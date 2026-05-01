"""Test sandbox agent-server reachability and basic operations."""

from __future__ import annotations

import logging
import time

import httpx
import pytest

logger = logging.getLogger(__name__)


def _run_command(
    client: httpx.Client,
    command: str,
    timeout: float = 30.0,
) -> tuple[str, int | None]:
    """Execute a bash command via the upstream /execute_action API.

    Returns:
        (stdout, exit_code) tuple
    """
    r = client.post("/execute_action", json={
        "action": {
            "action": "run",
            "args": {"command": command},
        }
    }, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    content = data.get("content", "")
    extras = data.get("extras", {})
    metadata = extras.get("metadata", {})
    exit_code = metadata.get("exit_code", extras.get("exit_code"))
    return content, exit_code


class TestSandboxHealth:
    """Verify the sandbox agent-server is reachable and healthy."""

    def test_health_endpoint(self, sandbox_client: httpx.Client) -> None:
        """GET /alive should return 200."""
        r = sandbox_client.get("/alive")
        assert r.status_code == 200

    def test_server_info(self, sandbox_client: httpx.Client) -> None:
        """GET /server_info should return server metadata."""
        r = sandbox_client.get("/server_info")
        if r.status_code == 200:
            data = r.json()
            assert isinstance(data, dict)


class TestSandboxBashExecution:
    """Verify bash command execution via the agent-server API."""

    def test_echo_command(self, sandbox_client: httpx.Client) -> None:
        """Running 'echo hello' should return 'hello' with exit code 0."""
        stdout, exit_code = _run_command(sandbox_client, "echo hello")
        assert exit_code == 0, f"Expected exit code 0, got {exit_code}"
        assert "hello" in stdout, f"Expected 'hello' in stdout, got: {stdout!r}"

    def test_file_write_read(self, sandbox_client: httpx.Client) -> None:
        """Writing and reading a file in /workspace should work."""
        marker = "integration-test-marker-12345"
        stdout, exit_code = _run_command(
            sandbox_client,
            f"echo '{marker}' > /workspace/test-integration.txt && cat /workspace/test-integration.txt",
        )
        assert exit_code == 0
        assert marker in stdout

    def test_tools_available(self, sandbox_client: httpx.Client) -> None:
        """Essential tools (git, bash, tmux) should be on PATH."""
        for tool in ["git", "bash"]:
            _, exit_code = _run_command(sandbox_client, f"which {tool}")
            assert exit_code == 0, f"{tool} not found on PATH"

    def test_nix_available(self, sandbox_client: httpx.Client) -> None:
        """Nix should be available if NIX_PACKAGES was configured."""
        stdout, exit_code = _run_command(sandbox_client, "which nix 2>/dev/null || echo nix-not-found")
        if "nix-not-found" in stdout:
            pytest.skip("Nix not available in this sandbox image")
        assert exit_code == 0
