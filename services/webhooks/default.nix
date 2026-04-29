{ lib, pythonPackages, openhands-common }:

pythonPackages.buildPythonPackage {
  pname = "openhands-webhooks";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ pythonPackages.setuptools ];

  dependencies = [
    openhands-common
    pythonPackages.fastapi
    pythonPackages.uvicorn
    pythonPackages.httpx
    pythonPackages.pyjwt
    pythonPackages.cryptography
  ];

  doCheck = false;

  pythonImportsCheck = [
    "openhands_webhooks"
    "openhands_webhooks.app"
    "openhands_webhooks.config"
    "openhands_webhooks.openhands_client"
    "openhands_webhooks.status_monitor"
  ];

  meta = {
    description = "Inbound webhook handler for OpenHands conversations";
    license = lib.licenses.mit;
  };
}
