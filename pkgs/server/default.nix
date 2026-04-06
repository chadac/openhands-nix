# OpenHands Server (openhands-ai)
#
# The main OpenHands application: FastAPI backend + React frontend.
# Serves the web UI on port 3000, manages agent sessions via Socket.IO,
# and delegates work to agent-server instances.
#
# Built from All-Hands-AI/OpenHands main branch (targeting SDK 1.14).
# Some heavy optional deps are removed (playwright, browsergym, pythonnet,
# google-cloud-aiplatform) — these are for browser automation and Vertex AI.
{ lib, pythonPackages, sdkPackages, fetchFromGitHub, buildNpmPackage, nodejs }:

let
  version = "1.5.0-dev";
  rev = "1f275a7cfe91bf8ca090430b49dcae3c5392fe7d";

  src = fetchFromGitHub {
    owner = "All-Hands-AI";
    repo = "OpenHands";
    inherit rev;
    hash = "sha256-pHXuhIrTum13sgmo3HCTLNX/XaEwoACS65WS/cROqkY=";
  };

  meta = {
    homepage = "https://github.com/All-Hands-AI/OpenHands";
    license = lib.licenses.mit;
    description = "OpenHands: Code Less, Make More — AI agent platform";
    maintainers = [ ];
  };

  # ---- Frontend (React + Vite) ----
  frontend = buildNpmPackage {
    pname = "openhands-frontend";
    inherit version src meta;
    sourceRoot = "source/frontend";

    npmDepsHash = "sha256-JHiq0wZZ8nC/LAaTvku515+d256VdfIg37TqWt8Rs5s=";
    nodejs = nodejs;

    buildPhase = ''
      runHook preBuild
      npm run build
      runHook postBuild
    '';

    installPhase = ''
      runHook preInstall
      cp -r build $out
      runHook postInstall
    '';
  };

  # ---- Backend (Python / FastAPI) ----
  backend = pythonPackages.buildPythonPackage {
    pname = "openhands-ai";
    inherit version src meta;
    pyproject = true;

    build-system = [ pythonPackages.poetry-core ];

    # Patch version since poetry-dynamic-versioning isn't available.
    # The pyproject.toml has both [project] (PEP 621) and [tool.poetry] sections.
    # We need to set version in both and remove the dynamic marker.
    postPatch = ''
      substituteInPlace pyproject.toml \
        --replace-fail 'dynamic = [ "version" ]' 'version = "${version}"' \
        --replace-fail 'version = "1.5.0"' 'version = "${version}"'
    '';

    # Remove deps that are hard to package or not needed for core functionality:
    # - playwright/browsergym-core: browser automation (needs browser binaries)
    # - pythonnet: .NET interop
    # - google-cloud-aiplatform: Vertex AI (massive dep tree, not in nixpkgs)
    # - jupyter-*: notebook features
    # - poetry: only used for version introspection
    pythonRemoveDeps = [
      "pythonnet"
      "google-cloud-aiplatform"
      "jupyter-kernel-gateway"
      "ipywidgets"
      "qtconsole"
      "poetry"
      "playwright"
      "browsergym-core"
    ];

    # Relax version pins where nixpkgs has slightly different versions
    pythonRelaxDeps = [
      "openai"
      "anyio"
      "zope-interface"
      "python-socketio"
      "protobuf"
      "starlette"
      "orjson"
      "pillow"
      "pyjwt"
      "pypdf"
      "python-multipart"
      "redis"
    ];

    dependencies = with pythonPackages; [
      # SDK packages
      sdkPackages.openhands-sdk
      sdkPackages.openhands-agent-server
      sdkPackages.openhands-tools

      # Web framework
      fastapi
      uvicorn
      starlette
      aiohttp
      python-socketio
      sse-starlette
      python-multipart

      # Database
      sqlalchemy
      asyncpg
      pg8000

      # Auth & security
      authlib
      jwcrypto
      pyjwt

      # AI / LLM
      litellm
      openai
      anthropic
      google-genai
      lmnr

      # Telemetry
      opentelemetry-api
      opentelemetry-exporter-otlp-proto-grpc

      # Cloud & infra
      boto3
      kubernetes
      docker
      redis

      # Utilities
      anyio
      deprecation
      deprecated
      dirhash
      httpx-aiohttp
      html2text
      jinja2
      joblib
      json-repair
      numpy
      orjson
      pathspec
      pexpect
      pillow
      prompt-toolkit
      protobuf
      psutil
      pybase62
      pygithub
      pypdf
      python-docx
      python-dotenv
      python-frontmatter
      python-json-logger
      python-pptx
      pylatexenc
      pyyaml
      rapidfuzz
      requests
      setuptools
      shellingham
      tenacity
      termcolor
      toml
      tornado
      types-toml
      urllib3
      whatthepatch
      zope-interface

      # Agent tools
      bashlex
      openhands-aci
      memory-profiler

      # Google APIs
      google-api-python-client
      google-auth-httplib2
      google-auth-oauthlib
      google-cloud-storage

      # MCP
      mcp

      # Jupyter (needed by local runtime's dependency check)
      jupyter-core
    ];

    doCheck = false;

    # Install our custom Nix runtime and register it
    postInstall = ''
      SITE=$(find $out -type d -name site-packages | head -1)

      # Install the NixRuntime module
      mkdir -p $SITE/openhands/runtime/impl/nix
      cp ${./nix_runtime.py} $SITE/openhands/runtime/impl/nix/nix_runtime.py
      cat > $SITE/openhands/runtime/impl/nix/__init__.py <<'PYEOF'
from openhands.runtime.impl.nix.nix_runtime import NixRuntime

__all__ = ['NixRuntime']
PYEOF

      # Register the 'nix' runtime in the registry
      substituteInPlace $SITE/openhands/runtime/__init__.py \
        --replace-fail \
          "from openhands.runtime.impl.local.local_runtime import LocalRuntime" \
          "from openhands.runtime.impl.local.local_runtime import LocalRuntime
from openhands.runtime.impl.nix.nix_runtime import NixRuntime" \
        --replace-fail \
          "'cli': CLIRuntime," \
          "'cli': CLIRuntime,
    'nix': NixRuntime,"

      # Make V1 app_server recognize 'nix' runtime as process-based (no Docker needed)
      substituteInPlace $SITE/openhands/app_server/config.py \
        --replace-fail \
          "os.getenv('RUNTIME') in ('local', 'process')" \
          "os.getenv('RUNTIME') in ('local', 'process', 'nix')"

      # Remove browsing agents from agenthub imports (browsergym not packaged).
      substituteInPlace $SITE/openhands/agenthub/__init__.py \
        --replace-fail \
          "    browsing_agent," \
          "    # browsing_agent,  # removed: requires browsergym" \
        --replace-fail \
          "    visualbrowsing_agent," \
          "    # visualbrowsing_agent,  # removed: requires browsergym" \
        --replace-fail \
          "    'browsing_agent'," \
          "    # 'browsing_agent'," \
        --replace-fail \
          "    'visualbrowsing_agent'," \
          "    # 'visualbrowsing_agent',"

      # Stub out BrowserTool (depends on browsergym which is not packaged).
      # Replace browser.py with a None stub so imports don't fail.
      cat > $SITE/openhands/agenthub/codeact_agent/tools/browser.py <<'PYEOF'
# Stubbed out: browsergym is not packaged
BrowserTool = None
PYEOF

      # Remove BrowserTool from tools __init__.py exports
      substituteInPlace $SITE/openhands/agenthub/codeact_agent/tools/__init__.py \
        --replace-fail \
          "from .browser import BrowserTool" \
          "from .browser import BrowserTool  # stubbed: returns None"

      # Guard BrowserTool usage in function_calling.py (skip when None)
      substituteInPlace $SITE/openhands/agenthub/codeact_agent/function_calling.py \
        --replace-fail \
          "elif tool_call.function.name == BrowserTool['function']['name']:" \
          "elif BrowserTool is not None and tool_call.function.name == BrowserTool['function']['name']:"

      # Guard BrowserTool in codeact_agent.py (skip append when None)
      substituteInPlace $SITE/openhands/agenthub/codeact_agent/codeact_agent.py \
        --replace-fail \
          "                tools.append(BrowserTool)" \
          "                if BrowserTool is not None: tools.append(BrowserTool)"

      # Install the openhands_nix extension package (Kubernetes sandbox service)
      mkdir -p $SITE/openhands_nix
      cat > $SITE/openhands_nix/__init__.py <<'PYEOF'
"""OpenHands Nix extensions — Kubernetes sandbox and other Nix-specific integrations."""
PYEOF
      cp ${./kubernetes_sandbox.py} $SITE/openhands_nix/kubernetes_sandbox.py
      cp ${./workspace-pvc-template.yaml} $SITE/openhands_nix/workspace-pvc-template.yaml
      cp ${./strip_prefix_middleware.py} $SITE/openhands_nix/strip_prefix_middleware.py

      # Patch config_from_env() to recognize RUNTIME=kubernetes.
      # Insert kubernetes sandbox branch before the Docker else fallback.
      substituteInPlace $SITE/openhands/app_server/config.py \
        --replace-fail \
          "config.sandbox = ProcessSandboxServiceInjector()
        else:" \
          "config.sandbox = ProcessSandboxServiceInjector()
        elif os.getenv('RUNTIME') == 'kubernetes':
            from openhands_nix.kubernetes_sandbox import KubernetesSandboxServiceInjector
            config.sandbox = KubernetesSandboxServiceInjector()
        else:"

      # Insert kubernetes sandbox_spec branch before the Docker else fallback.
      substituteInPlace $SITE/openhands/app_server/config.py \
        --replace-fail \
          "config.sandbox_spec = ProcessSandboxSpecServiceInjector()
        else:" \
          "config.sandbox_spec = ProcessSandboxSpecServiceInjector()
        elif os.getenv('RUNTIME') == 'kubernetes':
            from openhands_nix.kubernetes_sandbox import KubernetesSandboxSpecServiceInjector
            config.sandbox_spec = KubernetesSandboxSpecServiceInjector()
        else:"

      # Fix ProcessSandboxSpecService: empty working_dir causes mkdir missing operand.
      # Set a proper path for the agent server project workspace directory.
      substituteInPlace $SITE/openhands/app_server/sandbox/process_sandbox_spec_service.py \
        --replace-fail \
          "working_dir=" \
          "working_dir='/workspace/project',  # was:"

      # Fix GitLab service: httpx.AsyncClient() has no timeout by default,
      # causing requests to hang indefinitely on slow/unreachable GitLab instances.
      substituteInPlace $SITE/openhands/integrations/gitlab/service/base.py \
        --replace-fail \
          "async with httpx.AsyncClient(verify=httpx_verify_option()) as client:" \
          "async with httpx.AsyncClient(verify=httpx_verify_option(), timeout=30.0) as client:"

      # Fix server-side health check: the upstream code parses conversation_url
      # (external, behind OAuth) to build /server_info URL. This causes 302
      # redirects through the ALB's OIDC middleware. Patch to use internal
      # cluster service URL instead.
      substituteInPlace $SITE/openhands/server/routes/manage_conversations.py \
        --replace-fail \
          "conversation_url = urlparse(app_conversation.conversation_url)" \
          "_ns = __import__('os').getenv('SANDBOX_K8S_NAMESPACE', 'openhands')" \
        --replace-fail \
          "sandbox_info_url = f'{str(conversation_url.scheme)}://{str(conversation_url.netloc)}/server_info'" \
          "sandbox_info_url = f'http://oh-sandbox-{app_conversation.sandbox_id}.{_ns}.svc.cluster.local:8000/server_info'"

      # Fix _get_agent_server_url: prefer AGENT_SERVER_INTERNAL (cluster-local)
      # over AGENT_SERVER (external ALB) for server-to-sandbox API calls.
      # The external URL goes through the ALB which requires OIDC auth,
      # causing 302 redirects. Internal URLs stay within the cluster.
      substituteInPlace $SITE/openhands/app_server/app_conversation/live_status_app_conversation_service.py \
        --replace-fail \
          'agent_server_url = next(
            exposed_url.url
            for exposed_url in exposed_urls
            if exposed_url.name == AGENT_SERVER
        )
        agent_server_url = replace_localhost_hostname_for_docker(agent_server_url)
        return agent_server_url' \
          'agent_server_url = next(
            (exposed_url.url for exposed_url in exposed_urls if exposed_url.name == "AGENT_SERVER_INTERNAL"),
            next((exposed_url.url for exposed_url in exposed_urls if exposed_url.name == AGENT_SERVER), ""),
        )
        if not agent_server_url:
            agent_server_url = next(exposed_url.url for exposed_url in exposed_urls if exposed_url.name == AGENT_SERVER)
        agent_server_url = replace_localhost_hostname_for_docker(agent_server_url)
        return agent_server_url'

      # Recreate missing sandboxes when a user opens a conversation.
      # The get_conversation endpoint is user-initiated (not polling), so
      # it's safe to trigger recreation here.  batch_get_sandboxes (used by
      # search/list) intentionally does NOT recreate.
      substituteInPlace $SITE/openhands/server/routes/manage_conversations.py \
        --replace-fail \
          'if app_conversation:
                if (
                    app_conversation.sandbox_status == SandboxStatus.RUNNING' \
          'if app_conversation:
                # Recreate sandbox if missing (user opened conversation)
                if app_conversation.sandbox_id and app_conversation.sandbox_status in (None, SandboxStatus.MISSING, SandboxStatus.ERROR):
                    try:
                        _svc = app_conversation_service.sandbox_service
                        _recreated = await _svc.get_sandbox(app_conversation.sandbox_id)
                        if _recreated:
                            app_conversation = await app_conversation_service.get_app_conversation(conversation_uuid)
                    except Exception:
                        pass
                if (
                    app_conversation and app_conversation.sandbox_status == SandboxStatus.RUNNING'

      # Fix SPAStaticFiles crash on WebSocket connections.
      # The catch-all static file mount at "/" receives WebSocket scopes when
      # Socket.IO doesn't match the path. StaticFiles asserts scope["type"] == "http"
      # which crashes the connection. Override __call__ to reject non-HTTP scopes.
      substituteInPlace $SITE/openhands/server/static.py \
        --replace-fail \
          'class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:' \
          'class SPAStaticFiles(StaticFiles):
    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope["type"] != "http":
            # Reject WebSocket (and other non-HTTP) connections gracefully
            # instead of hitting the assert in StaticFiles.__call__
            if scope["type"] == "websocket":
                await receive()  # consume websocket.connect
                await send({"type": "websocket.close", "code": 1000})
            return
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope: Scope) -> Response:'

      # Fix ProcessSandboxService bugs:
      # 1. _get_process_status: idle server is STATUS_SLEEPING, not STATUS_RUNNING
      # 2. _start_agent_process: stdout/stderr=PIPE without reading causes deadlock
      #    when pipe buffer fills up. Use DEVNULL instead.
      # 3. Early-exit error message referenced stderr.decode() which is None with DEVNULL.
      substituteInPlace $SITE/openhands/app_server/sandbox/process_sandbox_service.py \
        --replace-fail \
          "if status == psutil.STATUS_RUNNING:" \
          "if status in (psutil.STATUS_RUNNING, psutil.STATUS_SLEEPING):" \
        --replace-fail \
          "stdout=subprocess.PIPE," \
          "stdout=subprocess.DEVNULL," \
        --replace-fail \
          "stderr=subprocess.PIPE," \
          "stderr=subprocess.DEVNULL," \
        --replace-fail \
          "raise SandboxError(f'Agent process failed to start: {stderr.decode()}')" \
          "raise SandboxError(f'Agent process failed to start (exit code {process.returncode})')"
    '';

    pythonImportsCheck = [
      "openhands"
      "openhands.server"
    ];
  };

in {
  inherit frontend backend;
}
