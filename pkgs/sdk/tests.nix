# Segmented test derivations for OpenHands SDK monorepo.
#
# Each test derivation covers a single subdirectory or file, so you can
# run `nix build .#checks.x86_64-linux.sdk-tests-llm` without waiting
# for the entire suite.
#
# Usage from flake:
#   checks = import ./pkgs/sdk/tests.nix { ... };
{ lib, pythonPackages, sdkPackages, runCommand }:

let
  inherit (sdkPackages) openhands-sdk openhands-tools openhands-agent-server openhands-workspace;

  src = openhands-sdk.src;  # full monorepo source

  # Helper: create a test derivation that runs pytest on a specific path.
  #
  # The derivation copies the monorepo into a writable tmpdir, sets up
  # the Python environment with the built package + test deps, and runs
  # pytest on the given test path.
  mkTest = {
    name,                   # derivation name suffix (e.g. "sdk-llm")
    testPath,               # pytest path relative to repo root (e.g. "tests/sdk/llm")
    packages ? [],          # extra Python packages needed at test time
    extraIgnore ? [],       # --ignore paths
    disabledTests ? [],     # -k exclusions
    timeout ? 120,          # per-test timeout in seconds
    extraSetup ? "",        # extra shell in preCheck
  }:
  let
    python = pythonPackages.python.withPackages (ps: [
      # Test framework
      ps.pytest
      ps.pytest-asyncio
      ps.pytest-cov
      ps.pytest-timeout
      ps.psutil
      # The packages under test
      openhands-sdk
      openhands-tools
      openhands-agent-server
      openhands-workspace
    ] ++ packages);

    ignoreFlags = lib.concatMapStringsSep " " (p: "--ignore=${p}") extraIgnore;
    disableExpr =
      if disabledTests == [] then ""
      else "-k '${lib.concatStringsSep " and " (map (t: "not ${t}") disabledTests)}'";
  in
  runCommand "openhands-test-${name}" {
    nativeBuildInputs = [ python ];
  } ''
    # Copy source to writable location
    cp -r ${src} $TMPDIR/src
    chmod -R u+w $TMPDIR/src
    cd $TMPDIR/src

    export HOME=$TMPDIR
    export REPO_ROOT=$TMPDIR/src

    ${extraSetup}

    python -m pytest \
      ${testPath} \
      --timeout=${toString timeout} \
      -v --tb=short \
      ${ignoreFlags} \
      ${disableExpr} \
      || { echo "TESTS FAILED for ${name}"; exit 1; }

    # Nix needs an output
    touch $out
  '';

  # ============================================================
  # SDK tests — one derivation per subdirectory
  # ============================================================
  sdkTests = {
    sdk-agent = mkTest {
      name = "sdk-agent";
      testPath = "tests/sdk/agent";
    };
    sdk-config = mkTest {
      name = "sdk-config";
      testPath = "tests/sdk/config";
    };
    sdk-context = mkTest {
      name = "sdk-context";
      testPath = "tests/sdk/context";
    };
    sdk-conversation = mkTest {
      name = "sdk-conversation";
      testPath = "tests/sdk/conversation";
    };
    sdk-critic = mkTest {
      name = "sdk-critic";
      testPath = "tests/sdk/critic";
    };
    sdk-event = mkTest {
      name = "sdk-event";
      testPath = "tests/sdk/event";
    };
    # sdk-git — skipped: needs real git repo / git binary
    # sdk-hooks — skipped: needs subprocess execution
    sdk-io = mkTest {
      name = "sdk-io";
      testPath = "tests/sdk/io";
    };
    sdk-llm = mkTest {
      name = "sdk-llm";
      testPath = "tests/sdk/llm";
    };
    sdk-logger = mkTest {
      name = "sdk-logger";
      testPath = "tests/sdk/logger";
    };
    # sdk-mcp — skipped: needs external server processes
    sdk-plugin = mkTest {
      name = "sdk-plugin";
      testPath = "tests/sdk/plugin";
      extraIgnore = [
        "tests/sdk/plugin/test_plugin_fetch.py"
        "tests/sdk/plugin/test_plugin_fetch_integration.py"
      ];
    };
    sdk-security = mkTest {
      name = "sdk-security";
      testPath = "tests/sdk/security";
    };
    sdk-skills = mkTest {
      name = "sdk-skills";
      testPath = "tests/sdk/skills";
    };
    sdk-subagent = mkTest {
      name = "sdk-subagent";
      testPath = "tests/sdk/subagent";
    };
    sdk-tool = mkTest {
      name = "sdk-tool";
      testPath = "tests/sdk/tool";
    };
    sdk-utils = mkTest {
      name = "sdk-utils";
      testPath = "tests/sdk/utils";
    };
    sdk-workspace = mkTest {
      name = "sdk-workspace";
      testPath = "tests/sdk/workspace";
    };
  };

  # ============================================================
  # Tools tests
  # ============================================================
  toolsTests = {
    tools-apply-patch = mkTest {
      name = "tools-apply-patch";
      testPath = "tests/tools/apply_patch";
    };
    # tools-browser-use — skipped: needs Playwright/Chromium
    # tools-delegate — skipped: needs filesystem permissions
    tools-file-editor = mkTest {
      name = "tools-file-editor";
      testPath = "tests/tools/file_editor";
      disabledTests = [
        # MemoryError in sandbox
        "test_to_mcp_tool_detailed_type_validation_editor"
      ];
    };
    tools-glob = mkTest {
      name = "tools-glob";
      testPath = "tests/tools/glob";
    };
    tools-grep = mkTest {
      name = "tools-grep";
      testPath = "tests/tools/grep";
    };
    tools-terminal = mkTest {
      name = "tools-terminal";
      testPath = "tests/tools/terminal";
      extraIgnore = [
        # Cleanup tests timeout in sandbox
        "tests/tools/terminal/test_conversation_cleanup.py"
      ];
    };
    tools-misc = mkTest {
      name = "tools-misc";
      testPath = "tests/tools/test_builtin_agents.py tests/tools/test_init.py tests/tools/test_tool_name_consistency.py tests/tools/test_working_dir_standardization.py";
    };
  };

  # ============================================================
  # Agent server tests — single derivation (flat directory)
  # ============================================================
  agentServerTests = {
    agent-server = mkTest {
      name = "agent-server";
      testPath = "tests/agent_server";
      packages = with pythonPackages; [ httpx ];
      disabledTests = [
        # Needs Docker daemon
        "docker"
      ];
    };
  };

  # ============================================================
  # Workspace tests — single derivation (flat directory)
  # ============================================================
  workspaceTests = {
    workspace = mkTest {
      name = "workspace";
      testPath = "tests/workspace";
      disabledTests = [
        # Needs Docker daemon
        "docker"
      ];
    };
  };

in
  sdkTests // toolsTests // agentServerTests // workspaceTests
