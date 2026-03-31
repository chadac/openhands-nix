# browser-use and its dependencies not yet in nixpkgs.
#
# Packages: uuid7, bubus, cdp-use, browser-use
#
# browser-use has many hard dependencies on LLM provider SDKs
# (openai, anthropic, google-genai, groq, ollama) that OpenHands
# doesn't use through browser-use. We strip those via pythonRemoveDeps
# and patch the import sites to be lazy.
{ lib, pythonPackages, fetchurl }:

let
  uuid7 = pythonPackages.buildPythonPackage {
    pname = "uuid7";
    version = "0.1.0";
    pyproject = true;
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/source/u/uuid7/uuid7-0.1.0.tar.gz";
      hash = "sha256-jFeqMu50VtPMaMlcRTC8VxZG3vrAGJXPxzVFRJiUpjw=";
    };
    build-system = [ pythonPackages.setuptools ];
    doCheck = false;
    pythonImportsCheck = [ "uuid_extensions" ];
  };

  bubus = pythonPackages.buildPythonPackage {
    pname = "bubus";
    version = "1.5.6";
    pyproject = true;
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/source/b/bubus/bubus-1.5.6.tar.gz";
      hash = "sha256-GlRW8KV26GYTp71m6BmJG2d3eDILbikQlOM5sNnfLg0=";
    };
    build-system = [ pythonPackages.hatchling ];
    dependencies = with pythonPackages; [
      aiofiles
      anyio
      portalocker
      pydantic
      typing-extensions
      uuid7
    ];
    doCheck = false;
    pythonImportsCheck = [ "bubus" ];
  };

  cdp-use = pythonPackages.buildPythonPackage {
    pname = "cdp-use";
    version = "1.4.5";
    pyproject = true;
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/source/c/cdp-use/cdp_use-1.4.5.tar.gz";
      hash = "sha256-DaOjLfRjNqA/9aIrxrxELNfS8tUKEY/UhW8p039tJqA=";
    };
    build-system = [ pythonPackages.hatchling ];
    dependencies = with pythonPackages; [
      httpx
      typing-extensions
      websockets
    ];
    doCheck = false;
    pythonImportsCheck = [ "cdp_use" ];
  };

  browser-use = pythonPackages.buildPythonPackage {
    pname = "browser-use";
    version = "0.9.0";
    pyproject = true;
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/source/b/browser-use/browser_use-0.9.0.tar.gz";
      hash = "sha256-ScJgQ/ZMi5G4EyUGbcfkc7zOX5RNboVbu1bvxNVgPS8=";
    };
    build-system = [ pythonPackages.hatchling ];

    dependencies = with pythonPackages; [
      # Core browser functionality
      bubus
      cdp-use
      uuid7
      httpx
      anyio
      psutil
      pydantic
      pydantic-settings
      typing-extensions
      portalocker
      pillow
      markdownify
      python-dotenv

      # Used by mcp/server.py (BrowserUseServer) — OpenHands imports this
      mcp
      aiohttp
      requests
    ];

    # Strip LLM provider SDKs and other deps OpenHands doesn't use through
    # browser-use. OpenHands has its own LLM layer (litellm/bedrock).
    pythonRemoveDeps = [
      "anthropic"
      "openai"
      "google-genai"
      "google-api-core"
      "google-api-python-client"
      "google-auth"
      "google-auth-oauthlib"
      "groq"
      "ollama"
      "posthog"     # telemetry
      "authlib"     # OAuth — not used in headless sandbox
      "pyotp"       # OTP — not used in headless sandbox
      "pypdf"       # PDF — not used in headless sandbox
      "reportlab"   # PDF — not used in headless sandbox
      "screeninfo"  # display info — not used in headless sandbox
    ];

    # Relax strict version pins that conflict with nixpkgs versions
    pythonRelaxDeps = [
      "aiohttp"
      "portalocker"
    ];

    # Patch out eager imports of stripped LLM providers and telemetry so
    # they don't cause ImportErrors at module load time.
    # OpenHands only uses BrowserUseServer as a CDP wrapper — it doesn't
    # use the Agent, LLM, or telemetry features from browser-use.
    postPatch = ''
      # Only patch the 2 files in OpenHands' import chain. Other files
      # (agent/, llm/, cli.py etc.) are never imported so they don't matter.

      # 1. mcp/__init__.py eagerly imports MCPClient/MCPToolWrapper which
      #    chain to agent.views → openai. OpenHands only needs BrowserUseServer
      #    which is already lazy via __getattr__.
      sed -i \
        -e 's/^from browser_use.mcp.client/# &/' \
        -e 's/^from browser_use.mcp.controller/# &/' \
        browser_use/mcp/__init__.py

      # 2. mcp/server.py imports Agent, ChatOpenAI, telemetry etc. at module
      #    level. Comment out lines that reference stripped packages.
      #    Use line-anchored patterns (^) so we don't hit indented code.
      sed -i \
        -e 's/^from browser_use import ActionModel, Agent/# &/' \
        -e 's/^from browser_use.llm/# &/' \
        -e 's/^from browser_use.tools.service/# &/' \
        -e 's/^from browser_use.telemetry/# &/' \
        -e 's/^from browser_use.filesystem/# &/' \
        browser_use/mcp/server.py

      # Stub out the telemetry init (self._telemetry = ProductTelemetry())
      sed -i \
        -e 's/self._telemetry = ProductTelemetry()/self._telemetry = None/' \
        browser_use/mcp/server.py

      # The MCP SDK import block has sys.exit(1) on failure. When browser_use
      # is imported as a submodule, 'import mcp.server.stdio' can fail due to
      # namespace collision. Change sys.exit(1) to just log — OpenHands uses
      # its own BrowserUseServer wrapper anyway.
      substituteInPlace browser_use/mcp/server.py \
        --replace-fail \
          "logger.error('MCP SDK not installed. Install with: pip install mcp')" \
          "logger.warning('MCP SDK import failed (namespace collision?) - BrowserUseServer may have limited functionality')" \
        --replace-fail \
          $'\tsys.exit(1)' \
          $'\tpass  # Don'"'"'t exit — OpenHands handles MCP separately'
    '';

    doCheck = false;

    # Only check the modules OpenHands actually uses — the full package
    # would fail due to stripped LLM imports.
    pythonImportsCheck = [
      "browser_use.dom"
      "browser_use.browser"
    ];
  };

in
{
  inherit uuid7 bubus cdp-use browser-use;
}
