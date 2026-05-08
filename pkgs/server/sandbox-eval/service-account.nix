# Per-sandbox ServiceAccount with optional IRSA annotation.
{ config, lib, ... }:

let
  cfg = config.sandbox;
in
{
  config.kubernetes.resources.serviceAccounts."sandbox-${cfg.id}" = {
    metadata = {
      namespace = cfg.namespace;
      labels = cfg.labels;
    } // lib.optionalAttrs (cfg.irsaRoleArn != "") {
      annotations."eks.amazonaws.com/role-arn" = cfg.irsaRoleArn;
    };
  };
}
