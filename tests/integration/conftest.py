"""Shared fixtures for OpenHands Kubernetes integration tests.

Uses kubectl port-forward to reach both the server and sandbox services,
bypassing ALB/Cognito auth.

Configuration via environment variables:
  OPENHANDS_TEST_NAMESPACE    — K8s namespace (default: "openhands")
  OPENHANDS_TEST_KUBECONFIG   — kubeconfig path (default: ~/.kube/config)
  OPENHANDS_TEST_TIMEOUT      — sandbox startup timeout seconds (default: 300)
  OPENHANDS_TEST_KEEP_SANDBOX — if "true", don't delete sandbox after tests
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

import httpx
import pytest
from kubernetes import client, config as k8s_config

logger = logging.getLogger(__name__)

NAMESPACE = os.getenv("OPENHANDS_TEST_NAMESPACE", "openhands")
KUBECONFIG = os.getenv("OPENHANDS_TEST_KUBECONFIG", "")
STARTUP_TIMEOUT = int(os.getenv("OPENHANDS_TEST_TIMEOUT", "300"))
KEEP_SANDBOX = os.getenv("OPENHANDS_TEST_KEEP_SANDBOX", "").lower() == "true"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-sandbox",
        action="store_true",
        default=False,
        help="Don't delete sandbox resources after tests (for debugging)",
    )


# ---------------------------------------------------------------------------
# K8s client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def k8s_clients() -> tuple[client.CoreV1Api, client.BatchV1Api, client.NetworkingV1Api]:
    """Initialize Kubernetes API clients."""
    if KUBECONFIG:
        k8s_config.load_kube_config(config_file=KUBECONFIG)
    else:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()

    return (
        client.CoreV1Api(),
        client.BatchV1Api(),
        client.NetworkingV1Api(),
    )


# ---------------------------------------------------------------------------
# Port-forwarding
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@contextmanager
def port_forward(
    namespace: str,
    resource: str,
    remote_port: int,
) -> Generator[int, None, None]:
    """Run kubectl port-forward, yield the local port, kill on exit."""
    local_port = _free_port()
    cmd = ["kubectl", "port-forward", "-n", namespace, resource, f"{local_port}:{remote_port}"]
    if KUBECONFIG:
        cmd.extend(["--kubeconfig", KUBECONFIG])

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=1):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.3)
                if proc.poll() is not None:
                    stderr = proc.stderr.read().decode() if proc.stderr else ""
                    raise RuntimeError(f"port-forward exited early: {stderr}")
        else:
            proc.kill()
            raise RuntimeError(f"port-forward to {resource}:{remote_port} timed out")

        logger.info("Port-forward %s:%d → localhost:%d", resource, remote_port, local_port)
        yield local_port
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Server connection
# ---------------------------------------------------------------------------


@dataclass
class ServerConnection:
    """HTTP client for the OpenHands server API."""

    base_url: str
    client: httpx.Client

    def create_conversation(self) -> dict:
        """Create a new conversation (triggers sandbox creation)."""
        r = self.client.post("/api/conversations", json={})
        r.raise_for_status()
        return r.json()

    def get_conversation(self, conversation_id: str) -> dict:
        """Get conversation details including sandbox status."""
        r = self.client.get(f"/api/conversations/{conversation_id}")
        r.raise_for_status()
        return r.json()

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation and its sandbox."""
        r = self.client.delete(f"/api/conversations/{conversation_id}")
        if r.status_code != 404:
            r.raise_for_status()


@pytest.fixture(scope="session")
def server() -> Generator[ServerConnection, None, None]:
    """Port-forward to the OpenHands server and yield an HTTP client."""
    with port_forward(NAMESPACE, "svc/openhands", 3000) as local_port:
        base_url = f"http://127.0.0.1:{local_port}"
        with httpx.Client(base_url=base_url, timeout=30, follow_redirects=True) as http:
            yield ServerConnection(base_url=base_url, client=http)


# ---------------------------------------------------------------------------
# Sandbox lifecycle
# ---------------------------------------------------------------------------


@dataclass
class SandboxFixture:
    """A running sandbox with connection details."""

    conversation_id: str
    sandbox_id: str


@pytest.fixture(scope="module")
def sandbox(
    request: pytest.FixtureRequest,
    server: ServerConnection,
    k8s_clients: tuple[client.CoreV1Api, client.BatchV1Api, client.NetworkingV1Api],
) -> Generator[SandboxFixture, None, None]:
    """Create a conversation, wait for sandbox RUNNING, yield, always cleanup."""
    core_v1 = k8s_clients[0]
    keep = request.config.getoption("--keep-sandbox", default=False) or KEEP_SANDBOX

    # 1. Create conversation
    logger.info("Creating test conversation...")
    conv = server.create_conversation()
    conversation_id = conv["conversation_id"]
    logger.info("Created conversation %s", conversation_id)

    sandbox_id: str | None = None
    try:
        # 2. Wait for sandbox pod to be Running + Ready
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            conv_info = server.get_conversation(conversation_id)
            sandbox_id = conv_info.get("sandbox_id") or (
                conv_info.get("sandbox", {}).get("id") if isinstance(conv_info.get("sandbox"), dict) else None
            )
            if sandbox_id:
                try:
                    pods = core_v1.list_namespaced_pod(
                        namespace=NAMESPACE,
                        label_selector=f"openhands.ai/sandbox-id={sandbox_id}",
                        limit=1,
                    )
                    if pods.items:
                        pod = pods.items[0]
                        phase = pod.status.phase if pod.status else None
                        if phase == "Running" and all(
                            cs.ready for cs in (pod.status.container_statuses or [])
                        ):
                            logger.info("Sandbox %s is Running", sandbox_id)
                            break
                except Exception as e:
                    logger.debug("Error checking pod: %s", e)
            time.sleep(3)
        else:
            pytest.fail(
                f"Sandbox did not reach Running within {STARTUP_TIMEOUT}s "
                f"(conversation={conversation_id}, sandbox={sandbox_id})"
            )

        yield SandboxFixture(conversation_id=conversation_id, sandbox_id=sandbox_id)

    finally:
        # 3. Always cleanup
        if not keep and conversation_id:
            logger.info("Cleaning up conversation %s", conversation_id)
            try:
                server.delete_conversation(conversation_id)
            except Exception as e:
                logger.warning("Cleanup failed for %s: %s", conversation_id, e)


@pytest.fixture()
def sandbox_client(sandbox: SandboxFixture) -> Generator[httpx.Client, None, None]:
    """Port-forward to the sandbox agent-server and yield an HTTP client."""
    svc = f"svc/oh-sandbox-{sandbox.sandbox_id}"
    with port_forward(NAMESPACE, svc, 8000) as local_port:
        with httpx.Client(base_url=f"http://127.0.0.1:{local_port}", timeout=30) as http:
            yield http
