{
  description = "Nix packages for OpenHands AI agent platform";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    nix2container = {
      url = "github:nlewo/nix2container";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = inputs@{ self, nixpkgs, flake-parts, nix2container }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];

      perSystem = { pkgs, system, lib, ... }:
        let
          python = pkgs.python312;

          n2c = nix2container.packages.${system}.nix2container;

          # Apply our Python overlay for missing/bumped deps
          pythonPackages = python.pkgs.overrideScope (
            import ./overlays/python.nix { inherit pkgs; }
          );

          # browser-use and its deps (uuid7, bubus, cdp-use)
          browserDeps = import ./pkgs/sdk/browser-deps.nix {
            inherit lib pythonPackages;
            inherit (pkgs) fetchurl;
          };

          # SDK packages (openhands-sdk, tools, agent-server, workspace)
          sdkPackages = pkgs.callPackage ./pkgs/sdk {
            inherit pythonPackages browserDeps;
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

          # Shared library for OpenHands services
          openhands-common = pkgs.callPackage ./services/common {
            inherit pythonPackages;
          };

          # OpenHands microservices
          openhands-webhooks = pkgs.callPackage ./services/webhooks {
            inherit pythonPackages openhands-common;
          };

          openhands-lifecycle = pkgs.callPackage ./services/lifecycle {
            inherit pythonPackages openhands-common;
          };

          openhands-broker = pkgs.callPackage ./services/broker {
            inherit pythonPackages openhands-common;
          };

          # Nix-specific agent skills
          skillsDir = ./skills;

          # Agent-server container image builder
          agentServerImages = import ./pkgs/images/agent-server.nix {
            inherit pkgs lib pythonPackages sdkPackages serverPackages skillsDir n2c;
          };

          # Full server image (UI + API)
          serverImages = import ./pkgs/images/server.nix {
            inherit pkgs lib pythonPackages sdkPackages serverPackages skillsDir n2c;
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
            # Default is minimal (no openvscode-server, gh, docker-client).
            # Use agent-server-image-full for the full variant.
            agent-server-image = agentServerImages.mkAgentServerImageMinimal { };
            agent-server-image-full = agentServerImages.mkAgentServerImage {
              name = "openhands-agent-server-full";
            };

            # OpenHands server (web UI + API)
            openhands-server = serverImages.entrypoint;
            openhands-frontend = serverPackages.frontend;

            # Server container image: nix build .#server-image
            server-image = serverImages.mkServerImage { };

            # OpenHands microservices
            inherit openhands-common openhands-webhooks openhands-lifecycle openhands-broker;
          };

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
            inherit openhands-common openhands-webhooks openhands-lifecycle openhands-broker;
          } // sdkTests;
        };

      flake = {
        # kubenix modules for deploying OpenHands on Kubernetes
        kubenixModules.openhands = ./kubernetes;
      };
    };
}
