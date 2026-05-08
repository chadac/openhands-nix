# Per-sandbox K8s resource modules.
#
# Evaluated at runtime via kubenix when a new sandbox is created.
# Each sub-module declares options under `sandbox.*` and renders
# its K8s resources from those options.
{ ... }:

{
  imports = [
    ./options.nix
    ./service-account.nix
    ./pvc.nix
    ./job.nix
    ./service.nix
    ./ingress.nix
  ];
}
