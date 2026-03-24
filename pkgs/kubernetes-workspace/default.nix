# OpenHands Nix Workspaces
#
# Python package providing Nix-powered workspace backends:
#   - NixWorkspace: base config (packages, flake refs, nix expressions)
#   - KubernetesWorkspace: K8s Jobs with entrypoint-based nix install
#   - NixCSIWorkspace: K8s with nix-csi CSI driver (instant startup)
#   - LocalNixWorkspace: local dev wrapped in `nix shell`
#
# K8s backends require kubectl on PATH at runtime.
{ lib, pythonPackages, sdkPackages }:

pythonPackages.buildPythonPackage {
  pname = "openhands-nix";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ pythonPackages.setuptools ];

  dependencies = [
    sdkPackages.openhands-sdk
    pythonPackages.pydantic
  ];

  doCheck = false;

  pythonImportsCheck = [
    "openhands_nix"
    "openhands_nix.workspace"
    "openhands_nix.kubernetes"
    "openhands_nix.csi"
    "openhands_nix.local"
  ];

  meta = {
    description = "Nix-powered workspace backends for OpenHands";
    license = lib.licenses.mit;
  };
}
