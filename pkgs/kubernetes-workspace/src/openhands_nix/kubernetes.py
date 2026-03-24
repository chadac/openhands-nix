"""KubernetesWorkspace: K8s Jobs with entrypoint-based Nix package install.

Creates a Kubernetes Job running the agent-server image. Nix packages
are installed at pod startup via the NIX_PACKAGES env var before the
agent-server begins serving.

For faster startup with pre-provisioned packages, see NixCSIWorkspace.

Example:
    workspace = KubernetesWorkspace(
        image="openhands-agent-server:latest",
        namespace="openhands",
        nix=NixEnvironment(packages=["nixpkgs#nodejs", "nixpkgs#ripgrep"]),
        env={"OPENAI_API_KEY": "sk-..."},
    )
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import uuid
from typing import Any

from pydantic import Field
from openhands.sdk.workspace.remote.base import RemoteWorkspace

from openhands_nix.workspace import NixEnvironment

logger = logging.getLogger(__name__)

_DEFAULT_INIT_TIMEOUT = 300.0
_DEFAULT_PORT = 8000


class KubernetesWorkspace(RemoteWorkspace):
    """Workspace backed by a Kubernetes Job running agent-server.

    Lifecycle:
    1. Create a K8s Job with a single pod running the agent-server image
    2. Entrypoint installs Nix packages (if configured)
    3. Wait for the pod to be Running and healthy
    4. Set up port-forwarding (or use a Service/Ingress URL)
    5. Delegate all operations to RemoteWorkspace
    6. On cleanup, delete the Job and stop port-forwarding
    """

    # --- Nix environment ---
    nix: NixEnvironment = Field(
        default_factory=NixEnvironment,
        description="Nix environment configuration (packages, flake refs, etc.)",
    )

    # --- Container & K8s config ---
    image: str = Field(
        description="Container image running agent-server (should include Nix)",
    )
    namespace: str = Field(default="default")
    working_dir: str = Field(default="/workspace")
    host: str = Field(default="")

    service_account: str | None = Field(default=None)
    resource_requests: dict[str, str] = Field(
        default_factory=lambda: {"cpu": "250m", "memory": "512Mi"},
    )
    resource_limits: dict[str, str] = Field(default_factory=dict)
    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[dict[str, Any]] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    image_pull_policy: str = Field(default="IfNotPresent")
    image_pull_secrets: list[str] = Field(default_factory=list)
    volumes: list[dict[str, Any]] = Field(default_factory=list)
    volume_mounts: list[dict[str, Any]] = Field(default_factory=list)

    # Connectivity
    port: int = Field(default=_DEFAULT_PORT)
    use_port_forward: bool = Field(default=True)
    external_host: str | None = Field(default=None)
    local_port: int | None = Field(default=None)

    # Lifecycle
    init_timeout: float = Field(default=_DEFAULT_INIT_TIMEOUT)
    cleanup_on_exit: bool = Field(default=True)
    kubeconfig: str | None = Field(default=None)
    context: str | None = Field(default=None)

    # Private state
    _job_name: str | None = None
    _pod_name: str | None = None
    _port_forward_proc: subprocess.Popen | None = None
    _port_forward_thread: threading.Thread | None = None
    _stop_event: threading.Event = threading.Event()
    _local_port: int | None = None

    model_config = {"arbitrary_types_allowed": True}

    def model_post_init(self, __context: Any) -> None:
        self._stop_event = threading.Event()
        self._job_name = f"openhands-workspace-{uuid.uuid4().hex[:8]}"

        logger.info(
            "Creating KubernetesWorkspace: job=%s namespace=%s image=%s nix_packages=%s",
            self._job_name, self.namespace, self.image,
            self.nix.packages if self.nix.has_nix_config else "(none)",
        )

        self._create_job()
        self._wait_for_pod_running()

        if self.use_port_forward:
            self._start_port_forward()
            object.__setattr__(
                self, "host", f"http://127.0.0.1:{self._local_port}"
            )
        elif self.external_host:
            object.__setattr__(self, "host", self.external_host)
        else:
            raise ValueError(
                "use_port_forward=False requires external_host to be set"
            )

        self._wait_for_health()
        super().model_post_init(__context)

    # --- kubectl helper ---

    def _kubectl(self, *args: str, input: str | None = None) -> str:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        cmd += list(args)

        logger.debug("kubectl: %s", " ".join(cmd))
        result = subprocess.run(
            cmd, capture_output=True, text=True, input=input, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"kubectl failed (exit {result.returncode}): {result.stderr}"
            )
        return result.stdout

    # --- Job creation ---

    def _build_env_list(self) -> list[dict[str, str]]:
        env_list = [
            {"name": k, "value": v} for k, v in self.env.items()
        ]
        if self.nix.has_nix_config:
            install_args = self.nix.to_install_args()
            env_list.append({
                "name": "NIX_PACKAGES",
                "value": " ".join(install_args),
            })
        return env_list

    def _build_container_spec(self, env_list: list[dict]) -> dict[str, Any]:
        has_nix = self.nix.has_nix_config
        container: dict[str, Any] = {
            "name": "agent-server",
            "image": self.image,
            "imagePullPolicy": self.image_pull_policy,
            "ports": [{"containerPort": self.port, "name": "http"}],
            "env": env_list,
            "resources": {},
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": self.port},
                "initialDelaySeconds": 30 if has_nix else 5,
                "periodSeconds": 5,
                "timeoutSeconds": 3,
                "failureThreshold": 60 if has_nix else 12,
            },
            "livenessProbe": {
                "httpGet": {"path": "/health", "port": self.port},
                "initialDelaySeconds": 60 if has_nix else 15,
                "periodSeconds": 30,
                "timeoutSeconds": 5,
            },
        }
        if self.resource_requests:
            container["resources"]["requests"] = self.resource_requests
        if self.resource_limits:
            container["resources"]["limits"] = self.resource_limits
        if self.volume_mounts:
            container["volumeMounts"] = list(self.volume_mounts)
        return container

    def _create_job(self) -> None:
        job_labels = {
            "app.kubernetes.io/name": "openhands-workspace",
            "app.kubernetes.io/managed-by": "openhands-nix",
            "openhands.ai/workspace-id": self._job_name,
            **self.labels,
        }

        env_list = self._build_env_list()
        container = self._build_container_spec(env_list)

        pod_spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "containers": [container],
        }

        if self.service_account:
            pod_spec["serviceAccountName"] = self.service_account
        if self.node_selector:
            pod_spec["nodeSelector"] = self.node_selector
        if self.tolerations:
            pod_spec["tolerations"] = self.tolerations
        if self.volumes:
            pod_spec.setdefault("volumes", []).extend(self.volumes)
        if self.image_pull_secrets:
            pod_spec["imagePullSecrets"] = [
                {"name": s} for s in self.image_pull_secrets
            ]

        job_manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": self._job_name,
                "namespace": self.namespace,
                "labels": job_labels,
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 60,
                "template": {
                    "metadata": {"labels": job_labels},
                    "spec": pod_spec,
                },
            },
        }

        self._kubectl(
            "apply", "-f", "-", "-n", self.namespace,
            input=json.dumps(job_manifest),
        )
        logger.info("Created Job %s in namespace %s", self._job_name, self.namespace)

    # --- Pod lifecycle ---

    def _wait_for_pod_running(self) -> None:
        deadline = time.monotonic() + self.init_timeout
        selector = f"openhands.ai/workspace-id={self._job_name}"

        while time.monotonic() < deadline:
            output = self._kubectl(
                "get", "pods", "-n", self.namespace,
                "-l", selector,
                "-o", "jsonpath={.items[0].metadata.name},{.items[0].status.phase}",
            )
            if "," in output:
                pod_name, phase = output.split(",", 1)
                if phase == "Running":
                    self._pod_name = pod_name
                    logger.info("Pod %s is Running", pod_name)
                    return
                elif phase in ("Failed", "Unknown"):
                    try:
                        logs = self._kubectl(
                            "logs", pod_name, "-n", self.namespace, "--tail=50",
                        )
                    except Exception:
                        logs = "(could not fetch logs)"
                    raise RuntimeError(
                        f"Pod {pod_name} entered {phase} state. Logs:\n{logs}"
                    )
                logger.debug("Pod %s phase: %s, waiting...", pod_name, phase)
            time.sleep(3)

        raise TimeoutError(
            f"Pod did not reach Running state within {self.init_timeout}s"
        )

    def _start_port_forward(self) -> None:
        if self.local_port:
            self._local_port = self.local_port
        else:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                self._local_port = s.getsockname()[1]

        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        cmd += [
            "port-forward", f"pod/{self._pod_name}",
            f"{self._local_port}:{self.port}", "-n", self.namespace,
        ]

        logger.info(
            "Starting port-forward: localhost:%s -> %s:%s",
            self._local_port, self._pod_name, self.port,
        )

        self._port_forward_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(2)

        if self._port_forward_proc.poll() is not None:
            output = self._port_forward_proc.stdout.read() if self._port_forward_proc.stdout else ""
            raise RuntimeError(f"kubectl port-forward exited immediately: {output}")

        def _stream_logs():
            proc = self._port_forward_proc
            if proc and proc.stdout:
                for line in proc.stdout:
                    if self._stop_event.is_set():
                        break
                    logger.debug("port-forward: %s", line.rstrip())

        self._port_forward_thread = threading.Thread(target=_stream_logs, daemon=True)
        self._port_forward_thread.start()

    def _wait_for_health(self) -> None:
        import urllib.request
        import urllib.error

        deadline = time.monotonic() + self.init_timeout
        health_url = f"{self.host}/health"

        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(health_url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        logger.info("Agent-server is healthy at %s", self.host)
                        return
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(2)

        raise TimeoutError(
            f"Agent-server did not become healthy at {health_url} "
            f"within {self.init_timeout}s"
        )

    # --- Lifecycle ---

    def pause(self) -> None:
        raise NotImplementedError(
            "KubernetesWorkspace does not support pause. "
            "Use cleanup() and create a new workspace instead."
        )

    def resume(self) -> None:
        raise NotImplementedError(
            "KubernetesWorkspace does not support resume."
        )

    def cleanup(self) -> None:
        logger.info("Cleaning up KubernetesWorkspace: job=%s", self._job_name)

        self._stop_event.set()
        if self._port_forward_proc:
            try:
                self._port_forward_proc.terminate()
                self._port_forward_proc.wait(timeout=5)
            except Exception:
                try:
                    self._port_forward_proc.kill()
                except Exception:
                    pass
            self._port_forward_proc = None

        if self.cleanup_on_exit and self._job_name:
            try:
                self._kubectl(
                    "delete", "job", self._job_name,
                    "-n", self.namespace, "--grace-period=10",
                )
                logger.info("Deleted Job %s", self._job_name)
            except Exception as e:
                logger.warning("Failed to delete Job %s: %s", self._job_name, e)

    def __enter__(self) -> KubernetesWorkspace:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()

    def __del__(self) -> None:
        try:
            self.cleanup()
        except Exception:
            pass
