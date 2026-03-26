"""KubernetesSandboxService: manage agent-server sandboxes as K8s Jobs.

This module implements the SandboxService and SandboxSpecService interfaces
from the OpenHands V1 app server, backed by Kubernetes Jobs.

Each sandbox is a K8s Job running a single pod with the agent-server image.
The sandbox is exposed via a K8s Service, with port-forwarding or Ingress
for external access.

Configuration via environment variables:
  SANDBOX_K8S_NAMESPACE       — namespace for sandbox Jobs (default: "openhands")
  SANDBOX_K8S_IMAGE           — agent-server image (default: from get_agent_server_image())
  SANDBOX_K8S_SERVICE_ACCOUNT — service account for sandbox pods
  SANDBOX_K8S_NODE_SELECTOR   — JSON node selector (e.g. '{"gpu": "true"}')
  SANDBOX_K8S_TOLERATIONS     — JSON tolerations array
  SANDBOX_K8S_RESOURCE_REQUESTS — JSON resource requests (default: {"cpu": "250m", "memory": "512Mi"})
  SANDBOX_K8S_RESOURCE_LIMITS — JSON resource limits
  SANDBOX_K8S_IMAGE_PULL_SECRETS — comma-separated list of image pull secret names
  SANDBOX_K8S_STORAGE_CLASS   — storage class for workspace PVCs (optional)
  SANDBOX_HOST_PORT           — port of the main app server (for webhook callbacks)
  SANDBOX_STARTUP_GRACE_SECONDS — grace period before health check failures → ERROR
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import string
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    ExposedUrl,
    SandboxInfo,
    SandboxPage,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import SandboxService
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    get_agent_server_env,
    get_agent_server_image,
)
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxSpecInfo,
    SandboxSpecInfoPage,
)

logger = logging.getLogger(__name__)

# Label used to identify sandbox Jobs managed by this service
_LABEL_MANAGED_BY = "openhands.ai/managed-by"
_LABEL_MANAGED_BY_VALUE = "openhands-nix-kubernetes"
_LABEL_SANDBOX_ID = "openhands.ai/sandbox-id"
_LABEL_SESSION_KEY_HASH = "openhands.ai/session-key-hash"

# Environment variable names matching upstream conventions
_SESSION_API_KEY_VAR = "OH_SESSION_API_KEYS_0"
_WEBHOOK_CALLBACK_VAR = "OH_WEBHOOKS_0_BASE_URL"

_DEFAULT_PORT = 8000


def _generate_session_key() -> str:
    """Generate a secure random session API key (base62, 43 chars)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(43))


def _hash_session_key(key: str) -> str:
    """Short hash of session key for label-based lookup (not security-sensitive)."""
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _env_json(var: str, default: Any = None) -> Any:
    """Parse a JSON environment variable, returning default if unset or invalid."""
    raw = os.getenv(var)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid JSON in %s: %s", var, raw)
        return default


def _pod_phase_to_status(phase: str | None, container_statuses: list | None = None) -> SandboxStatus:
    """Map K8s pod phase to SandboxStatus."""
    if phase == "Running":
        # Check if containers are actually ready
        if container_statuses:
            for cs in container_statuses:
                if cs.state and cs.state.waiting:
                    return SandboxStatus.STARTING
        return SandboxStatus.RUNNING
    elif phase == "Pending":
        return SandboxStatus.STARTING
    elif phase == "Succeeded":
        return SandboxStatus.PAUSED
    elif phase == "Failed":
        return SandboxStatus.ERROR
    elif phase == "Unknown":
        return SandboxStatus.ERROR
    return SandboxStatus.STARTING


class KubernetesSandboxService(SandboxService):
    """Manage agent-server sandboxes as Kubernetes Jobs."""

    def __init__(
        self,
        namespace: str | None = None,
        host_port: int | None = None,
        startup_grace_seconds: float = 60.0,
        health_check_path: str | None = "/health",
    ):
        self.namespace = namespace or os.getenv("SANDBOX_K8S_NAMESPACE", "openhands")
        self.host_port = host_port or int(os.getenv("SANDBOX_HOST_PORT", "3000"))
        self.startup_grace_seconds = startup_grace_seconds or float(
            os.getenv("SANDBOX_STARTUP_GRACE_SECONDS", "60")
        )
        self.health_check_path = health_check_path

        # Initialize K8s client
        try:
            k8s_config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
            logger.info("Loaded kubeconfig from default location")

        self._batch_v1 = client.BatchV1Api()
        self._core_v1 = client.CoreV1Api()

    def _label_selector(self, extra: dict[str, str] | None = None) -> str:
        """Build a label selector string for our managed Jobs."""
        labels = {_LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE}
        if extra:
            labels.update(extra)
        return ",".join(f"{k}={v}" for k, v in labels.items())

    def _job_name(self, sandbox_id: str) -> str:
        return f"oh-sandbox-{sandbox_id}"

    def _service_name(self, sandbox_id: str) -> str:
        return f"oh-sandbox-{sandbox_id}"

    def _sandbox_id_from_job(self, job: client.V1Job) -> str:
        """Extract sandbox ID from Job labels."""
        return job.metadata.labels.get(_LABEL_SANDBOX_ID, "")

    def _get_pod_for_job(self, job_name: str) -> client.V1Pod | None:
        """Find the pod created by a Job."""
        try:
            pods = self._core_v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"job-name={job_name}",
                limit=1,
            )
            return pods.items[0] if pods.items else None
        except ApiException:
            return None

    def _extract_session_key(self, job: client.V1Job) -> str | None:
        """Extract session API key from Job's container env vars."""
        try:
            containers = job.spec.template.spec.containers
            if containers:
                for env_var in containers[0].env or []:
                    if env_var.name == _SESSION_API_KEY_VAR:
                        return env_var.value
        except (AttributeError, IndexError):
            pass
        return None

    def _job_to_sandbox_info(self, job: client.V1Job) -> SandboxInfo | None:
        """Convert a K8s Job to SandboxInfo."""
        sandbox_id = self._sandbox_id_from_job(job)
        if not sandbox_id:
            return None

        # Get pod status
        pod = self._get_pod_for_job(self._job_name(sandbox_id))
        if pod:
            phase = pod.status.phase if pod.status else None
            container_statuses = pod.status.container_statuses if pod.status else None
            status = _pod_phase_to_status(phase, container_statuses)
        else:
            # No pod yet — still starting
            if job.status and job.status.failed:
                status = SandboxStatus.ERROR
            else:
                status = SandboxStatus.STARTING

        # Check if Job is suspended (paused)
        if job.spec.suspend:
            status = SandboxStatus.PAUSED

        # Build exposed URLs
        exposed_urls: list[ExposedUrl] | None = None
        session_api_key: str | None = None

        if status == SandboxStatus.RUNNING:
            session_api_key = self._extract_session_key(job)
            # Try to get the Service ClusterIP
            try:
                svc = self._core_v1.read_namespaced_service(
                    name=self._service_name(sandbox_id),
                    namespace=self.namespace,
                )
                cluster_ip = svc.spec.cluster_ip
                exposed_urls = [
                    ExposedUrl(
                        name=AGENT_SERVER,
                        url=f"http://{cluster_ip}:{_DEFAULT_PORT}",
                        port=_DEFAULT_PORT,
                    ),
                ]
            except ApiException:
                # Service not found — fall back to pod IP
                if pod and pod.status and pod.status.pod_ip:
                    exposed_urls = [
                        ExposedUrl(
                            name=AGENT_SERVER,
                            url=f"http://{pod.status.pod_ip}:{_DEFAULT_PORT}",
                            port=_DEFAULT_PORT,
                        ),
                    ]

        # Parse creation time
        created_at = datetime.now(timezone.utc)
        if job.metadata.creation_timestamp:
            created_at = job.metadata.creation_timestamp.replace(tzinfo=timezone.utc)

        return SandboxInfo(
            id=sandbox_id,
            created_by_user_id=None,
            sandbox_spec_id=job.metadata.labels.get("openhands.ai/spec-id", "default"),
            status=status,
            session_api_key=session_api_key,
            exposed_urls=exposed_urls,
            created_at=created_at,
        )

    async def search_sandboxes(
        self,
        page_id: str | None = None,
        limit: int = 100,
    ) -> SandboxPage:
        try:
            jobs = self._batch_v1.list_namespaced_job(
                namespace=self.namespace,
                label_selector=self._label_selector(),
                limit=limit,
                _continue=page_id or None,
            )
        except ApiException as e:
            logger.error("Failed to list sandbox Jobs: %s", e)
            return SandboxPage(items=[], next_page_id=None)

        items = []
        for job in jobs.items:
            info = self._job_to_sandbox_info(job)
            if info:
                items.append(info)

        return SandboxPage(
            items=items,
            next_page_id=jobs.metadata._continue if jobs.metadata else None,
        )

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        try:
            job = self._batch_v1.read_namespaced_job(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
            )
        except ApiException as e:
            if e.status == 404:
                return None
            logger.error("Failed to get sandbox Job %s: %s", sandbox_id, e)
            return None

        return self._job_to_sandbox_info(job)

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        key_hash = _hash_session_key(session_api_key)
        try:
            jobs = self._batch_v1.list_namespaced_job(
                namespace=self.namespace,
                label_selector=self._label_selector({_LABEL_SESSION_KEY_HASH: key_hash}),
                limit=1,
            )
        except ApiException as e:
            logger.error("Failed to search sandbox by session key: %s", e)
            return None

        if not jobs.items:
            return None

        info = self._job_to_sandbox_info(jobs.items[0])
        # Verify the full key matches (hash collision protection)
        if info and info.session_api_key != session_api_key:
            return None
        return info

    async def start_sandbox(
        self,
        sandbox_spec_id: str | None = None,
        sandbox_id: str | None = None,
    ) -> SandboxInfo:
        if sandbox_id is None:
            sandbox_id = secrets.token_hex(8)

        session_api_key = _generate_session_key()
        key_hash = _hash_session_key(session_api_key)

        image = sandbox_spec_id or get_agent_server_image()

        # Build environment variables
        env_vars: dict[str, str] = {
            "PORT": str(_DEFAULT_PORT),
            "HOST": "0.0.0.0",
            "LOG_JSON": "true",
            "PYTHONUNBUFFERED": "1",
            "OH_CONVERSATIONS_PATH": "/workspace/conversations",
            "OH_BASH_EVENTS_DIR": "/workspace/bash_events",
            _SESSION_API_KEY_VAR: session_api_key,
            **get_agent_server_env(),
        }

        # Webhook callback URL (for the main app server)
        app_server_host = os.getenv("SANDBOX_K8S_APP_SERVER_HOST")
        if app_server_host:
            env_vars[_WEBHOOK_CALLBACK_VAR] = f"{app_server_host}/api/v1/webhooks"

        # CORS origins
        cors_origins = os.getenv("OH_ALLOW_CORS_ORIGINS", "").split(",")
        for idx, origin in enumerate(cors_origins):
            origin = origin.strip()
            if origin:
                env_vars[f"OH_ALLOW_CORS_ORIGINS_{idx}"] = origin

        # NIX_PACKAGES for dynamic Nix package installation
        nix_packages = os.getenv("SANDBOX_NIX_PACKAGES", "")
        if nix_packages:
            env_vars["NIX_PACKAGES"] = nix_packages

        # Build Job labels
        labels = {
            _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
            _LABEL_SANDBOX_ID: sandbox_id,
            _LABEL_SESSION_KEY_HASH: key_hash,
            "openhands.ai/spec-id": image.split("/")[-1].split(":")[0],
        }

        # Container spec
        container = client.V1Container(
            name="agent-server",
            image=image,
            image_pull_policy=os.getenv("SANDBOX_K8S_IMAGE_PULL_POLICY", "IfNotPresent"),
            ports=[client.V1ContainerPort(container_port=_DEFAULT_PORT, name="http")],
            env=[client.V1EnvVar(name=k, value=v) for k, v in env_vars.items()],
            resources=client.V1ResourceRequirements(
                requests=_env_json("SANDBOX_K8S_RESOURCE_REQUESTS", {"cpu": "250m", "memory": "512Mi"}),
                limits=_env_json("SANDBOX_K8S_RESOURCE_LIMITS") or None,
            ),
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=_DEFAULT_PORT),
                initial_delay_seconds=10,
                period_seconds=5,
                timeout_seconds=3,
                failure_threshold=30,
            ),
            liveness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=_DEFAULT_PORT),
                initial_delay_seconds=30,
                period_seconds=30,
                timeout_seconds=5,
            ),
        )

        # Pod spec
        pod_spec = client.V1PodSpec(
            containers=[container],
            restart_policy="Never",
        )

        # Optional: service account
        sa = os.getenv("SANDBOX_K8S_SERVICE_ACCOUNT")
        if sa:
            pod_spec.service_account_name = sa

        # Optional: node selector
        node_selector = _env_json("SANDBOX_K8S_NODE_SELECTOR")
        if node_selector:
            pod_spec.node_selector = node_selector

        # Optional: tolerations
        tolerations = _env_json("SANDBOX_K8S_TOLERATIONS")
        if tolerations:
            pod_spec.tolerations = [client.V1Toleration(**t) for t in tolerations]

        # Optional: image pull secrets
        pull_secrets = os.getenv("SANDBOX_K8S_IMAGE_PULL_SECRETS", "")
        if pull_secrets:
            pod_spec.image_pull_secrets = [
                client.V1LocalObjectReference(name=s.strip())
                for s in pull_secrets.split(",") if s.strip()
            ]

        # Create the Job
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
                labels=labels,
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=300,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=pod_spec,
                ),
            ),
        )

        try:
            self._batch_v1.create_namespaced_job(
                namespace=self.namespace,
                body=job,
            )
            logger.info("Created sandbox Job %s (image=%s)", sandbox_id, image)
        except ApiException as e:
            raise RuntimeError(f"Failed to create sandbox Job: {e}") from e

        # Create a ClusterIP Service for stable networking
        service = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(
                name=self._service_name(sandbox_id),
                namespace=self.namespace,
                labels=labels,
            ),
            spec=client.V1ServiceSpec(
                selector={_LABEL_SANDBOX_ID: sandbox_id},
                ports=[client.V1ServicePort(port=_DEFAULT_PORT, target_port=_DEFAULT_PORT, name="http")],
                type="ClusterIP",
            ),
        )

        try:
            self._core_v1.create_namespaced_service(
                namespace=self.namespace,
                body=service,
            )
        except ApiException as e:
            logger.warning("Failed to create sandbox Service (non-fatal): %s", e)

        return SandboxInfo(
            id=sandbox_id,
            created_by_user_id=None,
            sandbox_spec_id=image,
            status=SandboxStatus.STARTING,
            session_api_key=session_api_key,
            exposed_urls=None,
            created_at=datetime.now(timezone.utc),
        )

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        try:
            job = self._batch_v1.read_namespaced_job(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
            )
        except ApiException as e:
            if e.status == 404:
                return False
            raise

        if not job.spec.suspend:
            return True  # Already running

        # Unsuspend the Job
        job.spec.suspend = False
        try:
            self._batch_v1.replace_namespaced_job(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
                body=job,
            )
            logger.info("Resumed sandbox Job %s", sandbox_id)
            return True
        except ApiException as e:
            logger.error("Failed to resume sandbox %s: %s", sandbox_id, e)
            return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        try:
            job = self._batch_v1.read_namespaced_job(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
            )
        except ApiException as e:
            if e.status == 404:
                return False
            raise

        if job.spec.suspend:
            return True  # Already suspended

        # Suspend the Job (K8s 1.24+)
        job.spec.suspend = True
        try:
            self._batch_v1.replace_namespaced_job(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
                body=job,
            )
            logger.info("Paused sandbox Job %s", sandbox_id)
            return True
        except ApiException as e:
            logger.error("Failed to pause sandbox %s: %s", sandbox_id, e)
            return False

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        deleted = False

        # Delete the Job
        try:
            self._batch_v1.delete_namespaced_job(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
            deleted = True
            logger.info("Deleted sandbox Job %s", sandbox_id)
        except ApiException as e:
            if e.status != 404:
                logger.error("Failed to delete sandbox Job %s: %s", sandbox_id, e)

        # Delete the Service
        try:
            self._core_v1.delete_namespaced_service(
                name=self._service_name(sandbox_id),
                namespace=self.namespace,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete sandbox Service %s: %s", sandbox_id, e)

        return deleted


class KubernetesSandboxSpecService(SandboxSpecService):
    """Sandbox spec service for Kubernetes — returns a single default spec."""

    def __init__(self):
        image = os.getenv("SANDBOX_K8S_IMAGE") or get_agent_server_image()
        self._default_spec = SandboxSpecInfo(
            id=image,
            command=["--port", str(_DEFAULT_PORT)],
            initial_env={
                "LOG_JSON": "true",
                "OH_CONVERSATIONS_PATH": "/workspace/conversations",
                "OH_BASH_EVENTS_DIR": "/workspace/bash_events",
                "PYTHONUNBUFFERED": "1",
                **get_agent_server_env(),
            },
            working_dir="/workspace/project",
        )

    async def search_sandbox_specs(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxSpecInfoPage:
        return SandboxSpecInfoPage(items=[self._default_spec], next_page_id=None)

    async def get_sandbox_spec(self, sandbox_spec_id: str) -> SandboxSpecInfo | None:
        if sandbox_spec_id == self._default_spec.id:
            return self._default_spec
        return None


# ---- Dependency Injection Injectors ----

from collections.abc import AsyncGenerator

from fastapi import Request

from openhands.app_server.sandbox.sandbox_service import SandboxServiceInjector
from openhands.app_server.sandbox.sandbox_spec_service import SandboxSpecServiceInjector
from openhands.app_server.services.injector import InjectorState


class KubernetesSandboxServiceInjector(SandboxServiceInjector):
    """Dependency injector for Kubernetes sandbox services."""

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        yield KubernetesSandboxService()


class KubernetesSandboxSpecServiceInjector(SandboxSpecServiceInjector):
    """Dependency injector for Kubernetes sandbox spec services."""

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxSpecService, None]:
        yield KubernetesSandboxSpecService()
