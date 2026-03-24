"""NixCSIWorkspace: K8s workspace with nix-csi for instant package provisioning.

Uses the nix-csi CSI driver (https://github.com/Lillecarl/nix-csi) to
mount Nix packages as ephemeral volumes. The CSI driver builds/fetches
packages on the node *before* the pod starts, so startup is instant —
no waiting for in-container installs.

Requires nix-csi deployed to the cluster.

Example:
    workspace = NixCSIWorkspace(
        image="openhands-agent-server:latest",
        namespace="openhands",
        nix=NixEnvironment(packages=["nixpkgs#nodejs", "nixpkgs#ripgrep"]),
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

_DEFAULT_PORT = 8000
_DEFAULT_INIT_TIMEOUT = 300.0

# nix-csi driver name
_CSI_DRIVER = "nix.csi.store"

# Where the CSI volume gets mounted inside the container
_NIX_MOUNT_PATH = "/nix"


class NixCSIWorkspace(RemoteWorkspace):
    """Workspace backed by K8s + nix-csi ephemeral volumes.

    Lifecycle:
    1. Create a K8s Job with a CSI ephemeral volume for Nix packages
    2. nix-csi builds/fetches packages on the node (before pod starts)
    3. Pod starts with packages already mounted at /nix
    4. Port-forward and delegate to RemoteWorkspace
    5. Cleanup deletes the Job
    """

    nix: NixEnvironment = Field(
        default_factory=NixEnvironment,
        description="Nix environment configuration",
    )

    # Container & K8s config
    image: str = Field(
        description="Container image running agent-server",
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

    # CSI-specific config
    csi_driver: str = Field(
        default=_CSI_DRIVER,
        description="CSI driver name (default: nix.csi.store)",
    )
    csi_read_only: bool = Field(
        default=False,
        description="Mount the Nix store read-only (enables shared page cache)",
    )

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
        self._job_name = f"openhands-csi-{uuid.uuid4().hex[:8]}"

        logger.info(
            "Creating NixCSIWorkspace: job=%s namespace=%s csi_driver=%s",
            self._job_name, self.namespace, self.csi_driver,
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

    def _create_job(self) -> None:
        job_labels = {
            "app.kubernetes.io/name": "openhands-workspace",
            "app.kubernetes.io/managed-by": "openhands-nix",
            "openhands.ai/workspace-id": self._job_name,
            **self.labels,
        }

        env_list = [
            {"name": k, "value": v} for k, v in self.env.items()
        ]

        # CSI ephemeral volume for Nix packages
        csi_volume_attrs = self.nix.to_csi_volume_attributes()
        nix_volume = {
            "name": "nix-env",
            "csi": {
                "driver": self.csi_driver,
                "readOnly": self.csi_read_only,
                "volumeAttributes": csi_volume_attrs,
            },
        }
        nix_mount = {
            "name": "nix-env",
            "mountPath": _NIX_MOUNT_PATH,
            "readOnly": self.csi_read_only,
        }

        container_mounts = [nix_mount]
        if self.volume_mounts:
            container_mounts.extend(self.volume_mounts)

        container: dict[str, Any] = {
            "name": "agent-server",
            "image": self.image,
            "imagePullPolicy": self.image_pull_policy,
            "ports": [{"containerPort": self.port, "name": "http"}],
            "env": env_list,
            "volumeMounts": container_mounts,
            "resources": {},
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": self.port},
                "initialDelaySeconds": 5,
                "periodSeconds": 5,
                "timeoutSeconds": 3,
            },
            "livenessProbe": {
                "httpGet": {"path": "/health", "port": self.port},
                "initialDelaySeconds": 15,
                "periodSeconds": 30,
                "timeoutSeconds": 5,
            },
        }

        if self.resource_requests:
            container["resources"]["requests"] = self.resource_requests
        if self.resource_limits:
            container["resources"]["limits"] = self.resource_limits

        pod_volumes = [nix_volume]
        if self.volumes:
            pod_volumes.extend(self.volumes)

        pod_spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "containers": [container],
            "volumes": pod_volumes,
        }

        if self.service_account:
            pod_spec["serviceAccountName"] = self.service_account
        if self.node_selector:
            pod_spec["nodeSelector"] = self.node_selector
        if self.tolerations:
            pod_spec["tolerations"] = self.tolerations
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
        logger.info("Created Job %s with nix-csi volume", self._job_name)

    # Reuse the same pod lifecycle methods as KubernetesWorkspace

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

    def pause(self) -> None:
        raise NotImplementedError("NixCSIWorkspace does not support pause.")

    def resume(self) -> None:
        raise NotImplementedError("NixCSIWorkspace does not support resume.")

    def cleanup(self) -> None:
        logger.info("Cleaning up NixCSIWorkspace: job=%s", self._job_name)

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

    def __enter__(self) -> NixCSIWorkspace:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()

    def __del__(self) -> None:
        try:
            self.cleanup()
        except Exception:
            pass
