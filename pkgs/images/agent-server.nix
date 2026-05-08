# Build OCI container images for the OpenHands agent-server sandbox.
#
# Uses nix2container for content-addressed layer dedup. Shared deps
# between server and agent-server images are only pushed/pulled once.
#
# Two variants mirror upstream:
#   - minimal: Python + Node.js + git + core tools + Nix
#   - full:    minimal + OpenVSCode Server + GitHub CLI + Docker CLI
#
# Both include the Nix package manager so packages can be installed
# dynamically at pod startup via the NIX_PACKAGES env var.
#
# Usage:
#   nix build .#agent-server-image
#   nix run .#agent-server-image.copyToRegistry
#
{ pkgs, lib, pythonPackages, sdkPackages, serverPackages, skillsDir, n2c }:

let
  # Python environment with agent-server, SDK, and upstream runtime
  # (upstream kubernetes_runtime runs `python -m openhands.runtime.action_execution_server`
  # inside sandbox pods, which requires the main openhands-ai package)
  basePython = pythonPackages.python.withPackages (ps: [
    sdkPackages.openhands-sdk
    sdkPackages.openhands-tools
    sdkPackages.openhands-agent-server
    sdkPackages.openhands-workspace
    serverPackages.backend
    ps.boto3
  ]);

  # Minimal system packages — matches upstream base-image-minimal
  baseSystemPackages = with pkgs; [
    coreutils
    bash
    gitMinimal
    gnused
    gnugrep
    findutils
    gawk
    gnutar
    gzip
    xz
    which
    tmux
    procps
    util-linux
    curl
    wget
    jq
    openssh
    # Fonts + fontconfig — required for Chromium/browser-use to render pages
    # without thousands of HarfBuzz errors that crash the CDP debug interface
    fontconfig
    freefont_ttf
    dejavu_fonts
  ];

  # Additional packages for the full variant
  fullPackages = with pkgs; [
    openvscode-server
    gh
    docker-client
  ];

  # Fontconfig configuration pointing to Nix font paths
  fontsConf = pkgs.makeFontsConf {
    fontDirectories = [ pkgs.freefont_ttf pkgs.dejavu_fonts ];
  };

  entrypoint = pkgs.writeShellApplication {
    name = "openhands-entrypoint";
    runtimeInputs = baseSystemPackages ++ [ pkgs.nix basePython ];
    text = builtins.readFile ./entrypoint.sh;
  };

  mkLazyPackage = import ./lazy-package.nix { inherit pkgs; };

  lazyChromium = mkLazyPackage {
    name = "chromium";
    flakeRef = "nixpkgs#ungoogled-chromium";
    binaries = [ "chromium" ];
  };
  lazyVscode = pkgs.writeShellScriptBin "openvscode-server" (builtins.readFile ./lazy-vscode.sh);
  gitCredentialBroker = pkgs.writeShellScriptBin "git-credential-broker" (builtins.readFile ./git-credential-broker.sh);

  # Shims for upstream compatibility: the upstream kubernetes_runtime hardcodes
  # /openhands/micromamba/bin/micromamba run -n openhands poetry run <cmd>
  # In our nix image, Python is directly on PATH — these shims just pass through.
  micromambaShim = pkgs.writeShellScriptBin "micromamba" ''
    # Usage: micromamba run -n <env> <cmd...>
    # Skip "run -n <env>" and exec the rest
    if [ "$1" = "run" ] && [ "$2" = "-n" ]; then
      shift 3  # skip: run -n <envname>
    fi
    exec "$@"
  '';
  poetryShim = pkgs.writeShellScriptBin "poetry" ''
    # Usage: poetry run <cmd...>
    # Skip "run" and exec the rest
    if [ "$1" = "run" ]; then
      shift  # skip: run
    fi
    exec "$@"
  '';

  # Shared image builder used by both variants
  mkImage = {
    name ? "openhands-agent-server",
    tag ? "latest",
    extraPackages ? [],
    extraPythonPackages ? [],
    extraSkillsDirs ? [],
    port ? 8000,
    variant ? "full",
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
      pkgs.nix
      pkgs.cacert
      entrypoint
      lazyChromium
      micromambaShim
      poetryShim
      gitCredentialBroker
    ] ++ lib.optionals (variant != "full") [ lazyVscode ]
      ++ extraPackages;

    # Root filesystem overlay
    rootfs = pkgs.runCommand "openhands-agent-rootfs" {} ''
      mkdir -p $out/workspace/project $out/tmp $out/root $out/etc $out/openhands/code $out/openhands/micromamba/bin
      chmod 1777 $out/tmp

      # Upstream kubernetes_runtime hardcodes /openhands/micromamba/bin/micromamba
      ln -s ${micromambaShim}/bin/micromamba $out/openhands/micromamba/bin/micromamba

      # Nix config (nix database/store dirs are handled by initializeNixDatabase)
      mkdir -p $out/root/.config/nix

      # Enable flakes for dynamic installs
      cat > $out/root/.config/nix/nix.conf <<NIXCONF
      experimental-features = nix-command flakes
      sandbox = false
      filter-syscalls = false
      NIXCONF

      # /etc files with nixbld users for Nix builds
      echo "root:x:0:0:root:/root:/bin/bash" > $out/etc/passwd
      for i in $(seq 1 10); do
        echo "nixbld$i:x:$((30000 + i)):30000:Nix build user $i:/var/empty:/bin/nologin" >> $out/etc/passwd
      done
      echo "root:x:0:" > $out/etc/group
      echo "nixbld:x:30000:$(seq -s, 1 10 | sed 's/[0-9]*/nixbld&/g')" >> $out/etc/group

      # Symlink chromium into /usr/bin so browser-use can find it
      # (browser-use's _find_installed_browser_path checks /usr/bin/chromium)
      mkdir -p $out/usr/bin
      ln -s ${lazyChromium}/bin/chromium $out/usr/bin/chromium

      # browser-use config: increase page load wait times for React SPAs
      # (default 0.5s is too short for heavy JS bundles; React apps with
      # persistent WebSocket connections can also cause networkidle to hang)
      mkdir -p $out/root/.config/browseruse
      cat > $out/root/.config/browseruse/config.json <<BROWSERCONF
      {
        "browser_profile": {
          "default": {
            "id": "default",
            "default": true,
            "wait_for_network_idle_page_load_time": 5.0,
            "minimum_wait_page_load_time": 2.0,
            "executable_path": "${lazyChromium}/bin/chromium"
          }
        }
      }
      BROWSERCONF

      # Skills for the agent (base + extra skill directories)
      mkdir -p $out/root/.openhands/skills
      ${lib.concatMapStringsSep "\n" (d: "cp ${d}/*.md $out/root/.openhands/skills/") ((lib.toList skillsDir) ++ extraSkillsDirs)}

      ${lib.optionalString (variant == "full") ''
        # OpenVSCode Server: symlink into expected location
        mkdir -p $out/openhands/.openvscode-server
        ln -s ${pkgs.openvscode-server}/lib/openvscode-server/* $out/openhands/.openvscode-server/ 2>/dev/null || true
        mkdir -p $out/usr/local/bin
        ln -s ${pkgs.openvscode-server}/bin/openvscode-server $out/usr/local/bin/code
      ''}
    '';

    # Symlink bin paths into /bin
    rootBinEnv = pkgs.buildEnv {
      name = "agent-root-env";
      paths = allPackages;
      pathsToLink = [ "/bin" "/etc/ssl" "/share" ];
    };

    vscodeEnv = lib.optionals (variant == "full") [
      "EDITOR=code"
      "VISUAL=code"
      "GIT_EDITOR=code --wait"
      "OPENVSCODE_SERVER_ROOT=/openhands/.openvscode-server"
    ];
    # System packages layer — changes rarely, stays cached across pushes.
    systemLayer = n2c.buildLayer {
      deps = baseSystemPackages ++ variantPackages ++ [ pkgs.nix pkgs.cacert ];
    };

  in
  n2c.buildImage {
    inherit name tag;
    initializeNixDatabase = true;
    maxLayers = 80;

    layers = [ systemLayer ];
    copyToRoot = [ rootfs rootBinEnv ];

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
        "TERM=xterm-256color"
        "TERMINFO_DIRS=${pkgs.ncurses}/share/terminfo"
        "SU_TO_USER=false"
        "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "FONTCONFIG_FILE=${fontsConf}"
      ] ++ vscodeEnv;
    };
  };

in
{
  mkAgentServerImage = args: mkImage ({ variant = "full"; } // args);
  mkAgentServerImageMinimal = args: mkImage ({ variant = "minimal"; } // args);
}
