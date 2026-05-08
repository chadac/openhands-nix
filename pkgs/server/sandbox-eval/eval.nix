# Runtime evaluator: renders sandbox kubenix modules into K8s manifests.
#
# Called from Python via:
#   nix eval --json --impure --expr \
#     '(import /path/to/eval.nix { kubenixFlake = "github:hall/kubenix/..."; inputFile = "/tmp/input.json"; })'
#
# Arguments:
#   kubenixFlake     — flake reference string for kubenix
#   inputFile        — path to JSON file with sandbox config
#   extraModules     — list of paths to extra kubenix modules (optional)
#
# The input JSON must contain at minimum:
#   { id, namespace, image, sessionApiKey }
# and may contain any other sandbox.* option overrides.
#
{ kubenixFlake
, inputFile
, extraModules ? []
}:

let
  kubenix = builtins.getFlake kubenixFlake;
  input = builtins.fromJSON (builtins.readFile inputFile);

  result = kubenix.evalModules.${builtins.currentSystem} {
    modules = [
      kubenix.nixosModules.kubenix.k8s
      ./default.nix
      { config.sandbox = input; }
    ] ++ extraModules;
  };
in
result.config.kubernetes.objects
