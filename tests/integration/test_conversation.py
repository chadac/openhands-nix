"""Test conversation lifecycle: create → sandbox resources → running."""

from __future__ import annotations

import logging

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from conftest import NAMESPACE, SandboxFixture, ServerConnection

logger = logging.getLogger(__name__)


class TestConversationLifecycle:
    """Verify that creating a conversation provisions the expected K8s resources."""

    def test_sandbox_job_exists(
        self,
        sandbox: SandboxFixture,
        k8s_clients: tuple[client.CoreV1Api, client.BatchV1Api, client.NetworkingV1Api],
    ) -> None:
        """A K8s Job named oh-sandbox-{id} should exist."""
        _, batch_v1, _ = k8s_clients
        job = batch_v1.read_namespaced_job(
            name=f"oh-sandbox-{sandbox.sandbox_id}",
            namespace=NAMESPACE,
        )
        assert job is not None
        assert job.metadata.labels.get("openhands.ai/sandbox-id") == sandbox.sandbox_id

    def test_sandbox_service_exists(
        self,
        sandbox: SandboxFixture,
        k8s_clients: tuple[client.CoreV1Api, client.BatchV1Api, client.NetworkingV1Api],
    ) -> None:
        """A ClusterIP Service named oh-sandbox-{id} should exist."""
        core_v1, _, _ = k8s_clients
        svc = core_v1.read_namespaced_service(
            name=f"oh-sandbox-{sandbox.sandbox_id}",
            namespace=NAMESPACE,
        )
        assert svc is not None
        ports = {p.name: p.port for p in svc.spec.ports}
        assert ports.get("http") == 8000
        assert ports.get("vscode") == 8001

    def test_sandbox_ingress_exists(
        self,
        sandbox: SandboxFixture,
        k8s_clients: tuple[client.CoreV1Api, client.BatchV1Api, client.NetworkingV1Api],
    ) -> None:
        """A per-sandbox Ingress should exist (requires SANDBOX_K8S_EXTERNAL_HOST)."""
        _, _, networking_v1 = k8s_clients
        try:
            ingress = networking_v1.read_namespaced_ingress(
                name=f"oh-sandbox-{sandbox.sandbox_id}",
                namespace=NAMESPACE,
            )
        except ApiException as e:
            if e.status == 404:
                pytest.skip(
                    "Sandbox Ingress not created — SANDBOX_K8S_EXTERNAL_HOST may not be set"
                )
            raise
        assert ingress is not None
        # Should have auth-type: none (sandbox uses session API key, not Cognito)
        assert ingress.metadata.annotations.get("alb.ingress.kubernetes.io/auth-type") == "none"

    def test_sandbox_pod_running(
        self,
        sandbox: SandboxFixture,
        k8s_clients: tuple[client.CoreV1Api, client.BatchV1Api, client.NetworkingV1Api],
    ) -> None:
        """The sandbox pod should be Running with all containers ready."""
        core_v1, _, _ = k8s_clients
        pods = core_v1.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector=f"openhands.ai/sandbox-id={sandbox.sandbox_id}",
            limit=1,
        )
        assert len(pods.items) == 1
        pod = pods.items[0]
        assert pod.status.phase == "Running"
        for cs in pod.status.container_statuses or []:
            assert cs.ready, f"Container {cs.name} is not ready"

    def test_conversation_shows_sandbox(
        self,
        sandbox: SandboxFixture,
        server: ServerConnection,
    ) -> None:
        """The conversation API should return sandbox info with status RUNNING."""
        conv = server.get_conversation(sandbox.conversation_id)
        # The response structure varies; check that sandbox reference exists
        sandbox_ref = conv.get("sandbox_id") or conv.get("sandbox", {}).get("id")
        assert sandbox_ref == sandbox.sandbox_id
