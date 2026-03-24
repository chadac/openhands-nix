# OpenHands Software Agent SDK
#
# Builds the four packages from the OpenHands/software-agent-sdk monorepo:
#   - openhands-sdk       (core agent functionality)
#   - openhands-tools     (runtime tools: bash, browser, file editing)
#   - openhands-agent-server (FastAPI server for agents)
#   - openhands-workspace (Docker workspace management)
#
# Tests are disabled by default (doCheck = false) because the monorepo
# test suite has many sandbox-incompatible tests (git, docker, network).
# Import checks validate that packages install correctly.
# browser-use is skipped for now (requires Playwright/Chromium).
{ lib, pythonPackages, fetchFromGitHub }:

let
  version = "1.14.0";

  src = fetchFromGitHub {
    owner = "OpenHands";
    repo = "software-agent-sdk";
    rev = "v${version}";
    hash = "sha256-+8mRM+sSEOwiPnVuRUVoSOqbGNzYy7Dx67Zkl+673TA=";
  };

  meta = {
    homepage = "https://github.com/OpenHands/software-agent-sdk";
    license = lib.licenses.mit;
    maintainers = [ ];
  };

  openhands-sdk = pythonPackages.buildPythonPackage {
    pname = "openhands-sdk";
    inherit version src meta;
    pyproject = true;
    sourceRoot = "source/openhands-sdk";
    build-system = [ pythonPackages.setuptools ];

    dependencies = with pythonPackages; [
      agent-client-protocol
      deprecation
      fakeredis
      fastmcp
      filelock
      httpx
      litellm
      lmnr
      pydantic
      python-frontmatter
      python-json-logger
      tenacity
      websockets
    ];

    doCheck = false;

    pythonImportsCheck = [
      "openhands.sdk"
      "openhands.sdk.agent"
      "openhands.sdk.llm"
      "openhands.sdk.event"
    ];
  };

  openhands-tools = pythonPackages.buildPythonPackage {
    pname = "openhands-tools";
    inherit version src meta;
    pyproject = true;
    sourceRoot = "source/openhands-tools";
    build-system = [ pythonPackages.setuptools ];

    dependencies = with pythonPackages; [
      openhands-sdk
      bashlex
      binaryornot
      cachetools
      libtmux
      psutil
      pydantic
      func-timeout
      tom-swe
      # browser-use intentionally skipped — requires Playwright/Chromium
    ];

    pythonRemoveDeps = [ "browser-use" ];
    doCheck = false;

    pythonImportsCheck = [
      "openhands.tools"
    ];
  };

  openhands-agent-server = pythonPackages.buildPythonPackage {
    pname = "openhands-agent-server";
    inherit version src meta;
    pyproject = true;
    sourceRoot = "source/openhands-agent-server";
    build-system = [ pythonPackages.setuptools ];

    dependencies = with pythonPackages; [
      aiosqlite
      alembic
      docker
      fastapi
      openhands-sdk
      openhands-tools
      pydantic
      sqlalchemy
      uvicorn
      websockets
      wsproto
    ];

    # Disable browser tools — browser-use requires Playwright/Chromium binaries
    # which we don't package. The enable_browser flag gates all browser_use imports.
    postPatch = ''
      substituteInPlace openhands/agent_server/tool_router.py \
        --replace-fail "enable_browser=True" "enable_browser=False"
      substituteInPlace openhands/agent_server/__main__.py \
        --replace-fail "enable_browser=True" "enable_browser=False"
      substituteInPlace openhands/agent_server/conversation_router.py \
        --replace-fail "enable_browser=True" "enable_browser=False"
    '';

    doCheck = false;

    pythonImportsCheck = [
      "openhands.agent_server"
    ];
  };

  openhands-workspace = pythonPackages.buildPythonPackage {
    pname = "openhands-workspace";
    inherit version src meta;
    pyproject = true;
    sourceRoot = "source/openhands-workspace";
    build-system = [ pythonPackages.setuptools ];

    dependencies = with pythonPackages; [
      openhands-sdk
      openhands-agent-server
      pydantic
    ];

    doCheck = false;

    pythonImportsCheck = [
      "openhands.workspace"
    ];
  };

in
{
  inherit openhands-sdk openhands-tools openhands-agent-server openhands-workspace;
}
