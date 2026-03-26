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
{ lib, pythonPackages, fetchFromGitHub, browserDeps }:

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
      browserDeps.browser-use
    ];

    # Keep browser_tool_set out of the default agent — Chromium is fetched
    # lazily on first use and we don't want every conversation to trigger
    # a download. Browser tools are still registered and available on request.
    postPatch = ''
      substituteInPlace openhands/tools/preset/subagents/default.md \
        --replace-fail "  - browser_tool_set" ""
    '';

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

    # Add StripPrefixMiddleware — AWS ALB doesn't rewrite paths, so when routing
    # /sandbox/<id>/health to the pod, the pod receives the full path. This
    # middleware strips the OH_WEB_URL path prefix before FastAPI routes see it.
    postPatch = ''
      # Copy strip_prefix_middleware into the agent_server package
      cp ${../server/strip_prefix_middleware.py} openhands/agent_server/strip_prefix_middleware.py

      # Patch api.py to add StripPrefixMiddleware after CORS middleware
      substituteInPlace openhands/agent_server/api.py \
        --replace-fail \
          'app.add_middleware(LocalhostCORSMiddleware, allow_origins=config.allow_cors_origins)' \
          'app.add_middleware(LocalhostCORSMiddleware, allow_origins=config.allow_cors_origins)
          # Strip path prefix for ALB-based routing (e.g. /sandbox/<id>/ -> /)
          from openhands.agent_server.strip_prefix_middleware import StripPrefixMiddleware, get_strip_prefix
          _prefix = get_strip_prefix()
          if _prefix:
              app.add_middleware(StripPrefixMiddleware, prefix=_prefix)'
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
