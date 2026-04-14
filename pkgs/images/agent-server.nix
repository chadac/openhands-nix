# Build OCI container images for the OpenHands agent-server sandbox.
#
# Two variants mirror upstream:
#   - minimal: Python + Node.js + git + core tools + Nix
#   - full:    minimal + OpenVSCode Server + GitHub CLI + Docker CLI
#
# Both include the Nix package manager so packages can be installed
# dynamically at pod startup via the NIX_PACKAGES env var.
#
# Usage:
#   # Build the default (full) image:
#   nix build .#agent-server-image
#
#   # Build minimal:
#   nix build .#agent-server-image-minimal
#
#   # Run with dynamic packages:
#   docker run -e NIX_PACKAGES="nixpkgs#ripgrep" openhands-agent-server
#
#   # Or pre-bake extra packages into the image:
#   mkAgentServerImage { extraPackages = [ pkgs.nodejs ]; }
#
{ pkgs, lib, pythonPackages, sdkPackages, skillsDir }:

let
  # Python environment with agent-server and all SDK packages
  basePython = pythonPackages.python.withPackages (ps: [
    sdkPackages.openhands-sdk
    sdkPackages.openhands-tools
    sdkPackages.openhands-agent-server
    sdkPackages.openhands-workspace
    ps.boto3  # Required by litellm for AWS Bedrock
  ]);

  # Minimal system packages — matches upstream base-image-minimal
  baseSystemPackages = with pkgs; [
    coreutils
    bash
    gitMinimal  # git without git-p4 (avoids pulling in python 3.13)
    gnused
    gnugrep
    findutils
    gawk
    gnutar
    gzip
    xz
    which
    tmux        # required by openhands-tools libtmux
    procps      # ps, top — required by psutil
    util-linux  # mount — needed for overlay store setup
    curl
    wget
    jq
  ];

  # Additional packages for the full variant — matches upstream base-image
  fullPackages = with pkgs; [
    openvscode-server  # VSCode Web IDE
    gh                 # GitHub CLI
    docker-client      # Docker CLI (no daemon)
  ];

  entrypoint = pkgs.writeShellApplication {
    name = "openhands-entrypoint";
    runtimeInputs = baseSystemPackages ++ [ pkgs.nix basePython ];
    text = builtins.readFile ./entrypoint.sh;
  };

  # Lazy Chromium wrapper — downloads from Nix binary cache on first use.
  # Keeps the image small while supporting browser-use for web browsing.
  lazyChromium = pkgs.writeShellScriptBin "chromium" (builtins.readFile ./lazy-chromium.sh);

  # Lazy OpenVSCode Server wrapper — waits for nix profile install, then
  # sets up /openhands/.openvscode-server and execs the real binary.
  lazyVscode = pkgs.writeShellScriptBin "openvscode-server" (builtins.readFile ./lazy-vscode.sh);

  # Shared image builder used by both variants
  mkImage = {
    name ? "openhands-agent-server",
    tag ? "latest",
    extraPackages ? [],
    extraPythonPackages ? [],
    port ? 8000,
    variant ? "full",   # "full" or "minimal"
  }:
  let
    python = if extraPythonPackages == [] then basePython
      else pythonPackages.python.withPackages (ps: [
        sdkPackages.openhands-sdk
        sdkPackages.openhands-tools
        sdkPackages.openhands-agent-server
        sdkPackages.openhands-workspace
      ] ++ extraPythonPackages);

    variantPackages = if variant == "full" then fullPackages else [];

    allPackages = baseSystemPackages ++ variantPackages ++ [
      python
      pkgs.nix       # Nix package manager for dynamic installs
      pkgs.cacert    # SSL certificates for fetching from caches
      entrypoint
      lazyChromium   # Lazy Chromium — fetched from Nix cache on first browser use
      lazyVscode     # Lazy OpenVSCode Server — installed via nix profile at pod startup
    ] ++ extraPackages;

    # OpenVSCode Server setup for fakeRootCommands (full variant only)
    vscodeSetup = lib.optionalString (variant == "full") ''
      # OpenVSCode Server: symlink into expected location
      mkdir -p ./openhands/.openvscode-server
      ln -s ${pkgs.openvscode-server}/lib/openvscode-server/* ./openhands/.openvscode-server/ 2>/dev/null || true
      # Make 'code' available as a command
      mkdir -p ./usr/local/bin
      ln -s ${pkgs.openvscode-server}/bin/openvscode-server ./usr/local/bin/code
    '';

    vscodeEnv = lib.optionals (variant == "full") [
      "EDITOR=code"
      "VISUAL=code"
      "GIT_EDITOR=code --wait"
      "OPENVSCODE_SERVER_ROOT=/openhands/.openvscode-server"
    ];
  in
  pkgs.dockerTools.buildLayeredImage {
    inherit name tag;

    contents = allPackages;

    fakeRootCommands = ''
      mkdir -p ./workspace/project ./tmp ./root ./etc ./nix/var/nix/db ./openhands
      chmod 1777 ./tmp

      # Nix needs these directories to function
      mkdir -p ./nix/var/nix/gcroots
      mkdir -p ./nix/var/nix/profiles
      mkdir -p ./root/.config/nix

      # Enable flakes and nix-command for dynamic installs
      cat > ./root/.config/nix/nix.conf <<NIXCONF
      experimental-features = nix-command flakes
      sandbox = false
      filter-syscalls = false
      NIXCONF

      # Basic /etc files — include nixbld users for Nix builds
      echo "root:x:0:0:root:/root:/bin/bash" > ./etc/passwd
      for i in $(seq 1 10); do
        echo "nixbld$i:x:$((30000 + i)):30000:Nix build user $i:/var/empty:/bin/nologin" >> ./etc/passwd
      done
      echo "root:x:0:" > ./etc/group
      echo "nixbld:x:30000:$(seq -s, 1 10 | sed 's/[0-9]*/nixbld&/g')" >> ./etc/group

      # Nix-specific skills for the agent
      mkdir -p ./root/.openhands/skills
      cp ${skillsDir}/*.md ./root/.openhands/skills/

      ${vscodeSetup}
    '';

    config = {
      Entrypoint = [ "${entrypoint}/bin/openhands-entrypoint" ];
      ExposedPorts = {
        "${toString port}/tcp" = {};
      };
      WorkingDir = "/workspace";
      Env = [
        "HOME=/root"
        "PATH=${lib.makeBinPath allPackages}:/root/.nix-profile/bin:/usr/local/bin:/usr/bin:/bin"
        "PORT=${toString port}"
        "HOST=0.0.0.0"
        "PYTHONDONTWRITEBYTECODE=1"
        "REPO_ROOT=/workspace"
        "LC_ALL=C.UTF-8"
        "LANG=C.UTF-8"
        "LOG_JSON=true"
        "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        # NIX_PACKAGES is set by KubernetesWorkspace at pod creation time
      ] ++ vscodeEnv;
    };
  };

in
{
  # Full variant (default) — matches upstream "base-image"
  mkAgentServerImage = args: mkImage ({ variant = "full"; } // args);

  # Minimal variant — matches upstream "base-image-minimal"
  mkAgentServerImageMinimal = args: mkImage ({ variant = "minimal"; } // args);
}
