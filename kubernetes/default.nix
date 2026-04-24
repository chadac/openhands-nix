# OpenHands kubenix top-level module
#
# Composes the server, webhooks, nix-csi, and external-secrets modules
# into a single deployable unit. Provides shared options (namespace, etc.).
#
{ config, lib, kubenix, ... }:

{
  imports = [
    kubenix.modules.k8s
    ./server.nix
    ./webhooks.nix
    ./nix-csi.nix
    ./external-secrets.nix
  ];

  options.openhands = with lib; {
    namespace = mkOption {
      type = types.str;
      default = "openhands";
      description = "Kubernetes namespace for all OpenHands resources";
    };
  };
}
