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
  SANDBOX_HOST_PORT           — port of the main app server (for webhook callbacks)
  SANDBOX_STARTUP_GRACE_SECONDS — grace period before health check failures → ERROR

  Workspace persistence (pick one):
  SANDBOX_K8S_WORKSPACE_PVC_TEMPLATE — path to a PVC YAML template for per-sandbox volumes.
      Template variables: ${sandbox_id}, ${namespace}. Default ships with EBS gp3 / 10Gi.
  SANDBOX_K8S_WORKSPACE_PVC  — (legacy) name of a shared PVC mounted with per-sandbox subPaths.

  Secret volume mounts:
  SANDBOX_K8S_SECRET_VOLUMES — JSON array of secret volume mounts. Each entry:
      {"secret": "secret-name", "mountPath": "/path", "defaultMode": 0o400}
      Example: '[{"secret":"git-deploy-key","mountPath":"/root/.ssh","defaultMode":256}]'
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import yaml

from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    ExposedUrl,
    SandboxInfo,
    SandboxPage,
    SandboxStatus,
)

# Internal URL entry — used by the server for server-to-sandbox API calls
# (avoids routing through the ALB which may require OIDC auth)
AGENT_SERVER_INTERNAL = "AGENT_SERVER_INTERNAL"
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
_DEFAULT_PVC_TEMPLATE = Path(__file__).parent / "workspace-pvc-template.yaml"


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
            os.getenv("SANDBOX_STARTUP_GRACE_SECONDS", "300")
        )
        self.health_check_path = health_check_path

        # IRSA role ARN for per-sandbox ServiceAccounts (Bedrock access, etc.)
        self.sandbox_irsa_role_arn = os.getenv("SANDBOX_K8S_SA_IRSA_ROLE_ARN", "")

        # Git repo to clone into /workspace/code/ via init container
        self.sandbox_git_repo = os.getenv("SANDBOX_GIT_REPO", "")

        # EFS filesystem ID for shared code volumes (companion dev environments)
        self.efs_filesystem_id = os.getenv("SANDBOX_K8S_EFS_FILESYSTEM_ID", "")

        # External URL for sandbox Ingress routes
        self.external_host = os.getenv("SANDBOX_K8S_EXTERNAL_HOST", "")
        self.ingress_class = os.getenv("SANDBOX_K8S_INGRESS_CLASS", "alb-external")
        self.ingress_group = os.getenv("SANDBOX_K8S_INGRESS_GROUP", "")

        # Initialize K8s client
        try:
            k8s_config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
            logger.info("Loaded kubeconfig from default location")

        self._batch_v1 = client.BatchV1Api()
        self._core_v1 = client.CoreV1Api()
        self._networking_v1 = client.NetworkingV1Api()

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

    def _sandbox_sa_name(self, sandbox_id: str) -> str:
        return f"sandbox-{sandbox_id}"

    def _ensure_sandbox_sa(self, sandbox_id: str) -> str:
        """Create a per-sandbox ServiceAccount with IRSA annotation. Returns the SA name."""
        sa_name = self._sandbox_sa_name(sandbox_id)

        annotations = {}
        if self.sandbox_irsa_role_arn:
            annotations["eks.amazonaws.com/role-arn"] = self.sandbox_irsa_role_arn

        sa = client.V1ServiceAccount(
            metadata=client.V1ObjectMeta(
                name=sa_name,
                namespace=self.namespace,
                labels={
                    _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
                    _LABEL_SANDBOX_ID: sandbox_id,
                },
                annotations=annotations,
            ),
        )

        try:
            self._core_v1.create_namespaced_service_account(
                namespace=self.namespace, body=sa,
            )
            logger.info("Created sandbox SA %s", sa_name)
        except ApiException as e:
            if e.status == 409:
                logger.info("Sandbox SA %s already exists", sa_name)
            else:
                raise

        return sa_name

    def _ingress_name(self, sandbox_id: str) -> str:
        return f"oh-sandbox-{sandbox_id}"

    def _sandbox_path_prefix(self, sandbox_id: str) -> str:
        return f"/sandbox/{sandbox_id}"

    def _sandbox_external_url(self, sandbox_id: str) -> str | None:
        """Build the externally-routable URL for a sandbox, or None if not configured."""
        if not self.external_host:
            return None
        return f"https://{self.external_host}{self._sandbox_path_prefix(sandbox_id)}"

    def _get_agent_server_url(self, sandbox: SandboxInfo) -> str:
        """Get agent server URL, preferring the internal K8s service URL.

        The base class uses AGENT_SERVER (external/ALB URL) which may have
        path prefixes that the agent-server doesn't handle. For in-cluster
        health checks, the internal URL is more reliable.
        """
        from openhands.app_server.errors import SandboxError

        if not sandbox.exposed_urls:
            raise SandboxError(f'No exposed URLs for sandbox: {sandbox.id}')

        # Prefer internal URL for in-cluster communication
        for exposed_url in sandbox.exposed_urls:
            if exposed_url.name == AGENT_SERVER_INTERNAL:
                return exposed_url.url

        # Fall back to external URL
        for exposed_url in sandbox.exposed_urls:
            if exposed_url.name == AGENT_SERVER:
                return exposed_url.url

        raise SandboxError(f'No agent server URL found for sandbox: {sandbox.id}')

    def _create_sandbox_ingress(
        self,
        sandbox_id: str,
        labels: dict[str, str],
        owner_ref: client.V1OwnerReference | None = None,
    ) -> None:
        """Create a per-sandbox Ingress that routes through the shared ALB."""
        path_prefix = self._sandbox_path_prefix(sandbox_id)
        ingress = client.V1Ingress(
            api_version="networking.k8s.io/v1",
            kind="Ingress",
            metadata=client.V1ObjectMeta(
                name=self._ingress_name(sandbox_id),
                namespace=self.namespace,
                labels=labels,
                owner_references=[owner_ref] if owner_ref else None,
                annotations={
                    "alb.ingress.kubernetes.io/group.name": self.ingress_group,
                    # No OIDC auth — sandbox uses session API key
                    "alb.ingress.kubernetes.io/auth-type": "none",
                    # Healthcheck against the sandbox pod (with path prefix)
                    "alb.ingress.kubernetes.io/healthcheck-path": f"{path_prefix}/health",
                },
            ),
            spec=client.V1IngressSpec(
                ingress_class_name=self.ingress_class,
                rules=[
                    client.V1IngressRule(
                        host=self.external_host,
                        http=client.V1HTTPIngressRuleValue(
                            paths=[
                                client.V1HTTPIngressPath(
                                    path=f"{path_prefix}/",
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name=self._service_name(sandbox_id),
                                            port=client.V1ServiceBackendPort(
                                                number=_DEFAULT_PORT,
                                            ),
                                        ),
                                    ),
                                ),
                                client.V1HTTPIngressPath(
                                    path=f"{path_prefix}/vscode/",
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name=self._service_name(sandbox_id),
                                            port=client.V1ServiceBackendPort(
                                                number=8001,
                                            ),
                                        ),
                                    ),
                                ),
                            ],
                        ),
                    ),
                ],
            ),
        )
        try:
            self._networking_v1.create_namespaced_ingress(
                namespace=self.namespace,
                body=ingress,
            )
            logger.info("Created sandbox Ingress %s (path=%s/)", sandbox_id, path_prefix)
        except ApiException as e:
            logger.warning("Failed to create sandbox Ingress (non-fatal): %s", e)

    def _get_job_owner_ref(self, sandbox_id: str) -> client.V1OwnerReference | None:
        """Look up the sandbox Job and return an ownerReference for it."""
        try:
            job = self._batch_v1.read_namespaced_job(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
            )
            return client.V1OwnerReference(
                api_version="batch/v1",
                kind="Job",
                name=job.metadata.name,
                uid=job.metadata.uid,
                block_owner_deletion=False,
            )
        except ApiException:
            return None

    def _ensure_sandbox_ingress(self, sandbox_id: str, labels: dict[str, str]) -> None:
        """Ensure Ingress exists for a running sandbox (idempotent)."""
        try:
            self._networking_v1.read_namespaced_ingress(
                name=self._ingress_name(sandbox_id),
                namespace=self.namespace,
            )
        except ApiException as e:
            if e.status == 404:
                logger.info("Sandbox Ingress %s missing — recreating", sandbox_id)
                owner_ref = self._get_job_owner_ref(sandbox_id)
                self._create_sandbox_ingress(sandbox_id, labels, owner_ref=owner_ref)
            else:
                logger.warning("Failed to check sandbox Ingress %s: %s", sandbox_id, e)

    def _delete_sandbox_ingress(self, sandbox_id: str) -> None:
        """Delete the per-sandbox Ingress."""
        try:
            self._networking_v1.delete_namespaced_ingress(
                name=self._ingress_name(sandbox_id),
                namespace=self.namespace,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete sandbox Ingress %s: %s", sandbox_id, e)

    def _pvc_name(self, sandbox_id: str) -> str:
        return f"oh-workspace-{sandbox_id}"

    def _load_pvc_template(self) -> str | None:
        """Load the PVC template string, or None if per-sandbox PVCs are disabled."""
        template_path = os.getenv("SANDBOX_K8S_WORKSPACE_PVC_TEMPLATE")
        if template_path == "default":
            template_path = str(_DEFAULT_PVC_TEMPLATE)
        if not template_path:
            return None
        path = Path(template_path)
        if not path.is_file():
            logger.error("PVC template not found: %s", path)
            return None
        return path.read_text()

    def _ensure_workspace_pvc(self, sandbox_id: str) -> str | None:
        """Provision a per-sandbox PVC from the template if it doesn't exist.

        Returns the PVC name to mount, or None if per-sandbox PVCs are disabled.
        """
        template_str = self._load_pvc_template()
        if template_str is None:
            return None

        rendered = Template(template_str).safe_substitute(
            sandbox_id=sandbox_id,
            namespace=self.namespace,
        )
        pvc_manifest = yaml.safe_load(rendered)
        pvc_name = pvc_manifest["metadata"]["name"]

        # Check if PVC already exists (conversation revival)
        try:
            self._core_v1.read_namespaced_persistent_volume_claim(
                name=pvc_name,
                namespace=self.namespace,
            )
            logger.info("Workspace PVC %s already exists — reusing", pvc_name)
            return pvc_name
        except ApiException as e:
            if e.status != 404:
                logger.error("Failed to check workspace PVC %s: %s", pvc_name, e)
                return None

        # Create PVC
        try:
            self._core_v1.create_namespaced_persistent_volume_claim(
                namespace=self.namespace,
                body=pvc_manifest,
            )
            logger.info("Created workspace PVC %s for sandbox %s", pvc_name, sandbox_id)
            return pvc_name
        except ApiException as e:
            if e.status == 409:
                # Race condition — another call created it first
                logger.info("Workspace PVC %s created concurrently — reusing", pvc_name)
                return pvc_name
            logger.error("Failed to create workspace PVC %s: %s", pvc_name, e)
            return None

    def _ensure_efs_directory(self, sandbox_id: str) -> None:
        """Run a short-lived Job to create the EFS subdirectory for this sandbox.

        The EFS CSI driver requires the subdirectory to exist before a PV can
        reference it via volume_handle.  This mounts the EFS root (no subpath)
        and runs ``mkdir -p /{sandbox_id}``.
        """
        import time as _time

        job_name = f"efs-init-{sandbox_id[:16]}"

        # Check if Job already completed
        try:
            job = self._batch_v1.read_namespaced_job(job_name, self.namespace)
            if job.status.succeeded and job.status.succeeded > 0:
                logger.info("EFS init job %s already completed", job_name)
                return
        except client.ApiException as e:
            if e.status != 404:
                raise

        # Temporary PV/PVC for EFS root (no subpath)
        root_pv_name = f"efs-root-{sandbox_id[:16]}"
        root_pvc_name = f"efs-root-{sandbox_id[:16]}"

        try:
            self._core_v1.read_persistent_volume(root_pv_name)
        except client.ApiException as e:
            if e.status == 404:
                self._core_v1.create_persistent_volume(client.V1PersistentVolume(
                    metadata=client.V1ObjectMeta(name=root_pv_name),
                    spec=client.V1PersistentVolumeSpec(
                        capacity={"storage": "1Gi"},
                        volume_mode="Filesystem",
                        access_modes=["ReadWriteMany"],
                        persistent_volume_reclaim_policy="Delete",
                        storage_class_name="",
                        csi=client.V1CSIPersistentVolumeSource(
                            driver="efs.csi.aws.com",
                            volume_handle=self.efs_filesystem_id,
                        ),
                    ),
                ))
            else:
                raise

        try:
            self._core_v1.read_namespaced_persistent_volume_claim(root_pvc_name, self.namespace)
        except client.ApiException as e:
            if e.status == 404:
                self._core_v1.create_namespaced_persistent_volume_claim(
                    self.namespace,
                    client.V1PersistentVolumeClaim(
                        metadata=client.V1ObjectMeta(name=root_pvc_name, namespace=self.namespace),
                        spec=client.V1PersistentVolumeClaimSpec(
                            access_modes=["ReadWriteMany"],
                            storage_class_name="",
                            volume_name=root_pv_name,
                            resources=client.V1VolumeResourceRequirements(
                                requests={"storage": "1Gi"},
                            ),
                        ),
                    ),
                )
            else:
                raise

        # Create the mkdir Job
        try:
            self._batch_v1.create_namespaced_job(self.namespace, client.V1Job(
                metadata=client.V1ObjectMeta(name=job_name, namespace=self.namespace),
                spec=client.V1JobSpec(
                    ttl_seconds_after_finished=60,
                    template=client.V1PodTemplateSpec(
                        spec=client.V1PodSpec(
                            restart_policy="Never",
                            containers=[client.V1Container(
                                name="init",
                                image="busybox:latest",
                                command=["sh", "-c", f"mkdir -p /efs/{sandbox_id} && echo done"],
                                volume_mounts=[client.V1VolumeMount(
                                    name="efs-root", mount_path="/efs",
                                )],
                            )],
                            volumes=[client.V1Volume(
                                name="efs-root",
                                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                    claim_name=root_pvc_name,
                                ),
                            )],
                        ),
                    ),
                ),
            ))
            logger.info("Created EFS init job %s", job_name)
        except client.ApiException as e:
            if e.status != 409:
                raise

        # Wait for completion (up to 2 minutes)
        for _ in range(60):
            _time.sleep(2)
            job = self._batch_v1.read_namespaced_job(job_name, self.namespace)
            if job.status.succeeded and job.status.succeeded > 0:
                logger.info("EFS init job %s completed", job_name)
                break
            if job.status.failed and job.status.failed > 0:
                raise RuntimeError(f"EFS init job {job_name} failed")
        else:
            raise RuntimeError(f"EFS init job {job_name} timed out")

        # Clean up temporary root PV/PVC
        try:
            self._core_v1.delete_namespaced_persistent_volume_claim(root_pvc_name, self.namespace)
            self._core_v1.delete_persistent_volume(root_pv_name)
        except client.ApiException:
            pass  # Best effort

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

            # Build the cluster-internal URL (Service DNS or pod IP)
            internal_url: str | None = None
            svc_name = self._service_name(sandbox_id)
            internal_url = f"http://{svc_name}.{self.namespace}.svc.cluster.local:{_DEFAULT_PORT}"

            # Ensure Ingress exists for running sandboxes (idempotent).
            # Ingresses can be lost during server restarts or cleanup races.
            if self.external_host and self.ingress_group:
                self._ensure_sandbox_ingress(sandbox_id, job.metadata.labels or {})

            # Use external URL (via Ingress) for frontend WebSocket connections,
            # and internal URL for server-to-sandbox API calls.
            external_url = self._sandbox_external_url(sandbox_id)
            if external_url:
                exposed_urls = [
                    ExposedUrl(
                        name=AGENT_SERVER,
                        url=external_url,
                        port=443,
                    ),
                    ExposedUrl(
                        name=AGENT_SERVER_INTERNAL,
                        url=internal_url,
                        port=_DEFAULT_PORT,
                    ),
                ]
            else:
                exposed_urls = [
                    ExposedUrl(
                        name=AGENT_SERVER,
                        url=internal_url,
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
                # Job is gone (TTL cleanup, manual delete, etc.)
                # Only auto-recreate if a workspace PVC exists for this sandbox,
                # meaning it had real work worth recovering.  This prevents the
                # recreation storm on server restart (old conversations without
                # PVCs are left alone).
                pvc_name = f"oh-workspace-{sandbox_id}"
                try:
                    self._core_v1.read_namespaced_persistent_volume_claim(
                        name=pvc_name, namespace=self.namespace,
                    )
                except ApiException:
                    logger.debug("Sandbox Job %s not found (no PVC) — skipping", sandbox_id)
                    return None

                logger.info(
                    "Sandbox Job %s not found but PVC exists — recreating",
                    sandbox_id,
                )
                return await self._recreate_sandbox(sandbox_id)
            logger.error("Failed to get sandbox Job %s: %s", sandbox_id, e)
            return None

        return self._job_to_sandbox_info(job)

    async def batch_get_sandboxes(
        self, sandbox_ids: list[str],
    ) -> list[SandboxInfo | None]:
        """Get sandbox info for multiple IDs without triggering auto-recreation.

        Overrides the base class which calls get_sandbox() per ID (and would
        auto-recreate missing sandboxes).  This method only returns info for
        sandboxes that actually have running/suspended Jobs.
        """
        results: list[SandboxInfo | None] = []
        for sandbox_id in sandbox_ids:
            try:
                job = self._batch_v1.read_namespaced_job(
                    name=self._job_name(sandbox_id),
                    namespace=self.namespace,
                )
                results.append(self._job_to_sandbox_info(job))
            except ApiException as e:
                if e.status == 404:
                    results.append(None)
                else:
                    logger.error("Failed to get sandbox Job %s: %s", sandbox_id, e)
                    results.append(None)
        return results

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

    async def _recreate_sandbox(self, sandbox_id: str) -> SandboxInfo:
        """Delete stale resources for a sandbox and recreate it with the same ID.

        This enables reconnection: when a user refreshes the page and the
        original sandbox Job is gone (TTL cleanup, crash, etc.), we create a
        fresh sandbox so the conversation can resume.

        The new sandbox gets a new session API key, but the upstream router
        reads the key from the SandboxInfo returned by get_sandbox(), so auth
        stays consistent.
        """
        # Clean up any leftover resources from the old sandbox
        try:
            self._batch_v1.delete_namespaced_job(
                name=self._job_name(sandbox_id),
                namespace=self.namespace,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete old Job %s during recreate: %s", sandbox_id, e)

        try:
            self._core_v1.delete_namespaced_service(
                name=self._service_name(sandbox_id),
                namespace=self.namespace,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete old Service %s during recreate: %s", sandbox_id, e)

        self._delete_sandbox_ingress(sandbox_id)

        # Small delay to let K8s propagate the deletion (especially for Jobs
        # that still exist in a terminal state)
        await asyncio.sleep(1)

        # Recreate with the same sandbox_id so the conversation record still matches
        try:
            return await self.start_sandbox(sandbox_id=sandbox_id)
        except RuntimeError as e:
            logger.error("Failed to recreate sandbox %s: %s", sandbox_id, e)
            return None

    async def start_sandbox(
        self,
        sandbox_spec_id: str | None = None,
        sandbox_id: str | None = None,
    ) -> SandboxInfo:
        if sandbox_id is None:
            sandbox_id = secrets.token_hex(8)

        session_api_key = _generate_session_key()
        key_hash = _hash_session_key(session_api_key)

        image = os.getenv("SANDBOX_K8S_IMAGE") or sandbox_spec_id or get_agent_server_image()

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

        # Forward SANDBOX_K8S_ENV_* vars as plain env vars on sandbox pods.
        # These are non-secret configuration (e.g. GITLAB_HOST) set in the
        # main server deployment. Secret values are injected via envFrom
        # referencing the K8s Secret named in SANDBOX_K8S_ENV_SECRET.
        _ENV_PREFIX = "SANDBOX_K8S_ENV_"
        for key, value in os.environ.items():
            if key.startswith(_ENV_PREFIX) and key != "SANDBOX_K8S_ENV_SECRET" and value:
                env_vars[key[len(_ENV_PREFIX):]] = value

        # Webhook callback URL (for the main app server)
        app_server_host = os.getenv("SANDBOX_K8S_APP_SERVER_HOST")
        if app_server_host:
            env_vars[_WEBHOOK_CALLBACK_VAR] = f"{app_server_host}/api/v1/webhooks"

        # CORS origins — include the external host so browser can reach sandbox
        cors_origins = [o.strip() for o in os.getenv("OH_ALLOW_CORS_ORIGINS", "").split(",") if o.strip()]
        if self.external_host:
            cors_origins.append(f"https://{self.external_host}")
        for idx, origin in enumerate(cors_origins):
            env_vars[f"OH_ALLOW_CORS_ORIGINS_{idx}"] = origin

        # Set the external URL so the agent-server configures root_path for
        # path-based reverse proxy routing (ALB Ingress with /sandbox/<id>/ prefix)
        external_url = self._sandbox_external_url(sandbox_id)
        if external_url:
            env_vars["OH_WEB_URL"] = external_url

        # VSCode server base path for path-based routing through ALB
        if self.external_host:
            path_prefix = self._sandbox_path_prefix(sandbox_id)
            env_vars["OH_VSCODE_BASE_PATH"] = f"{path_prefix}/vscode"

        # Expose the sandbox ID so tools can identify this sandbox uniquely.
        # NOTE: OPENHANDS_CONVERSATION_URL is also set but uses the sandbox_id
        # in the URL path (the real conversation_id isn't known at sandbox
        # creation time — it's assigned by the app server afterward).
        env_vars["OPENHANDS_SANDBOX_ID"] = sandbox_id
        if self.external_host:
            env_vars["OPENHANDS_CONVERSATION_URL"] = f"https://{self.external_host}/conversations/{sandbox_id}"

        # Path to projected env-manager authentication token
        env_vars["ENV_MANAGER_TOKEN_PATH"] = "/var/run/secrets/env-manager/token"

        # Broker service URL and token path for git credential helper
        broker_url = os.getenv("SANDBOX_BROKER_URL", "http://openhands-broker.openhands.svc.cluster.local:8080")
        env_vars["BROKER_URL"] = broker_url
        env_vars["BROKER_TOKEN_PATH"] = "/var/run/secrets/broker/token"

        # NIX_PACKAGES for dynamic Nix package installation
        nix_packages = os.getenv("SANDBOX_NIX_PACKAGES", "")
        if nix_packages:
            env_vars["NIX_PACKAGES"] = nix_packages
            # Allow unfree packages (e.g. terraform with BSL license)
            env_vars["NIXPKGS_ALLOW_UNFREE"] = "1"

        # Build Job labels
        labels = {
            _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
            _LABEL_SANDBOX_ID: sandbox_id,
            _LABEL_SESSION_KEY_HASH: key_hash,
            "openhands.ai/spec-id": image.split("/")[-1].split(":")[0],
        }

        # Volume mounts for the container
        volume_mounts = []

        # Workspace persistence: per-sandbox PVC (template) or shared PVC (legacy)
        workspace_pvc = self._ensure_workspace_pvc(sandbox_id)
        if workspace_pvc:
            # Per-sandbox PVC — mount the whole volume (no subPath needed)
            volume_mounts.append(
                client.V1VolumeMount(
                    name="workspace",
                    mount_path="/workspace",
                )
            )
        else:
            # Legacy: shared PVC with per-sandbox subPath
            shared_pvc = os.getenv("SANDBOX_K8S_WORKSPACE_PVC")
            if shared_pvc:
                workspace_pvc = shared_pvc
                volume_mounts.append(
                    client.V1VolumeMount(
                        name="workspace",
                        mount_path="/workspace",
                        sub_path=f"sandboxes/{sandbox_id}",
                    )
                )

        # Code volume: EFS-backed PVC for sharing code with companion environments.
        # Created here at sandbox start so the init container can clone directly into EFS.
        # The env-manager later creates a matching PVC in the app namespace pointing to
        # the same EFS path, giving the companion env immediate access to the code.
        code_mount_path = os.getenv("SANDBOX_K8S_CODE_MOUNT_PATH", "/workspace/code")
        code_pvc_name: str | None = None
        if self.efs_filesystem_id:
            # Ensure the EFS subdirectory exists before creating the PV
            self._ensure_efs_directory(sandbox_id)

            code_pvc_name = f"code-{sandbox_id}"
            code_pv_name = f"code-{sandbox_id}-sandbox"
            efs_path = f"/{sandbox_id}"
            try:
                self._core_v1.read_persistent_volume(code_pv_name)
            except client.ApiException as e:
                if e.status == 404:
                    self._core_v1.create_persistent_volume(client.V1PersistentVolume(
                        metadata=client.V1ObjectMeta(name=code_pv_name),
                        spec=client.V1PersistentVolumeSpec(
                            capacity={"storage": "10Gi"},
                            volume_mode="Filesystem",
                            access_modes=["ReadWriteMany"],
                            persistent_volume_reclaim_policy="Retain",
                            storage_class_name="",
                            csi=client.V1CSIPersistentVolumeSource(
                                driver="efs.csi.aws.com",
                                volume_handle=f"{self.efs_filesystem_id}:{efs_path}",
                            ),
                        ),
                    ))
                    logger.info(f"Created EFS PV {code_pv_name}")
                else:
                    raise
            try:
                self._core_v1.read_namespaced_persistent_volume_claim(code_pvc_name, self.namespace)
            except client.ApiException as e:
                if e.status == 404:
                    self._core_v1.create_namespaced_persistent_volume_claim(
                        self.namespace,
                        client.V1PersistentVolumeClaim(
                            metadata=client.V1ObjectMeta(
                                name=code_pvc_name,
                                namespace=self.namespace,
                                labels={
                                    "openhands.ai/sandbox-id": sandbox_id,
                                    "openhands.ai/purpose": "code-volume",
                                },
                            ),
                            spec=client.V1PersistentVolumeClaimSpec(
                                access_modes=["ReadWriteMany"],
                                storage_class_name="",
                                volume_name=code_pv_name,
                                resources=client.V1VolumeResourceRequirements(
                                    requests={"storage": "10Gi"},
                                ),
                            ),
                        ),
                    )
                    logger.info(f"Created EFS PVC {code_pvc_name}")
                else:
                    raise
            volume_mounts.append(
                client.V1VolumeMount(name="code", mount_path=code_mount_path)
            )
            logger.info(f"Mounting code volume {code_pvc_name} at {code_mount_path}")

        # Forward code mount path to sandbox so agents know where code lives
        env_vars["CODE_MOUNT_PATH"] = code_mount_path

        # Tmpfs mounts for Chromium browser support
        volume_mounts.append(
            client.V1VolumeMount(name="tmp", mount_path="/tmp")
        )
        volume_mounts.append(
            client.V1VolumeMount(name="dshm", mount_path="/dev/shm")
        )

        # Projected volume mount for env-manager authentication token
        volume_mounts.append(
            client.V1VolumeMount(
                name="env-manager-token",
                mount_path="/var/run/secrets/env-manager",
                read_only=True,
            )
        )

        # Projected volume mount for broker authentication token
        volume_mounts.append(
            client.V1VolumeMount(
                name="broker-token",
                mount_path="/var/run/secrets/broker",
                read_only=True,
            )
        )

        # Secret volume mounts (SSH keys, config files, etc.)
        secret_volumes_json = os.getenv("SANDBOX_K8S_SECRET_VOLUMES", "")
        secret_volume_specs: list[dict] = []
        if secret_volumes_json:
            try:
                secret_volume_specs = json.loads(secret_volumes_json)
            except json.JSONDecodeError:
                logger.warning("Invalid SANDBOX_K8S_SECRET_VOLUMES JSON, ignoring")

        for i, sv in enumerate(secret_volume_specs):
            vol_name = f"secret-{i}"
            volume_mounts.append(
                client.V1VolumeMount(
                    name=vol_name,
                    mount_path=sv["mountPath"],
                    read_only=sv.get("readOnly", True),
                )
            )

        # Secret-based env vars (tokens, credentials) — injected via envFrom
        # so secret values never appear in the Job spec.
        env_from: list | None = None
        env_secret = os.getenv("SANDBOX_K8S_ENV_SECRET")
        if env_secret:
            env_from = [
                client.V1EnvFromSource(
                    secret_ref=client.V1SecretEnvSource(
                        name=env_secret,
                        optional=True,
                    ),
                ),
            ]

        # Container spec
        container = client.V1Container(
            name="agent-server",
            image=image,
            image_pull_policy=os.getenv("SANDBOX_K8S_IMAGE_PULL_POLICY", "IfNotPresent"),
            ports=[client.V1ContainerPort(container_port=_DEFAULT_PORT, name="http")],
            env=[client.V1EnvVar(name=k, value=v) for k, v in env_vars.items()],
            env_from=env_from,
            volume_mounts=volume_mounts or None,
            resources=client.V1ResourceRequirements(
                requests=_env_json("SANDBOX_K8S_RESOURCE_REQUESTS", {"cpu": "250m", "memory": "512Mi"}),
                limits=_env_json("SANDBOX_K8S_RESOURCE_LIMITS") or None,
            ),
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/health", port=_DEFAULT_PORT),
                initial_delay_seconds=10,
                period_seconds=5,
                timeout_seconds=3,
                failure_threshold=60,  # 60 * 5s = 300s max
            ),
            # No liveness probe — sandbox agent-servers can become
            # unresponsive during heavy LLM calls, git clones, or nix
            # builds.  A liveness probe would kill the pod mid-task.
            # The readiness probe is sufficient for routing.
        )

        # Volumes
        volumes = []
        if workspace_pvc:
            volumes.append(
                client.V1Volume(
                    name="workspace",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=workspace_pvc,
                    ),
                )
            )
        # Code volume (EFS, if discovered above)
        if code_pvc_name:
            volumes.append(
                client.V1Volume(
                    name="code",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=code_pvc_name,
                    ),
                )
            )
        # Secret volumes
        for i, sv in enumerate(secret_volume_specs):
            vol_name = f"secret-{i}"
            volumes.append(
                client.V1Volume(
                    name=vol_name,
                    secret=client.V1SecretVolumeSource(
                        secret_name=sv["secret"],
                        default_mode=sv.get("defaultMode", 0o400),
                    ),
                )
            )

        # Tmpfs volumes for Chromium browser support.
        # /tmp must be writable for shared memory fallback (--disable-dev-shm-usage).
        # /dev/shm needs >64M for Chromium's shared memory requirements.
        volumes.append(
            client.V1Volume(
                name="tmp",
                empty_dir=client.V1EmptyDirVolumeSource(medium="Memory"),
            )
        )
        volumes.append(
            client.V1Volume(
                name="dshm",
                empty_dir=client.V1EmptyDirVolumeSource(
                    medium="Memory", size_limit="512Mi"
                ),
            )
        )

        # Projected volume for env-manager audience-scoped token
        volumes.append(
            client.V1Volume(
                name="env-manager-token",
                projected=client.V1ProjectedVolumeSource(
                    sources=[
                        client.V1VolumeProjection(
                            service_account_token=client.V1ServiceAccountTokenProjection(
                                audience="env-manager",
                                expiration_seconds=3600,
                                path="token",
                            )
                        )
                    ],
                ),
            )
        )

        # Projected volume for broker audience-scoped token
        volumes.append(
            client.V1Volume(
                name="broker-token",
                projected=client.V1ProjectedVolumeSource(
                    sources=[
                        client.V1VolumeProjection(
                            service_account_token=client.V1ServiceAccountTokenProjection(
                                audience="openhands-broker",
                                expiration_seconds=3600,
                                path="token",
                            )
                        )
                    ],
                ),
            )
        )

        # Init container: clone git repo into workspace before main container starts
        init_containers = []
        if self.sandbox_git_repo:
            # Build volume mounts for the init container — needs workspace + SSH keys + code volume
            init_mounts = []
            if workspace_pvc:
                init_mounts.append(
                    client.V1VolumeMount(name="workspace", mount_path="/workspace")
                )
            if code_pvc_name:
                init_mounts.append(
                    client.V1VolumeMount(name="code", mount_path=code_mount_path)
                )
            for i, sv in enumerate(secret_volume_specs):
                init_mounts.append(
                    client.V1VolumeMount(
                        name=f"secret-{i}",
                        mount_path=sv["mountPath"],
                        read_only=sv.get("readOnly", True),
                    )
                )
            init_containers.append(
                client.V1Container(
                    name="git-clone",
                    image="debian:bookworm-slim",
                    command=["sh", "-c",
                        "apt-get update -qq && apt-get install -y -qq git openssh-client >/dev/null 2>&1 && "
                        "mkdir -p /tmp/ssh && cp -L /root/.ssh/* /tmp/ssh/ && chmod 700 /tmp/ssh && "
                        "for f in /tmp/ssh/*; do chmod 600 \"$f\"; printf '\\n' >> \"$f\"; done && "
                        "if [ -d /workspace/code/.git ]; then "
                        "echo 'Repo already cloned, skipping'; "
                        "else "
                        "rm -rf /workspace/code/* /workspace/code/.* 2>/dev/null || true; "
                        "GIT_SSH_COMMAND='ssh -i /tmp/ssh/id_ed25519 -o StrictHostKeyChecking=no' "
                        f"git clone {self.sandbox_git_repo} /workspace/code; "
                        "fi"
                    ],
                    volume_mounts=init_mounts or None,
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "100m", "memory": "256Mi"},
                    ),
                )
            )

        # Pod spec
        pod_spec = client.V1PodSpec(
            init_containers=init_containers or None,
            containers=[container],
            volumes=volumes or None,
            restart_policy="Never",
        )

        # Per-sandbox ServiceAccount with IRSA annotation and projected token
        sa_name = self._ensure_sandbox_sa(sandbox_id)
        pod_spec.service_account_name = sa_name

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
                    metadata=client.V1ObjectMeta(
                        labels=labels,
                        annotations={"karpenter.sh/do-not-disrupt": "true"},
                    ),
                    spec=pod_spec,
                ),
            ),
        )

        try:
            created_job = self._batch_v1.create_namespaced_job(
                namespace=self.namespace,
                body=job,
            )
            logger.info("Created sandbox Job %s (image=%s)", sandbox_id, image)
        except ApiException as e:
            raise RuntimeError(f"Failed to create sandbox Job: {e}") from e

        # ownerReference so Service/Ingress are GC'd when the Job is TTL-cleaned.
        # PVCs are intentionally NOT owned — they persist across sandbox restarts.
        owner_ref = client.V1OwnerReference(
            api_version="batch/v1",
            kind="Job",
            name=created_job.metadata.name,
            uid=created_job.metadata.uid,
            block_owner_deletion=False,
        )

        # Create a ClusterIP Service for stable networking
        service = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(
                name=self._service_name(sandbox_id),
                namespace=self.namespace,
                labels=labels,
                owner_references=[owner_ref],
            ),
            spec=client.V1ServiceSpec(
                selector={_LABEL_SANDBOX_ID: sandbox_id},
                ports=[
                    client.V1ServicePort(port=_DEFAULT_PORT, target_port=_DEFAULT_PORT, name="http"),
                    client.V1ServicePort(port=8001, target_port=8001, name="vscode"),
                ],
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

        # Create an Ingress for external access (if external_host is configured)
        if self.external_host and self.ingress_group:
            self._create_sandbox_ingress(sandbox_id, labels, owner_ref=owner_ref)

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
                # Job is gone — check if PVC still exists and recreate
                pvc_name = f"oh-workspace-{sandbox_id}"
                try:
                    self._core_v1.read_namespaced_persistent_volume_claim(
                        name=pvc_name, namespace=self.namespace,
                    )
                except ApiException:
                    return False
                logger.info(
                    "Resume: Job %s gone but PVC exists — recreating",
                    sandbox_id,
                )
                info = await self._recreate_sandbox(sandbox_id)
                return info is not None
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
            # Recreate the Ingress that was removed during pause
            if self.external_host and self.ingress_group:
                self._ensure_sandbox_ingress(sandbox_id, job.metadata.labels or {})
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
            # Remove the Ingress while paused — recreated on resume
            self._delete_sandbox_ingress(sandbox_id)
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

        # Delete the Ingress (if it exists)
        self._delete_sandbox_ingress(sandbox_id)

        # Delete the per-sandbox ServiceAccount
        try:
            self._core_v1.delete_namespaced_service_account(
                name=self._sandbox_sa_name(sandbox_id),
                namespace=self.namespace,
            )
            logger.info("Deleted sandbox SA %s", sandbox_id)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete sandbox SA %s: %s", sandbox_id, e)

        # Delete EFS code volume PVC and PV (if they exist)
        code_pvc = f"code-{sandbox_id}"
        code_pv = f"code-{sandbox_id}-sandbox"
        try:
            self._core_v1.delete_namespaced_persistent_volume_claim(code_pvc, self.namespace)
            logger.info("Deleted code PVC %s", code_pvc)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete code PVC %s: %s", code_pvc, e)
        try:
            self._core_v1.delete_persistent_volume(code_pv)
            logger.info("Deleted code PV %s", code_pv)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete code PV %s: %s", code_pv, e)

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
        # Always return the default spec — sandbox_spec_id from Job labels may
        # be a short name (e.g. "agent-server") rather than the full image URL.
        return self._default_spec


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
