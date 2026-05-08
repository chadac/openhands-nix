"""KubernetesSandboxService: manage agent-server sandboxes as K8s Jobs.

This module implements the SandboxService and SandboxSpecService interfaces
from the OpenHands V1 app server, backed by Kubernetes Jobs.

Each sandbox is a K8s Job running a single pod with the agent-server image.
Resources (ServiceAccount, PVC, Job, Service, Ingress) are declared via
kubenix modules and rendered at runtime by calling ``nix eval`` on
``sandbox-eval/eval.nix``.

Configuration via environment variables:
  SANDBOX_K8S_NAMESPACE       — namespace for sandbox Jobs (default: "openhands")
  SANDBOX_K8S_IMAGE           — agent-server image (default: from get_agent_server_image())
  SANDBOX_HOST_PORT           — port of the main app server (for webhook callbacks)
  SANDBOX_STARTUP_GRACE_SECONDS — grace period before health check failures → ERROR

  SANDBOX_EVAL_NIX            — path to eval.nix (sandbox evaluator entrypoint)
  KUBENIX_FLAKE               — flake reference for kubenix
  SANDBOX_EXTRA_MODULES       — colon-separated list of extra kubenix module paths
  SANDBOX_K8S_TEMPLATE_CONFIG — JSON object with sandbox.* option overrides
  SANDBOX_HOOKS               — colon-separated list of hook executable paths
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import string
import subprocess
import tempfile
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

# Internal URL entry — used by the server for server-to-sandbox API calls
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

_LABEL_MANAGED_BY = "openhands.ai/managed-by"
_LABEL_MANAGED_BY_VALUE = "openhands-nix-kubernetes"
_LABEL_SANDBOX_ID = "openhands.ai/sandbox-id"
_LABEL_SESSION_KEY_HASH = "openhands.ai/session-key-hash"
_LABEL_PRESERVE = "openhands.ai/preserve"

_SESSION_API_KEY_VAR = "OH_SESSION_API_KEYS_0"
_DEFAULT_PORT = 8000


def _generate_session_key() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(43))


def _hash_session_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _env_json(var: str, default: Any = None) -> Any:
    raw = os.getenv(var)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid JSON in %s: %s", var, raw)
        return default


def _pod_phase_to_status(
    phase: str | None, container_statuses: list | None = None,
) -> SandboxStatus:
    if phase == "Running":
        if container_statuses:
            for cs in container_statuses:
                if cs.state and cs.state.waiting:
                    return SandboxStatus.STARTING
        return SandboxStatus.RUNNING
    elif phase == "Pending":
        return SandboxStatus.STARTING
    elif phase in ("Failed", "Unknown"):
        return SandboxStatus.ERROR
    elif phase == "Succeeded":
        return SandboxStatus.PAUSED
    return SandboxStatus.STARTING


def _run_hooks(hooks: list[str], input_data: dict) -> dict:
    """Run hook executables and return merged config fragments.

    Each hook receives the input JSON on stdin and outputs a config
    fragment on stdout. Fragments are deep-merged left to right.
    """
    merged: dict = {}
    input_json = json.dumps(input_data)
    for hook in hooks:
        try:
            result = subprocess.run(
                [hook],
                input=input_json,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.error("Hook %s failed (rc=%d): %s", hook, result.returncode, result.stderr)
                continue
            fragment = json.loads(result.stdout)
            merged = _deep_merge(merged, fragment)
        except Exception:
            logger.exception("Hook %s raised an exception", hook)
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _render_manifests(
    eval_nix: str,
    kubenix_flake: str,
    input_data: dict,
    extra_modules: list[str] | None = None,
) -> list[dict]:
    """Call nix eval on the sandbox expression and return K8s resource manifests."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        json.dump(input_data, f)
        input_path = f.name

    try:
        extra = ""
        if extra_modules:
            paths = " ".join(extra_modules)
            extra = f" extraModules = [ {paths} ];"

        expr = (
            f'(import {eval_nix} {{'
            f' kubenixFlake = "{kubenix_flake}";'
            f' inputFile = {input_path};'
            f'{extra}'
            f' }})'
        )

        result = subprocess.run(
            ["nix", "eval", "--json", "--impure", "--expr", expr],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"nix eval failed (rc={result.returncode}): {result.stderr}"
            )
        return json.loads(result.stdout)
    finally:
        os.unlink(input_path)


def _apply_manifest(
    core_v1: client.CoreV1Api,
    batch_v1: client.BatchV1Api,
    manifest: dict,
    namespace: str,
) -> None:
    """Apply a single K8s resource manifest."""
    kind = manifest.get("kind", "")
    name = manifest.get("metadata", {}).get("name", "unknown")

    if kind == "ServiceAccount":
        try:
            core_v1.create_namespaced_service_account(namespace=namespace, body=manifest)
            logger.info("Created ServiceAccount %s", name)
        except ApiException as e:
            if e.status != 409:
                raise
            logger.info("ServiceAccount %s already exists", name)

    elif kind == "PersistentVolumeClaim":
        try:
            core_v1.create_namespaced_persistent_volume_claim(namespace=namespace, body=manifest)
            logger.info("Created PVC %s", name)
        except ApiException as e:
            if e.status != 409:
                raise
            logger.info("PVC %s already exists", name)

    elif kind == "Job":
        try:
            batch_v1.create_namespaced_job(namespace=namespace, body=manifest)
            logger.info("Created Job %s", name)
        except ApiException as e:
            raise RuntimeError(f"Failed to create Job {name}: {e}") from e

    elif kind == "Service":
        try:
            core_v1.create_namespaced_service(namespace=namespace, body=manifest)
            logger.info("Created Service %s", name)
        except ApiException as e:
            if e.status == 409:
                logger.info("Service %s already exists", name)
            else:
                logger.warning("Failed to create Service %s (non-fatal): %s", name, e)

    elif kind == "Ingress":
        networking_v1 = client.NetworkingV1Api()
        try:
            networking_v1.create_namespaced_ingress(namespace=namespace, body=manifest)
            logger.info("Created Ingress %s", name)
        except ApiException as e:
            if e.status == 409:
                logger.info("Ingress %s already exists", name)
            else:
                logger.warning("Failed to create Ingress %s (non-fatal): %s", name, e)

    else:
        logger.warning("Unknown resource kind %s — skipping", kind)


class KubernetesSandboxService(SandboxService):
    """Manage agent-server sandboxes as Kubernetes Jobs.

    Resource creation uses a kubenix template rendered at runtime via
    ``nix eval``. This class applies the resulting manifests and handles
    lifecycle operations (get/pause/resume/delete).
    """

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

        # Sandbox eval paths (set at image build time)
        self.eval_nix = os.getenv("SANDBOX_EVAL_NIX", "")
        self.kubenix_flake = os.getenv("KUBENIX_FLAKE", "")

        # Extra kubenix module paths (colon-separated)
        extra_str = os.getenv("SANDBOX_EXTRA_MODULES", "")
        self.extra_modules: list[str] = [m for m in extra_str.split(":") if m]

        # Static template config overrides (sandbox.* options)
        self.template_config: dict = _env_json("SANDBOX_K8S_TEMPLATE_CONFIG", {})

        # Hook executables (colon-separated paths)
        hooks_str = os.getenv("SANDBOX_HOOKS", "")
        self.hooks: list[str] = [h for h in hooks_str.split(":") if h]

        # K8s client
        try:
            k8s_config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
            logger.info("Loaded kubeconfig from default location")

        self._batch_v1 = client.BatchV1Api()
        self._core_v1 = client.CoreV1Api()

    def _label_selector(self, extra: dict[str, str] | None = None) -> str:
        labels = {_LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE}
        if extra:
            labels.update(extra)
        return ",".join(f"{k}={v}" for k, v in labels.items())

    def _job_name(self, sandbox_id: str) -> str:
        return f"oh-sandbox-{sandbox_id}"

    def _service_name(self, sandbox_id: str) -> str:
        return f"oh-sandbox-{sandbox_id}"

    def _sandbox_id_from_job(self, job: client.V1Job) -> str:
        return job.metadata.labels.get(_LABEL_SANDBOX_ID, "")

    def _get_pod_for_job(self, job_name: str) -> client.V1Pod | None:
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
        sandbox_id = self._sandbox_id_from_job(job)
        if not sandbox_id:
            return None

        pod = self._get_pod_for_job(self._job_name(sandbox_id))
        if pod:
            phase = pod.status.phase if pod.status else None
            container_statuses = pod.status.container_statuses if pod.status else None
            status = _pod_phase_to_status(phase, container_statuses)
        else:
            if job.status and job.status.failed:
                status = SandboxStatus.ERROR
            else:
                status = SandboxStatus.STARTING

        if job.spec.suspend:
            status = SandboxStatus.PAUSED

        exposed_urls: list[ExposedUrl] | None = None
        session_api_key: str | None = None

        if status == SandboxStatus.RUNNING:
            session_api_key = self._extract_session_key(job)
            svc_name = self._service_name(sandbox_id)
            internal_url = f"http://{svc_name}.{self.namespace}.svc.cluster.local:{_DEFAULT_PORT}"

            # Use external ingress URL if configured, so the browser can reach
            # the sandbox WebSocket.  The internal URL is still used for
            # server-side API calls via AGENT_SERVER_INTERNAL.
            ingress_domain = self.template_config.get("ingress", {}).get("domain", "")
            if ingress_domain:
                external_url = f"https://{sandbox_id}.{ingress_domain}"
            else:
                external_url = internal_url

            exposed_urls = [
                ExposedUrl(name=AGENT_SERVER, url=external_url, port=_DEFAULT_PORT),
            ]

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
        self, page_id: str | None = None, limit: int = 100,
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
                pvc_name = f"oh-workspace-{sandbox_id}"
                try:
                    self._core_v1.read_namespaced_persistent_volume_claim(
                        name=pvc_name, namespace=self.namespace,
                    )
                except ApiException:
                    logger.debug("Sandbox Job %s not found (no PVC) — skipping", sandbox_id)
                    return None
                logger.info("Sandbox Job %s not found but PVC exists — recreating", sandbox_id)
                return await self._recreate_sandbox(sandbox_id)
            logger.error("Failed to get sandbox Job %s: %s", sandbox_id, e)
            return None

        return self._job_to_sandbox_info(job)

    async def batch_get_sandboxes(
        self, sandbox_ids: list[str],
    ) -> list[SandboxInfo | None]:
        results: list[SandboxInfo | None] = []
        for sandbox_id in sandbox_ids:
            try:
                job = self._batch_v1.read_namespaced_job(
                    name=self._job_name(sandbox_id), namespace=self.namespace,
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
        self, session_api_key: str,
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
        if info and info.session_api_key != session_api_key:
            return None
        return info

    async def _recreate_sandbox(self, sandbox_id: str) -> SandboxInfo:
        try:
            self._batch_v1.delete_namespaced_job(
                name=self._job_name(sandbox_id), namespace=self.namespace,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete old Job %s during recreate: %s", sandbox_id, e)

        try:
            self._core_v1.delete_namespaced_service(
                name=self._service_name(sandbox_id), namespace=self.namespace,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete old Service %s during recreate: %s", sandbox_id, e)

        try:
            networking_v1 = client.NetworkingV1Api()
            networking_v1.delete_namespaced_ingress(
                name=f"oh-sandbox-{sandbox_id}", namespace=self.namespace,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete old Ingress %s during recreate: %s", sandbox_id, e)

        await asyncio.sleep(1)

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
        image = os.getenv("SANDBOX_K8S_IMAGE") or sandbox_spec_id or get_agent_server_image()

        # Build sandbox-eval input (maps directly to sandbox.* options)
        input_data: dict[str, Any] = {
            "id": sandbox_id,
            "namespace": self.namespace,
            "image": image,
            "sessionApiKey": session_api_key,
        }

        # Forward agent-server env vars
        env: dict[str, str] = {}
        for k, v in get_agent_server_env().items():
            env[k] = v

        _ENV_PREFIX = "SANDBOX_K8S_ENV_"
        for key, value in os.environ.items():
            if key.startswith(_ENV_PREFIX) and key != "SANDBOX_K8S_ENV_SECRET" and value:
                env[key[len(_ENV_PREFIX):]] = value

        if env:
            input_data["env"] = env

        # Merge static template config (sandbox.* option overrides)
        if self.template_config:
            input_data = _deep_merge(input_data, self.template_config)

        # Run hooks
        if self.hooks:
            hook_config = _run_hooks(self.hooks, input_data)
            input_data = _deep_merge(input_data, hook_config)

        # Render and apply manifests
        manifests = _render_manifests(
            self.eval_nix, self.kubenix_flake, input_data, self.extra_modules,
        )
        for manifest in manifests:
            _apply_manifest(self._core_v1, self._batch_v1, manifest, self.namespace)

        logger.info("Created sandbox %s (image=%s, %d resources)", sandbox_id, image, len(manifests))

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
                name=self._job_name(sandbox_id), namespace=self.namespace,
            )
        except ApiException as e:
            if e.status == 404:
                pvc_name = f"oh-workspace-{sandbox_id}"
                try:
                    self._core_v1.read_namespaced_persistent_volume_claim(
                        name=pvc_name, namespace=self.namespace,
                    )
                except ApiException:
                    return False
                logger.info("Resume: Job %s gone but PVC exists — recreating", sandbox_id)
                info = await self._recreate_sandbox(sandbox_id)
                return info is not None
            raise

        if not job.spec.suspend:
            return True

        job.spec.suspend = False
        try:
            self._batch_v1.replace_namespaced_job(
                name=self._job_name(sandbox_id), namespace=self.namespace, body=job,
            )
            logger.info("Resumed sandbox Job %s", sandbox_id)
            return True
        except ApiException as e:
            logger.error("Failed to resume sandbox %s: %s", sandbox_id, e)
            return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        try:
            job = self._batch_v1.read_namespaced_job(
                name=self._job_name(sandbox_id), namespace=self.namespace,
            )
        except ApiException as e:
            if e.status == 404:
                return False
            raise

        if job.spec.suspend:
            return True

        job.spec.suspend = True
        try:
            self._batch_v1.replace_namespaced_job(
                name=self._job_name(sandbox_id), namespace=self.namespace, body=job,
            )
            logger.info("Paused sandbox Job %s", sandbox_id)
            return True
        except ApiException as e:
            logger.error("Failed to pause sandbox %s: %s", sandbox_id, e)
            return False

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        deleted = False

        try:
            self._batch_v1.delete_namespaced_job(
                name=self._job_name(sandbox_id), namespace=self.namespace,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
            deleted = True
            logger.info("Deleted sandbox Job %s", sandbox_id)
        except ApiException as e:
            if e.status != 404:
                logger.error("Failed to delete sandbox Job %s: %s", sandbox_id, e)

        try:
            self._core_v1.delete_namespaced_service(
                name=self._service_name(sandbox_id), namespace=self.namespace,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete sandbox Service %s: %s", sandbox_id, e)

        # Delete Ingress (may not exist if ingress was disabled)
        ingress_name = f"oh-sandbox-{sandbox_id}"
        try:
            networking_v1 = client.NetworkingV1Api()
            networking_v1.delete_namespaced_ingress(
                name=ingress_name, namespace=self.namespace,
            )
            logger.info("Deleted Ingress %s", ingress_name)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete Ingress %s: %s", ingress_name, e)

        sa_name = f"sandbox-{sandbox_id}"
        try:
            self._core_v1.delete_namespaced_service_account(
                name=sa_name, namespace=self.namespace,
            )
            logger.info("Deleted sandbox SA %s", sa_name)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete sandbox SA %s: %s", sa_name, e)

        # Delete PVCs without the preserve label
        try:
            pvcs = self._core_v1.list_namespaced_persistent_volume_claim(
                namespace=self.namespace,
                label_selector=f"{_LABEL_SANDBOX_ID}={sandbox_id}",
            )
            for pvc in pvcs.items:
                pvc_labels = pvc.metadata.labels or {}
                if pvc_labels.get(_LABEL_PRESERVE) == "true":
                    logger.info("Preserving PVC %s (has preserve label)", pvc.metadata.name)
                    continue
                try:
                    self._core_v1.delete_namespaced_persistent_volume_claim(
                        name=pvc.metadata.name, namespace=self.namespace,
                    )
                    logger.info("Deleted PVC %s", pvc.metadata.name)
                except ApiException as e:
                    if e.status != 404:
                        logger.warning("Failed to delete PVC %s: %s", pvc.metadata.name, e)
        except ApiException as e:
            logger.warning("Failed to list PVCs for sandbox %s: %s", sandbox_id, e)

        return deleted


class KubernetesSandboxSpecService(SandboxSpecService):
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
        self, page_id: str | None = None, limit: int = 100,
    ) -> SandboxSpecInfoPage:
        return SandboxSpecInfoPage(items=[self._default_spec], next_page_id=None)

    async def get_sandbox_spec(self, sandbox_spec_id: str) -> SandboxSpecInfo | None:
        return self._default_spec


# ---- Dependency Injection ----

from collections.abc import AsyncGenerator
from fastapi import Request
from openhands.app_server.sandbox.sandbox_service import SandboxServiceInjector
from openhands.app_server.sandbox.sandbox_spec_service import SandboxSpecServiceInjector
from openhands.app_server.services.injector import InjectorState


class KubernetesSandboxServiceInjector(SandboxServiceInjector):
    async def inject(
        self, state: InjectorState, request: Request | None = None,
    ) -> AsyncGenerator[SandboxService, None]:
        yield KubernetesSandboxService()


class KubernetesSandboxSpecServiceInjector(SandboxSpecServiceInjector):
    async def inject(
        self, state: InjectorState, request: Request | None = None,
    ) -> AsyncGenerator[SandboxSpecService, None]:
        yield KubernetesSandboxSpecService()
