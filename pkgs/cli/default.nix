# OpenHands CLI — Terminal User Interface for OpenHands AI Agent
#
# Builds from the OpenHands/OpenHands-CLI repository.
# Entry points: `openhands` (main TUI), `openhands-acp` (ACP mode)
{ lib, pythonPackages, sdkPackages, fetchFromGitHub }:

let
  version = "1.13.1";

  src = fetchFromGitHub {
    owner = "OpenHands";
    repo = "OpenHands-CLI";
    rev = version;
    hash = "sha256-9uKM88qJzRSYXvkDjQRuIMtZZ1lYHibBgMNQN7XGtW0=";
  };

in
pythonPackages.buildPythonApplication {
  pname = "openhands-cli";
  inherit version src;
  pyproject = true;

  build-system = [ pythonPackages.hatchling ];

  dependencies = with pythonPackages; [
    sdkPackages.openhands-sdk
    sdkPackages.openhands-tools
    sdkPackages.openhands-workspace
    agent-client-protocol
    prompt-toolkit
    rich
    textual
    typer
    pydantic
    textual-autocomplete
    pyperclip
    httpx
    textual-serve
    streamingjson
  ];

  # CLI pins exact SDK versions that differ from our monorepo build
  pythonRelaxDeps = [
    "openhands-sdk"
    "openhands-tools"
    "openhands-workspace"
    "rich"
  ];

  doCheck = false;

  pythonImportsCheck = [
    "openhands_cli"
  ];

  meta = {
    description = "OpenHands CLI - Terminal User Interface for OpenHands AI Agent";
    homepage = "https://github.com/OpenHands/OpenHands-CLI";
    license = lib.licenses.mit;
    mainProgram = "openhands";
    maintainers = [ ];
  };
}
