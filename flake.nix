{
  description = "Nix packages for OpenHands AI agent platform";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs = inputs@{ self, nixpkgs, flake-parts }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];

      perSystem = { pkgs, system, lib, ... }:
        let
          python = pkgs.python312;

          # Apply our Python overlay for missing/bumped deps
          pythonPackages = python.pkgs.overrideScope (
            import ./overlays/python.nix { inherit pkgs; }
          );

          # SDK packages (openhands-sdk, tools, agent-server, workspace)
          sdkPackages = pkgs.callPackage ./pkgs/sdk {
            inherit pythonPackages;
          };

          # CLI application
          cli = pkgs.callPackage ./pkgs/cli {
            inherit pythonPackages sdkPackages;
          };

          # Nix-powered workspace backends (K8s, CSI, local)
          openhands-nix = pkgs.callPackage ./pkgs/kubernetes-workspace {
            inherit pythonPackages sdkPackages;
          };

          # OpenHands server (backend + frontend)
          serverPackages = pkgs.callPackage ./pkgs/server {
            inherit pythonPackages sdkPackages;
            inherit (pkgs) nodejs;
          };

          # Nix-specific agent skills
          skillsDir = ./skills;

          # Agent-server container image builder
          agentServerImages = import ./pkgs/images/agent-server.nix {
            inherit pkgs lib pythonPackages sdkPackages skillsDir;
          };

          # Full server image (UI + API)
          serverImages = import ./pkgs/images/server.nix {
            inherit pkgs lib pythonPackages sdkPackages serverPackages skillsDir;
          };

          # Segmented test derivations
          sdkTests = import ./pkgs/sdk/tests.nix {
            inherit lib pythonPackages sdkPackages;
            inherit (pkgs) runCommand;
          };
        in
        {
          packages = {
            default = cli;
            inherit cli openhands-nix;
            inherit (sdkPackages)
              openhands-sdk
              openhands-tools
              openhands-agent-server
              openhands-workspace;

            # Container images: nix build .#agent-server-image
            agent-server-image = agentServerImages.mkAgentServerImage { };
            agent-server-image-minimal = agentServerImages.mkAgentServerImageMinimal {
              name = "openhands-agent-server-minimal";
            };

            # OpenHands server (web UI + API)
            openhands-server = serverPackages.backend;
            openhands-frontend = serverPackages.frontend;

            # Server container image: nix build .#server-image
            server-image = serverImages.mkServerImage { };
          };

          # The image builder is exposed via packages.agent-server-image
          # and can be customized by importing pkgs/images/agent-server.nix directly.

          # checks = packages (import checks) + segmented test suite
          checks = {
            inherit cli openhands-nix;
            inherit (sdkPackages)
              openhands-sdk
              openhands-tools
              openhands-agent-server
              openhands-workspace;
            openhands-server = serverPackages.backend;
            openhands-frontend = serverPackages.frontend;
          } // sdkTests;
        };
    };
}
