{ lib, pythonPackages }:

pythonPackages.buildPythonPackage {
  pname = "openhands-common";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ pythonPackages.setuptools ];

  dependencies = [
    pythonPackages.sqlalchemy
    pythonPackages.asyncpg
    pythonPackages.aio-pika
    pythonPackages.pydantic
    pythonPackages.pydantic-settings
  ];

  doCheck = false;

  pythonImportsCheck = [
    "openhands_common"
    "openhands_common.config"
    "openhands_common.models"
    "openhands_common.db"
    "openhands_common.messaging"
  ];

  meta = {
    description = "Shared database and messaging layer for OpenHands services";
    license = lib.licenses.mit;
  };
}
