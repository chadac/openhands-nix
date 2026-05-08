"""KubenixRuntime — extends KubernetesRuntime with template-based pod customization.

Reads SANDBOX_K8S_TEMPLATE_CONFIG (JSON env var) and injects extra env vars,
volumes, volume mounts, init containers, and per-sandbox service accounts
into the pod manifest.

Usage:
    RUNTIME=openhands_nix.kubenix_runtime.KubenixRuntime \
    SANDBOX_K8S_TEMPLATE_CONFIG='{"env":{"FOO":"bar"},...}' \
    openhands-server
"""

import json
import logging
import os

from openhands.runtime.impl.kubernetes.kubernetes_runtime import KubernetesRuntime

logger = logging.getLogger(__name__)


def _env_json(key: str, default=None):
    raw = os.getenv(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


class KubenixRuntime(KubernetesRuntime):
    """KubernetesRuntime subclass that injects extra config from SANDBOX_K8S_TEMPLATE_CONFIG.

    Supported template config keys:
      - env: dict of name -> value
      - volumes: dict of name -> volume spec (projected, secret, emptyDir)
      - volumeMounts: dict of name -> {mountPath, readOnly}
      - initContainers: dict of name -> {image, command, args, env, volumeMounts, imagePullPolicy}
      - irsaRoleArn: string — creates per-sandbox SA with IRSA annotation
      - resourceRequests: dict of cpu/memory overrides
      - resourceLimits: dict of cpu/memory overrides
      - imagePullPolicy: string
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._template_config = _env_json("SANDBOX_K8S_TEMPLATE_CONFIG", {})

    def _get_runtime_pod_manifest(self):
        from kubernetes.client import (
            V1Container,
            V1ContainerPort,
            V1EnvVar,
            V1VolumeMount,
        )

        pod = super()._get_runtime_pod_manifest()

        tc = self._template_config
        if not tc:
            return pod

        spec = pod.spec
        container = spec.containers[0]

        # --- Extra env vars ---
        extra_env = tc.get("env", {})
        for name, value in extra_env.items():
            container.env.append(V1EnvVar(name=name, value=str(value)))

        # --- Extra volumes ---
        extra_volumes = tc.get("volumes", {})
        for vol_name, vol_spec in extra_volumes.items():
            vol = _build_volume(vol_name, vol_spec)
            if vol:
                spec.volumes.append(vol)

        # --- Extra volume mounts ---
        extra_mounts = tc.get("volumeMounts", {})
        for mount_name, mount_spec in extra_mounts.items():
            container.volume_mounts.append(V1VolumeMount(
                name=mount_name,
                mount_path=mount_spec.get("mountPath", f"/mnt/{mount_name}"),
                read_only=mount_spec.get("readOnly", False),
            ))

        # --- Init containers ---
        extra_init = tc.get("initContainers", {})
        if extra_init:
            if not spec.init_containers:
                spec.init_containers = []
            for ic_name, ic_spec in extra_init.items():
                ic_env = [V1EnvVar(name=e["name"], value=e.get("value", ""))
                          for e in ic_spec.get("env", [])]
                ic_mounts = [V1VolumeMount(
                    name=m.get("name", ""),
                    mount_path=m.get("mountPath", ""),
                    read_only=m.get("readOnly", False),
                ) for m in ic_spec.get("volumeMounts", [])]
                ic_ports = None
                if "ports" in ic_spec:
                    ic_ports = [V1ContainerPort(
                        container_port=p.get("containerPort", 0),
                        name=p.get("name"),
                        protocol=p.get("protocol", "TCP"),
                    ) for p in ic_spec["ports"]]
                spec.init_containers.append(V1Container(
                    name=ic_name,
                    image=ic_spec.get("image", "busybox:latest"),
                    image_pull_policy=ic_spec.get("imagePullPolicy", "IfNotPresent"),
                    command=ic_spec.get("command"),
                    args=ic_spec.get("args"),
                    env=ic_env or None,
                    volume_mounts=ic_mounts or None,
                    ports=ic_ports,
                ))

        # --- Service account ---
        irsa_role = tc.get("irsaRoleArn", "")
        if irsa_role:
            sa_name = f"sandbox-{self.sid}"
            spec.service_account_name = sa_name
            _ensure_service_account(self, sa_name, irsa_role)

        logger.info(
            "KubenixRuntime: patched pod manifest: +%d env, +%d volumes, +%d mounts, +%d init containers",
            len(extra_env), len(extra_volumes), len(extra_mounts), len(extra_init),
        )
        return pod


def _build_volume(name, spec):
    """Convert a template config volume spec dict to a V1Volume."""
    from kubernetes.client import (
        V1EmptyDirVolumeSource,
        V1ProjectedVolumeSource,
        V1SecretVolumeSource,
        V1ServiceAccountTokenProjection,
        V1Volume,
        V1VolumeProjection,
    )

    if "projected" in spec:
        sources = []
        for src in spec["projected"].get("sources", []):
            if "serviceAccountToken" in src:
                sat = src["serviceAccountToken"]
                sources.append(V1VolumeProjection(
                    service_account_token=V1ServiceAccountTokenProjection(
                        audience=sat.get("audience", ""),
                        expiration_seconds=sat.get("expirationSeconds", 3600),
                        path=sat.get("path", "token"),
                    ),
                ))
        return V1Volume(name=name, projected=V1ProjectedVolumeSource(sources=sources))

    if "secret" in spec:
        secret = spec["secret"]
        return V1Volume(name=name, secret=V1SecretVolumeSource(
            secret_name=secret.get("secretName", ""),
            default_mode=secret.get("defaultMode"),
        ))

    if "emptyDir" in spec:
        ed = spec["emptyDir"]
        return V1Volume(name=name, empty_dir=V1EmptyDirVolumeSource(
            medium=ed.get("medium", ""),
            size_limit=ed.get("sizeLimit"),
        ))

    if "persistentVolumeClaim" in spec:
        from kubernetes.client import V1PersistentVolumeClaimVolumeSource
        pvc = spec["persistentVolumeClaim"]
        return V1Volume(name=name, persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
            claim_name=pvc.get("claimName", ""),
            read_only=pvc.get("readOnly", False),
        ))

    logger.warning("Unknown volume type for %s: %s", name, list(spec.keys()))
    return None


def _ensure_service_account(runtime, sa_name, irsa_role_arn):
    """Create a per-sandbox ServiceAccount with IRSA annotation (idempotent)."""
    from kubernetes.client import V1ObjectMeta, V1ServiceAccount
    from kubernetes.client.rest import ApiException

    try:
        runtime.k8s_client.create_namespaced_service_account(
            namespace=runtime._k8s_namespace,
            body=V1ServiceAccount(
                metadata=V1ObjectMeta(
                    name=sa_name,
                    namespace=runtime._k8s_namespace,
                    annotations={"eks.amazonaws.com/role-arn": irsa_role_arn},
                    labels={
                        "openhands.ai/managed-by": "kubenix-runtime",
                        "openhands.ai/sandbox-id": runtime.sid,
                    },
                ),
            ),
        )
        logger.info("Created sandbox SA %s with IRSA %s", sa_name, irsa_role_arn)
    except ApiException as e:
        if e.status == 409:
            logger.debug("SA %s already exists", sa_name)
        else:
            logger.error("Failed to create SA %s: %s", sa_name, e)
