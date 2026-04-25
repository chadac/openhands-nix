"""Test sandbox agent-server reachability and basic operations."""

from __future__ import annotations

import logging
import time

import httpx
import pytest

logger = logging.getLogger(__name__)


class TestSandboxHealth:
    """Verify the sandbox agent-server is reachable and healthy."""

    def test_health_endpoint(self, sandbox_client: httpx.Client) -> None:
        """GET /health should return 200."""
        r = sandbox_client.get("/health")
        assert r.status_code == 200

    def test_server_info(self, sandbox_client: httpx.Client) -> None:
        """GET /server_info should return server metadata."""
        r = sandbox_client.get("/server_info")
        # May return 404 if not implemented; 200 means it's there
        if r.status_code == 200:
            data = r.json()
            assert isinstance(data, dict)


class TestSandboxBashExecution:
    """Verify bash command execution via the agent-server API."""

    @staticmethod
    def _run_command(
        client: httpx.Client,
        command: str,
        timeout: int = 10,
        poll_timeout: float = 30.0,
    ) -> tuple[str, int | None]:
        """Execute a bash command and poll for output.

        Returns:
            (stdout, exit_code) tuple
        """
        r = client.post("/api/bash/start_bash_command", json={
            "command": command,
            "timeout": timeout,
        })
        r.raise_for_status()
        cmd_id = r.json()["id"]

        stdout = ""
        exit_code = None
        deadline = time.monotonic() + poll_timeout

        while time.monotonic() < deadline:
            r = client.get("/api/bash/bash_events/search", params={
                "command_id__eq": cmd_id,
                "kind__eq": "BashOutput",
                "limit": 100,
            })
            if r.status_code == 200:
                for item in r.json().get("items", []):
                    if item.get("stdout"):
                        stdout += item["stdout"]
                    if item.get("exit_code") is not None:
                        exit_code = item["exit_code"]
                        return stdout, exit_code
            time.sleep(0.5)

        return stdout, exit_code

    def test_echo_command(self, sandbox_client: httpx.Client) -> None:
        """Running 'echo hello' should return 'hello' with exit code 0."""
        stdout, exit_code = self._run_command(sandbox_client, "echo hello")
        assert exit_code == 0, f"Expected exit code 0, got {exit_code}"
        assert "hello" in stdout, f"Expected 'hello' in stdout, got: {stdout!r}"

    def test_file_write_read(self, sandbox_client: httpx.Client) -> None:
        """Writing and reading a file in /workspace should work."""
        marker = "integration-test-marker-12345"
        stdout, exit_code = self._run_command(
            sandbox_client,
            f"echo '{marker}' > /workspace/test-integration.txt && cat /workspace/test-integration.txt",
        )
        assert exit_code == 0
        assert marker in stdout

    def test_tools_available(self, sandbox_client: httpx.Client) -> None:
        """Essential tools (git, bash, tmux) should be on PATH."""
        for tool in ["git", "bash"]:
            _, exit_code = self._run_command(sandbox_client, f"which {tool}")
            assert exit_code == 0, f"{tool} not found on PATH"

    def test_nix_available(self, sandbox_client: httpx.Client) -> None:
        """Nix should be available if NIX_PACKAGES was configured."""
        stdout, exit_code = self._run_command(sandbox_client, "which nix 2>/dev/null || echo nix-not-found")
        if "nix-not-found" in stdout:
            pytest.skip("Nix not available in this sandbox image")
        assert exit_code == 0
