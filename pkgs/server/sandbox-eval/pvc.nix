# Per-sandbox workspace PersistentVolumeClaim.
{ config, lib, ... }:

let
  cfg = config.sandbox;
in
{
  config.kubernetes.resources.persistentVolumeClaims."oh-workspace-${cfg.id}" = {
    metadata = {
      namespace = cfg.namespace;
      labels = cfg.labels // lib.optionalAttrs cfg.preserveWorkspace {
        "openhands.ai/preserve" = "true";
      };
    };
    spec = {
      accessModes = [ "ReadWriteOnce" ];
      storageClassName = cfg.workspaceStorageClass;
      resources.requests.storage = cfg.workspaceSize;
    };
  };
}
