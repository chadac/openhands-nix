# OpenHands easykubenix top-level module
#
# Composes the server, webhooks, and external-secrets modules
# into a single deployable unit. Provides shared options (namespace, etc.).
#
# nix-csi is imported directly from the nix-csi repo's native kubenix modules
# (added in flake.nix via mkOpenhandsManifests).
#
{ config, lib, ... }:

{
  imports = [
    ./server.nix
    ./webhooks.nix
    ./lifecycle.nix
    ./broker.nix
    ./external-secrets.nix
  ];

  options.openhands = with lib; {
    namespace = mkOption {
      type = types.str;
      default = "openhands";
      description = "Kubernetes namespace for all OpenHands resources";
    };
  };

  # Create the openhands namespace
  config.kubernetes.resources.none.Namespace.${config.openhands.namespace} = {};
}
