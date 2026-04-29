{ lib, pythonPackages, openhands-common }:

pythonPackages.buildPythonApplication {
  pname = "openhands-lifecycle";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ pythonPackages.setuptools ];

  dependencies = [
    openhands-common
    pythonPackages.fastapi
    pythonPackages.uvicorn
    pythonPackages.httpx
    pythonPackages.kubernetes
  ];

  doCheck = false;

  pythonImportsCheck = [
    "openhands_lifecycle"
    "openhands_lifecycle.app"
    "openhands_lifecycle.config"
    "openhands_lifecycle.cleanup"
    "openhands_lifecycle.k8s"
  ];

  meta = {
    description = "Lifecycle manager for OpenHands sandbox resources";
    license = lib.licenses.mit;
  };
}
