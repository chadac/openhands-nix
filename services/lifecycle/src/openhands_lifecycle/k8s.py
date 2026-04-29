"""Kubernetes API client helpers for sandbox lifecycle management."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SANDBOX_PREFIX = "oh-sandbox-"
_WORKSPACE_PVC_SUFFIX = "-workspace"


def get_k8s_clients():
    """Lazily load Kubernetes API clients (in-cluster config).

    Returns (CoreV1Api, BatchV1Api, NetworkingV1Api) or (None, None, None).
    """
    try:
        from kubernetes import client, config
        config.load_incluster_config()
        return client.CoreV1Api(), client.BatchV1Api(), client.NetworkingV1Api()
    except Exception:
        logger.debug("Kubernetes API not available (not running in-cluster?)")
        return None, None, None


def get_sandbox_pod_start_time(
    sandbox_id: str, namespace: str,
) -> datetime | None:
    """Get the start time of the sandbox's Pod."""
    core_v1, _, _ = get_k8s_clients()
    if core_v1 is None:
        return None
    try:
        job_name = f"{SANDBOX_PREFIX}{sandbox_id}"
        pods = core_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={job_name}",
        )
        if pods.items:
            return pods.items[0].metadata.creation_timestamp
    except Exception:
        pass
    return None


def delete_sandbox_resources(
    sandbox_id: str, namespace: str,
) -> None:
    """Delete orphaned sandbox compute resources (Job, Service, Ingress)."""
    core_v1, batch_v1, networking_v1 = get_k8s_clients()
    if core_v1 is None:
        return

    resource_name = f"{SANDBOX_PREFIX}{sandbox_id}"

    # Delete Job (propagation=Background deletes the pod too)
    try:
        batch_v1.delete_namespaced_job(resource_name, namespace, propagation_policy="Background")
        logger.info("Deleted orphaned Job %s", resource_name)
    except Exception as e:
        if "404" not in str(e):
            logger.warning("Failed to delete Job %s: %s", resource_name, e)

    # Delete Service
    try:
        core_v1.delete_namespaced_service(resource_name, namespace)
        logger.info("Deleted orphaned Service %s", resource_name)
    except Exception as e:
        if "404" not in str(e):
            logger.warning("Failed to delete Service %s: %s", resource_name, e)

    # Delete Ingress
    try:
        networking_v1.delete_namespaced_ingress(resource_name, namespace)
        logger.info("Deleted orphaned Ingress %s", resource_name)
    except Exception as e:
        if "404" not in str(e):
            logger.warning("Failed to delete Ingress %s: %s", resource_name, e)


def cleanup_old_pvcs(namespace: str, max_age_days: int) -> None:
    """Delete sandbox workspace PVCs older than max_age_days."""
    if max_age_days <= 0:
        return

    core_v1, _, _ = get_k8s_clients()
    if core_v1 is None:
        return

    try:
        now = datetime.now(timezone.utc)
        pvcs = core_v1.list_namespaced_persistent_volume_claim(namespace)

        for pvc in pvcs.items:
            name = pvc.metadata.name
            if not (name.startswith(SANDBOX_PREFIX) and name.endswith(_WORKSPACE_PVC_SUFFIX)):
                continue

            created = pvc.metadata.creation_timestamp
            if not created:
                continue

            age_days = (now - created).total_seconds() / 86400
            if age_days > max_age_days:
                try:
                    core_v1.delete_namespaced_persistent_volume_claim(name, namespace)
                    logger.info("Deleted old PVC %s (age=%.0fd, max=%dd)", name, age_days, max_age_days)
                except Exception as e:
                    if "404" not in str(e):
                        logger.warning("Failed to delete PVC %s: %s", name, e)
    except Exception:
        logger.exception("Error during old PVC cleanup")
