# Build an OCI container image for the OpenHands agent-server.
#
# The image includes the Nix package manager itself, so packages can
# be installed dynamically at pod startup via the NIX_PACKAGES env var.
# The base Nix store is baked into the image layers (read-only); an
# overlay store is set up at runtime for new packages.
#
# Usage:
#   # Build the base image:
#   nix build .#agent-server-image
#
#   # Run with dynamic packages:
#   docker run -e NIX_PACKAGES="nixpkgs#nodejs nixpkgs#ripgrep" agent-server
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
  ]);

  # Minimal system packages always present in the workspace
  baseSystemPackages = with pkgs; [
    coreutils
    bash
    git
    gnused
    gnugrep
    findutils
    gawk
    gnutar
    gzip
    xz
    which
    tmux   # required by openhands-tools libtmux
    procps # required by openhands-tools psutil
    # Needed for overlay store setup
    util-linux # mount
  ];

  entrypoint = pkgs.writeShellApplication {
    name = "openhands-entrypoint";
    runtimeInputs = baseSystemPackages ++ [ pkgs.nix basePython ];
    text = builtins.readFile ./entrypoint.sh;
  };

in
{
  # Build a customized agent-server image.
  #
  # Arguments:
  #   name            - image name (default: "openhands-agent-server")
  #   tag             - image tag (default: "latest")
  #   extraPackages   - additional Nix packages to bake in (e.g. [ pkgs.nodejs ])
  #   extraPythonPackages - additional Python packages to bake in
  #   port            - port the agent-server listens on (default: 8000)
  mkAgentServerImage = {
    name ? "openhands-agent-server",
    tag ? "latest",
    extraPackages ? [],
    extraPythonPackages ? [],
    port ? 8000,
  }:
  let
    python = if extraPythonPackages == [] then basePython
      else pythonPackages.python.withPackages (ps: [
        sdkPackages.openhands-sdk
        sdkPackages.openhands-tools
        sdkPackages.openhands-agent-server
        sdkPackages.openhands-workspace
      ] ++ extraPythonPackages);

    allPackages = baseSystemPackages ++ [
      python
      pkgs.nix       # Nix package manager for dynamic installs
      pkgs.cacert    # SSL certificates for fetching from caches
      entrypoint
    ] ++ extraPackages;
  in
  pkgs.dockerTools.buildLayeredImage {
    inherit name tag;

    contents = allPackages;

    fakeRootCommands = ''
      mkdir -p ./workspace ./tmp ./root ./etc ./nix/var/nix/db
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

      # Basic /etc files
      echo "root:x:0:0:root:/root:/bin/bash" > ./etc/passwd
      echo "root:x:0:" > ./etc/group
      echo "nixbld:x:30000:" >> ./etc/group

      # Nix-specific skills for the agent
      mkdir -p ./root/.openhands/skills
      cp ${skillsDir}/*.md ./root/.openhands/skills/
    '';

    config = {
      Entrypoint = [ "${entrypoint}/bin/openhands-entrypoint" ];
      ExposedPorts = {
        "${toString port}/tcp" = {};
      };
      WorkingDir = "/workspace";
      Env = [
        "HOME=/root"
        "PATH=${lib.makeBinPath allPackages}:/root/.nix-profile/bin:/usr/bin:/bin"
        "PORT=${toString port}"
        "HOST=0.0.0.0"
        "PYTHONDONTWRITEBYTECODE=1"
        "REPO_ROOT=/workspace"
        "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        # NIX_PACKAGES is set by KubernetesWorkspace at pod creation time
      ];
    };
  };
}
