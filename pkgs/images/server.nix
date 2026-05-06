# Build an OCI container image for the full OpenHands server (UI + API).
#
# Uses nix2container for content-addressed layer dedup across images.
# Each nix store path becomes a separate OCI layer, so shared deps
# (Python, coreutils, nix, etc.) are only pushed/pulled once.
#
# Usage:
#   nix build .#server-image
#   # Push directly to registry (no docker daemon needed):
#   nix run .#server-image.copyToRegistry
#   # Or load into docker:
#   nix run .#server-image.copyToDockerDaemon
#
{ pkgs, lib, pythonPackages, sdkPackages, serverPackages, skillsDir, n2c }:

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
    nix
  ];

  allPackages = systemPackages ++ [ serverPython pkgs.cacert ];

  # Root filesystem overlay — replaces fakeRootCommands
  rootfs = pkgs.runCommand "openhands-server-rootfs" {} ''
    mkdir -p $out/app $out/workspace/project $out/tmp $out/root $out/etc $out/etc/nix
    chmod 1777 $out/tmp

    echo "root:x:0:0:root:/root:/bin/bash" > $out/etc/passwd
    echo "root:x:0:" > $out/etc/group

    # Nix configuration
    cat > $out/etc/nix/nix.conf <<'NIX_CONF'
    experimental-features = nix-command flakes
    sandbox = false
    NIX_CONF

    # Skills for the agent (from all skill directories)
    mkdir -p $out/root/.openhands/skills
    ${lib.concatMapStringsSep "\n" (d: "cp ${d}/*.md $out/root/.openhands/skills/") (lib.toList skillsDir)}
  '';

  entrypoint = pkgs.writeShellApplication {
    name = "openhands-server-entrypoint";
    runtimeInputs = systemPackages ++ [ serverPython ];
    text = ''
      PORT="''${PORT:-3000}"
      HOST="''${HOST:-0.0.0.0}"

      # Set up the frontend build directory where the server expects it.
      mkdir -p /app/frontend
      ln -sfn ${frontend} /app/frontend/build

      cd /app

      # Seed default settings via API after server starts (skips UI setup wizard).
      if [ "''${SEED_SETTINGS:-true}" = "true" ]; then
        (
          for _ in $(seq 1 60); do
            if python -c "import urllib.request; urllib.request.urlopen('http://localhost:''${PORT}/api/options/models')" 2>/dev/null; then
              break
            fi
            sleep 1
          done
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

  # Symlink bin paths into /bin for container PATH
  rootBinEnv = pkgs.buildEnv {
    name = "server-root-env";
    paths = allPackages ++ [ entrypoint ];
    pathsToLink = [ "/bin" "/etc/ssl" "/share" ];
  };

  # System packages layer — changes rarely (only when adding/updating system tools).
  # This is a large layer (~200MB) that stays cached across most pushes.
  systemLayer = n2c.buildLayer {
    deps = systemPackages ++ [ pkgs.cacert pkgs.nix ];
  };

in {
  inherit entrypoint;

  mkServerImage = {
    name ? "openhands-server",
    tag ? "latest",
    port ? 3000,
  }:
  n2c.buildImage {
    inherit name tag;
    initializeNixDatabase = true;
    maxLayers = 80;

    layers = [ systemLayer ];
    copyToRoot = [ rootfs rootBinEnv ];

    config = {
      Entrypoint = [ "${entrypoint}/bin/openhands-server-entrypoint" ];
      ExposedPorts = {
        "${toString port}/tcp" = {};
      };
      WorkingDir = "/app";
      Env = [
        "HOME=/root"
        "PATH=${lib.makeBinPath (allPackages ++ [ entrypoint ])}:/usr/bin:/bin"
        "PORT=${toString port}"
        "HOST=0.0.0.0"
        "PYTHONDONTWRITEBYTECODE=1"
        "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "RUNTIME=local"
        "ENABLE_BROWSER=false"
        "SKIP_DEPENDENCY_CHECK=1"
      ];
    };
  };
}
