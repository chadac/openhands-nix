"""Test sandbox lifecycle: cleanup, PVC persistence, and error handling.

These tests manage their own sandbox (not using the shared module-scoped fixture)
because they need to control the lifecycle explicitly.
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from conftest import NAMESPACE, STARTUP_TIMEOUT, ServerConnection

logger = logging.getLogger(__name__)


def _wait_for_sandbox_running(
    server: ServerConnection,
    core_v1: client.CoreV1Api,
    conversation_id: str,
    timeout: float = STARTUP_TIMEOUT,
) -> str:
    """Wait for sandbox to reach Running state, return sandbox_id."""
    deadline = time.monotonic() + timeout
    # Upstream KubernetesRuntime uses conversation_id as the sandbox identifier
    sandbox_id = conversation_id

    while time.monotonic() < deadline:
        try:
            pods = core_v1.list_namespaced_pod(
                namespace=NAMESPACE,
                label_selector=f"app=openhands-runtime,session={conversation_id}",
                limit=1,
            )
            if pods.items:
                pod = pods.items[0]
                if pod.status.phase == "Running" and all(
                    cs.ready for cs in (pod.status.container_statuses or [])
                ):
                    return sandbox_id
        except Exception as e:
            logger.debug("Error checking pod: %s", e)
        time.sleep(3)

    msg = (
        f"Sandbox did not reach Running within {timeout}s "
        f"(conversation={conversation_id})"
    )
    raise RuntimeError(msg)


def _run_command(
    client: httpx.Client,
    command: str,
    timeout: float = 30.0,
) -> tuple[str, int | None]:
    """Execute a bash command via the upstream /execute_action API."""
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


class TestSandboxCleanup:
    """Verify that deleting a conversation cleans up K8s resources."""

    def test_delete_removes_pod_and_service(
        self,
        server: ServerConnection,
        k8s_clients: tuple[client.CoreV1Api, client.BatchV1Api, client.NetworkingV1Api],
    ) -> None:
        """Deleting a conversation should remove its Pod and Service."""
        core_v1, _, _ = k8s_clients

        # Create and wait for sandbox
        conv = server.create_conversation()
        conversation_id = conv["conversation_id"]
        try:
            sandbox_id = _wait_for_sandbox_running(server, core_v1, conversation_id)
        except Exception:
            server.delete_conversation(conversation_id)
            raise

        # Delete
        server.delete_conversation(conversation_id)

        # Wait for resources to be cleaned up (propagation delay)
        deadline = time.monotonic() + 30
        pod_gone = False
        svc_gone = False

        while time.monotonic() < deadline:
            try:
                core_v1.read_namespaced_pod(f"openhands-runtime-{sandbox_id}", NAMESPACE)
            except ApiException as e:
                if e.status == 404:
                    pod_gone = True

            try:
                core_v1.read_namespaced_service(f"openhands-runtime-{sandbox_id}-svc", NAMESPACE)
            except ApiException as e:
                if e.status == 404:
                    svc_gone = True

            if pod_gone and svc_gone:
                break
            time.sleep(2)

        assert pod_gone, "Pod was not deleted after conversation cleanup"
        assert svc_gone, "Service was not deleted after conversation cleanup"

    @pytest.mark.skip(reason="Upstream KubernetesRuntime deletes PVCs on cleanup; test is for custom sandbox service")
    def test_pvc_survives_deletion(
        self,
        server: ServerConnection,
        k8s_clients: tuple[client.CoreV1Api, client.BatchV1Api, client.NetworkingV1Api],
    ) -> None:
        """Workspace PVC should survive conversation deletion (for recovery)."""
        core_v1, _, _ = k8s_clients

        conv = server.create_conversation()
        conversation_id = conv["conversation_id"]
        try:
            sandbox_id = _wait_for_sandbox_running(server, core_v1, conversation_id)
        except Exception:
            server.delete_conversation(conversation_id)
            raise

        pvc_name = f"openhands-runtime-{sandbox_id}-pvc"

        # Check PVC exists before deletion
        try:
            core_v1.read_namespaced_persistent_volume_claim(pvc_name, NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                # Clean up and skip — no PVC means upstream doesn't use per-sandbox PVCs
                server.delete_conversation(conversation_id)
                pytest.skip("No per-sandbox PVC created (shared PVC mode?)")
            raise

        # Delete conversation
        server.delete_conversation(conversation_id)
        time.sleep(5)

        # PVC should still exist
        try:
            pvc = core_v1.read_namespaced_persistent_volume_claim(pvc_name, NAMESPACE)
            assert pvc is not None
        except ApiException as e:
            if e.status == 404:
                pytest.fail("PVC was deleted along with sandbox — should be retained")
            raise
        finally:
            # Clean up the PVC manually since it survives deletion
            try:
                core_v1.delete_namespaced_persistent_volume_claim(pvc_name, NAMESPACE)
            except ApiException:
                pass


class TestSandboxBashErrors:
    """Verify error handling in bash command execution."""

    def test_nonexistent_command(self, sandbox_client: httpx.Client) -> None:
        """Running a command that doesn't exist should return non-zero exit code."""
        _, exit_code = _run_command(sandbox_client, "this_command_does_not_exist_xyz")
        assert exit_code is not None
        assert exit_code != 0

    def test_false_command(self, sandbox_client: httpx.Client) -> None:
        """'false' should return exit code 1."""
        _, exit_code = _run_command(sandbox_client, "false")
        assert exit_code == 1

    def test_stderr_output(self, sandbox_client: httpx.Client) -> None:
        """Commands writing to stderr should still complete."""
        stdout, exit_code = _run_command(
            sandbox_client, "echo ok && echo err >&2"
        )
        assert exit_code == 0
        assert "ok" in stdout

    def test_multiline_output(self, sandbox_client: httpx.Client) -> None:
        """Multi-line output should be captured correctly."""
        stdout, exit_code = _run_command(
            sandbox_client, "seq 1 5"
        )
        assert exit_code == 0
        lines = [l for l in stdout.strip().split("\n") if l.strip()]
        assert len(lines) >= 5

    def test_environment_variables(self, sandbox_client: httpx.Client) -> None:
        """Sandbox should have expected environment variables set."""
        stdout, exit_code = _run_command(
            sandbox_client, "echo $HOME"
        )
        assert exit_code == 0
        assert stdout.strip(), "HOME should be set"

    def test_workspace_directory_writable(self, sandbox_client: httpx.Client) -> None:
        """The /workspace directory should be writable."""
        marker = f"test-{int(time.time())}"
        stdout, exit_code = _run_command(
            sandbox_client,
            f"mkdir -p /workspace/test-dir && echo {marker} > /workspace/test-dir/file.txt && cat /workspace/test-dir/file.txt",
        )
        assert exit_code == 0
        assert marker in stdout


class TestConcurrentCommands:
    """Verify multiple bash commands can run without interference."""

    def test_two_sequential_commands(self, sandbox_client: httpx.Client) -> None:
        """Two commands run sequentially should produce distinct outputs."""
        stdout1, code1 = _run_command(sandbox_client, "echo cmd-one")
        assert code1 == 0
        assert "cmd-one" in stdout1

        stdout2, code2 = _run_command(sandbox_client, "echo cmd-two")
        assert code2 == 0
        assert "cmd-two" in stdout2
