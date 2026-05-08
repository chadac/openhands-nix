"""Tests for sandbox-eval: kubenix template rendering pipeline.

These tests invoke ``nix eval`` on the sandbox eval expression and
validate the rendered K8s manifests.

Requires: SANDBOX_EVAL_EXPR environment variable pointing to the
          nix store path of the eval expression file.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest


SANDBOX_EVAL_EXPR = os.environ["SANDBOX_EVAL_EXPR"]


def eval_sandbox(input_data: dict) -> dict:
    """Run nix eval with the given input and return parsed JSON output."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(input_data, f)
        f.flush()
        try:
            result = subprocess.run(
                [
                    "nix", "eval", "--json", "--impure",
                    "--expr", f"(import {SANDBOX_EVAL_EXPR} {{ inputFile = {f.name}; }})",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                pytest.fail(
                    f"nix eval failed (rc={result.returncode}):\n"
                    f"stderr: {result.stderr}\n"
                    f"stdout: {result.stdout}"
                )
            return json.loads(result.stdout)
        finally:
            os.unlink(f.name)


def get_resource(output: dict, kind: str, name: str | None = None) -> dict | None:
    """Find a resource by kind (and optionally name) in the output."""
    for item in output["items"]:
        if item["kind"] == kind:
            if name is None or item["metadata"]["name"] == name:
                return item
    return None


def get_resources(output: dict, kind: str) -> list[dict]:
    """Get all resources of a given kind."""
    return [item for item in output["items"] if item["kind"] == kind]


@pytest.fixture
def base_input() -> dict:
    return {
        "sandbox_id": "abc123def",
        "namespace": "openhands",
        "image": "my-registry/agent-server:v1.0",
        "session_api_key": "sk-test-key-12345",
    }


@pytest.fixture
def base_output(base_input: dict) -> dict:
    return eval_sandbox(base_input)


class TestBasicRendering:
    def test_output_is_k8s_list(self, base_output: dict) -> None:
        assert base_output["kind"] == "List"
        assert base_output["apiVersion"] == "v1"
        assert "items" in base_output

    def test_produces_four_resources(self, base_output: dict) -> None:
        kinds = sorted(item["kind"] for item in base_output["items"])
        assert kinds == ["Job", "PersistentVolumeClaim", "Service", "ServiceAccount"]

    def test_all_resources_namespaced(self, base_output: dict) -> None:
        for item in base_output["items"]:
            assert item["metadata"]["namespace"] == "openhands"

    def test_all_resources_labeled(self, base_output: dict) -> None:
        for item in base_output["items"]:
            labels = item["metadata"]["labels"]
            assert labels["openhands.ai/managed-by"] == "openhands-nix-kubernetes"
            assert labels["openhands.ai/sandbox-id"] == "abc123def"


class TestServiceAccount:
    def test_name(self, base_output: dict) -> None:
        sa = get_resource(base_output, "ServiceAccount")
        assert sa["metadata"]["name"] == "sandbox-abc123def"

    def test_no_irsa_by_default(self, base_output: dict) -> None:
        sa = get_resource(base_output, "ServiceAccount")
        annotations = sa["metadata"].get("annotations", {})
        assert "eks.amazonaws.com/role-arn" not in annotations

    def test_irsa_annotation(self, base_input: dict) -> None:
        base_input["config"] = {
            "irsaRoleArn": "arn:aws:iam::123456:role/test-role",
        }
        output = eval_sandbox(base_input)
        sa = get_resource(output, "ServiceAccount")
        assert sa["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] == (
            "arn:aws:iam::123456:role/test-role"
        )


class TestWorkspacePVC:
    def test_name(self, base_output: dict) -> None:
        pvc = get_resource(base_output, "PersistentVolumeClaim")
        assert pvc["metadata"]["name"] == "oh-workspace-abc123def"

    def test_default_storage_class(self, base_output: dict) -> None:
        pvc = get_resource(base_output, "PersistentVolumeClaim")
        assert pvc["spec"]["storageClassName"] == "ebs-gp3"

    def test_default_size(self, base_output: dict) -> None:
        pvc = get_resource(base_output, "PersistentVolumeClaim")
        assert pvc["spec"]["resources"]["requests"]["storage"] == "10Gi"

    def test_access_mode(self, base_output: dict) -> None:
        pvc = get_resource(base_output, "PersistentVolumeClaim")
        assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]

    def test_preserve_label(self, base_output: dict) -> None:
        pvc = get_resource(base_output, "PersistentVolumeClaim")
        assert pvc["metadata"]["labels"]["openhands.ai/preserve"] == "true"

    def test_custom_storage_class(self, base_input: dict) -> None:
        base_input["config"] = {
            "workspaceStorageClass": "fast-nvme",
            "workspaceSize": "50Gi",
        }
        output = eval_sandbox(base_input)
        pvc = get_resource(output, "PersistentVolumeClaim")
        assert pvc["spec"]["storageClassName"] == "fast-nvme"
        assert pvc["spec"]["resources"]["requests"]["storage"] == "50Gi"


class TestJob:
    def test_name(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        assert job["metadata"]["name"] == "oh-sandbox-abc123def"

    def test_backoff_limit(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        assert job["spec"]["backoffLimit"] == 0

    def test_ttl(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        assert job["spec"]["ttlSecondsAfterFinished"] == 300

    def test_session_key_label(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        assert job["metadata"]["labels"]["openhands.ai/session-key-hash"] == "sk-test-key-12345"

    def test_karpenter_annotation(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        tmpl = job["spec"]["template"]
        assert tmpl["metadata"]["annotations"]["karpenter.sh/do-not-disrupt"] == "true"

    def test_service_account(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        spec = job["spec"]["template"]["spec"]
        assert spec["serviceAccountName"] == "sandbox-abc123def"

    def test_restart_policy(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"

    def test_container_image(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "my-registry/agent-server:v1.0"
        assert container["name"] == "agent-server"

    def test_container_port(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        ports = container["ports"]
        assert len(ports) == 1
        assert ports[0]["containerPort"] == 8000
        assert ports[0]["name"] == "http"

    def test_default_resources(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["requests"] == {"cpu": "250m", "memory": "512Mi"}
        assert "limits" not in container["resources"]

    def test_readiness_probe(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        probe = container["readinessProbe"]
        assert probe["httpGet"]["path"] == "/health"
        assert probe["httpGet"]["port"] == 8000
        assert probe["failureThreshold"] == 60

    def test_base_env_vars(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env["PORT"] == "8000"
        assert env["HOST"] == "0.0.0.0"
        assert env["LOG_JSON"] == "true"
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["OH_SESSION_API_KEYS_0"] == "sk-test-key-12345"
        assert env["OPENHANDS_SANDBOX_ID"] == "abc123def"

    def test_base_volumes(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        volumes = job["spec"]["template"]["spec"]["volumes"]
        vol_names = [v["name"] for v in volumes]
        assert "workspace-volume" in vol_names
        assert "tmp" in vol_names
        assert "dshm" in vol_names

    def test_workspace_volume_claim(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        volumes = job["spec"]["template"]["spec"]["volumes"]
        ws = next(v for v in volumes if v["name"] == "workspace-volume")
        assert ws["persistentVolumeClaim"]["claimName"] == "oh-workspace-abc123def"

    def test_tmpfs_volumes(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        volumes = job["spec"]["template"]["spec"]["volumes"]
        tmp = next(v for v in volumes if v["name"] == "tmp")
        assert tmp["emptyDir"]["medium"] == "Memory"
        dshm = next(v for v in volumes if v["name"] == "dshm")
        assert dshm["emptyDir"]["medium"] == "Memory"
        assert dshm["emptyDir"]["sizeLimit"] == "512Mi"

    def test_base_volume_mounts(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
        assert mounts["workspace-volume"] == "/workspace"
        assert mounts["tmp"] == "/tmp"
        assert mounts["dshm"] == "/dev/shm"

    def test_no_init_containers_by_default(self, base_output: dict) -> None:
        job = get_resource(base_output, "Job")
        init = job["spec"]["template"]["spec"].get("initContainers", [])
        assert init == []


class TestService:
    def test_name(self, base_output: dict) -> None:
        svc = get_resource(base_output, "Service")
        assert svc["metadata"]["name"] == "oh-sandbox-abc123def"

    def test_selector(self, base_output: dict) -> None:
        svc = get_resource(base_output, "Service")
        assert svc["spec"]["selector"] == {"openhands.ai/sandbox-id": "abc123def"}

    def test_port(self, base_output: dict) -> None:
        svc = get_resource(base_output, "Service")
        ports = svc["spec"]["ports"]
        assert len(ports) == 1
        assert ports[0]["port"] == 8000
        assert ports[0]["targetPort"] == 8000
        assert ports[0]["name"] == "http"


class TestConfigOverrides:
    def test_resource_limits(self, base_input: dict) -> None:
        base_input["config"] = {
            "resourceRequests": {"cpu": "4", "memory": "16Gi"},
            "resourceLimits": {"memory": "16Gi"},
        }
        output = eval_sandbox(base_input)
        job = get_resource(output, "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["requests"] == {"cpu": "4", "memory": "16Gi"}
        assert container["resources"]["limits"] == {"memory": "16Gi"}

    def test_extra_env_vars(self, base_input: dict) -> None:
        base_input["config"] = {
            "extraEnv": [
                {"name": "BROKER_URL", "value": "http://broker:8080"},
                {"name": "MY_VAR", "value": "hello"},
            ],
        }
        output = eval_sandbox(base_input)
        job = get_resource(output, "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env["BROKER_URL"] == "http://broker:8080"
        assert env["MY_VAR"] == "hello"
        assert env["PORT"] == "8000"

    def test_extra_volumes(self, base_input: dict) -> None:
        base_input["config"] = {
            "extraVolumes": [
                {"name": "code", "persistentVolumeClaim": {"claimName": "code-pvc"}},
            ],
            "extraVolumeMounts": [
                {"name": "code", "mountPath": "/workspace/code"},
            ],
        }
        output = eval_sandbox(base_input)
        job = get_resource(output, "Job")
        volumes = job["spec"]["template"]["spec"]["volumes"]
        vol_names = [v["name"] for v in volumes]
        assert "code" in vol_names

        container = job["spec"]["template"]["spec"]["containers"][0]
        mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
        assert mounts["code"] == "/workspace/code"

    def test_extra_init_containers(self, base_input: dict) -> None:
        base_input["config"] = {
            "extraInitContainers": [
                {
                    "name": "git-clone",
                    "image": "alpine/git",
                    "command": ["git", "clone", "https://example.com/repo"],
                },
            ],
        }
        output = eval_sandbox(base_input)
        job = get_resource(output, "Job")
        init = job["spec"]["template"]["spec"]["initContainers"]
        assert len(init) == 1
        assert init[0]["name"] == "git-clone"
        assert init[0]["image"] == "alpine/git"

    def test_extra_labels(self, base_input: dict) -> None:
        base_input["config"] = {
            "extraLabels": {"team": "platform", "env": "dev"},
        }
        output = eval_sandbox(base_input)
        for item in output["items"]:
            labels = item["metadata"]["labels"]
            assert labels["team"] == "platform"
            assert labels["env"] == "dev"

    def test_custom_namespace(self) -> None:
        output = eval_sandbox({
            "sandbox_id": "ns-test",
            "namespace": "custom-ns",
            "image": "test:latest",
            "session_api_key": "key",
        })
        for item in output["items"]:
            assert item["metadata"]["namespace"] == "custom-ns"


class TestValidation:
    """Test that nix eval fails on invalid input (missing required fields)."""

    def test_missing_sandbox_id(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"namespace": "x", "image": "x", "session_api_key": "x"}, f)
            f.flush()
            try:
                result = subprocess.run(
                    ["nix", "eval", "--json", "--impure",
                     "--expr", f"(import {SANDBOX_EVAL_EXPR} {{ inputFile = {f.name}; }})"],
                    capture_output=True, text=True, timeout=30,
                )
                assert result.returncode != 0
            finally:
                os.unlink(f.name)

    def test_missing_namespace(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"sandbox_id": "x", "image": "x", "session_api_key": "x"}, f)
            f.flush()
            try:
                result = subprocess.run(
                    ["nix", "eval", "--json", "--impure",
                     "--expr", f"(import {SANDBOX_EVAL_EXPR} {{ inputFile = {f.name}; }})"],
                    capture_output=True, text=True, timeout=30,
                )
                assert result.returncode != 0
            finally:
                os.unlink(f.name)
