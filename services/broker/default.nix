{ lib, pythonPackages, openhands-common }:

pythonPackages.buildPythonPackage {
  pname = "openhands-broker";
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
    "openhands_broker"
    "openhands_broker.app"
    "openhands_broker.config"
    "openhands_broker.auth"
    "openhands_broker.proxy"
  ];

  meta = {
    description = "Credential-injecting transparent proxy for OpenHands agents";
    license = lib.licenses.mit;
  };
}
