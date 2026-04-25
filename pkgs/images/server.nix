# Build an OCI container image for the full OpenHands server (UI + API).
#
# Combines the Python backend (openhands-ai) with the React frontend
# static assets. Runs uvicorn on port 3000 serving both API and SPA.
#
# Usage:
#   nix build .#server-image
#   docker load < result
#   docker run -p 3000:3000 -e RUNTIME=process openhands-server:latest
#
{ pkgs, lib, pythonPackages, sdkPackages, serverPackages, skillsDir }:

let
  # Python environment with the full server + SDK
  serverPython = pythonPackages.python.withPackages (ps: [
    serverPackages.backend
    sdkPackages.openhands-sdk
    sdkPackages.openhands-tools
    sdkPackages.openhands-agent-server
    sdkPackages.openhands-workspace
  ]);

  frontend = serverPackages.frontend;

  # System packages needed at runtime
  systemPackages = with pkgs; [
    coreutils bash git gnused gnugrep findutils gawk
    gnutar gzip xz which tmux procps util-linux
    # Nix CLI for the nix runtime (nix shell, nix develop, etc.)
    nix
  ];

  entrypoint = pkgs.writeShellApplication {
    name = "openhands-server-entrypoint";
    runtimeInputs = systemPackages ++ [ serverPython ];
    text = ''
      PORT="''${PORT:-3000}"
      HOST="''${HOST:-0.0.0.0}"

      # Set up the frontend build directory where the server expects it.
      # The server mounts SPA static files from ./frontend/build/ (relative to CWD).
      mkdir -p /app/frontend
      ln -sfn ${frontend} /app/frontend/build

      cd /app

      # Seed default settings via API after server starts (skips UI setup wizard).
      # Settings are stored in SQLite, so we POST them once the server is ready.
      if [ "''${SEED_SETTINGS:-true}" = "true" ]; then
        (
          # Wait for server to accept connections
          for _ in $(seq 1 60); do
            if python -c "import urllib.request; urllib.request.urlopen('http://localhost:''${PORT}/api/options/models')" 2>/dev/null; then
              break
            fi
            sleep 1
          done
          # Only seed if no settings exist yet
          EXISTING=$(python -c "
      import urllib.request, json
      try:
          r = urllib.request.urlopen('http://localhost:''${PORT}/api/settings')
          data = json.loads(r.read())
          print('exists')
      except Exception:
          print('missing')
      " 2>/dev/null)
          if [ "$EXISTING" = "missing" ]; then
            python -c "
      import urllib.request, json
      settings = {
          'language': 'en',
          'agent': 'CodeActAgent',
          'max_iterations': 100,
          'security_analyzer': None,
          'confirmation_mode': False,
          'llm_model': '$(printenv LLM_MODEL 2>/dev/null || echo bedrock/anthropic.claude-sonnet-4-20250514)',
          'llm_api_key': '$(printenv LLM_API_KEY 2>/dev/null || echo unused)',
          'llm_base_url': None,
          'remote_runtime_resource_factor': 1,
          'enable_default_condenser': True,
          'enable_sound_notifications': False,
          'enable_proactive_conversation_starters': True,
          'user_consents_to_analytics': False,
      }
      data = json.dumps(settings).encode()
      req = urllib.request.Request(
          'http://localhost:''${PORT}/api/settings',
          data=data,
          headers={'Content-Type': 'application/json'},
          method='POST',
      )
      urllib.request.urlopen(req)
      print('[entrypoint] Seeded default settings via API')
      " 2>&1
          else
            echo "[entrypoint] Settings already exist, skipping seed"
          fi
        ) &
      fi

      echo "[entrypoint] Starting OpenHands server on ''${HOST}:''${PORT}"
      exec uvicorn openhands.server.listen:app \
        --host "$HOST" --port "$PORT" "$@"
    '';
  };

in {
  # Standalone server environment for nix-csi mode
  inherit entrypoint;

  mkServerImage = {
    name ? "openhands-server",
    tag ? "latest",
    port ? 3000,
  }:
  pkgs.dockerTools.buildLayeredImage {
    inherit name tag;

    contents = systemPackages ++ [
      serverPython
      pkgs.cacert
      entrypoint
    ];

    fakeRootCommands = ''
      mkdir -p ./app ./workspace ./workspace/project ./tmp ./root ./etc ./etc/nix
      chmod 1777 ./tmp

      echo "root:x:0:0:root:/root:/bin/bash" > ./etc/passwd
      echo "root:x:0:" > ./etc/group

      # Nix configuration for the container
      cat > ./etc/nix/nix.conf <<'NIX_CONF'
      experimental-features = nix-command flakes
      sandbox = false
      NIX_CONF

      # Nix-specific skills for the agent
      mkdir -p ./root/.openhands/skills
      cp ${skillsDir}/*.md ./root/.openhands/skills/
    '';

    config = {
      Entrypoint = [ "${entrypoint}/bin/openhands-server-entrypoint" ];
      ExposedPorts = {
        "${toString port}/tcp" = {};
      };
      WorkingDir = "/app";
      Env = [
        "HOME=/root"
        "PATH=${lib.makeBinPath (systemPackages ++ [ serverPython pkgs.cacert entrypoint ])}:/usr/bin:/bin"
        "PORT=${toString port}"
        "HOST=0.0.0.0"
        "PYTHONDONTWRITEBYTECODE=1"
        "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        # Default to local runtime (no docker-in-docker needed)
        "RUNTIME=local"
        # Disable browser automation (no Playwright binaries in this image)
        "ENABLE_BROWSER=false"
        "SKIP_DEPENDENCY_CHECK=1"
      ];
    };
  };
}
