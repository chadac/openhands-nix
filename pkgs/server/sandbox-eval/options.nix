# Sandbox option declarations.
#
# All sandbox configuration is expressed as module options.
# Collections (env, volumes, volumeMounts, initContainers) are attrs
# keyed by name so consumers can override individual entries.
# The resource modules map these to K8s list format.
{ config, lib, ... }:

let
  cfg = config.sandbox;
in
{
  options.sandbox = with lib; {
    # Required — set from input JSON
    id = mkOption { type = types.str; };
    namespace = mkOption { type = types.str; };
    image = mkOption { type = types.str; };
    sessionApiKey = mkOption { type = types.str; };

    port = mkOption {
      type = types.int;
      default = 8000;
    };

    labels = mkOption {
      type = types.attrsOf types.str;
      default = {};
    };

    # Env vars: keyed by var name → value string
    env = mkOption {
      type = types.attrsOf types.str;
      default = {};
    };

    # Volumes: keyed by volume name → volume spec (minus the name field)
    volumes = mkOption {
      type = types.attrsOf (types.attrsOf types.anything);
      default = {};
    };

    # Volume mounts: keyed by volume name → mount spec (minus the name field)
    volumeMounts = mkOption {
      type = types.attrsOf (types.attrsOf types.anything);
      default = {};
    };

    # Init containers: keyed by container name → container spec (minus the name field)
    initContainers = mkOption {
      type = types.attrsOf (types.attrsOf types.anything);
      default = {};
    };

    resourceRequests = mkOption {
      type = types.attrsOf types.str;
      default = { cpu = "250m"; memory = "512Mi"; };
    };

    resourceLimits = mkOption {
      type = types.attrsOf types.str;
      default = {};
    };

    imagePullPolicy = mkOption {
      type = types.str;
      default = "IfNotPresent";
    };

    irsaRoleArn = mkOption {
      type = types.str;
      default = "";
    };

    workspaceStorageClass = mkOption {
      type = types.str;
      default = "ebs-gp3";
    };

    workspaceSize = mkOption {
      type = types.str;
      default = "10Gi";
    };

    preserveWorkspace = mkOption {
      type = types.bool;
      default = true;
    };
  };

  config.sandbox = {
    labels = {
      "openhands.ai/managed-by" = lib.mkDefault "openhands-nix-kubernetes";
      "openhands.ai/sandbox-id" = lib.mkDefault cfg.id;
    };

    env = {
      PORT = lib.mkDefault (toString cfg.port);
      HOST = lib.mkDefault "0.0.0.0";
      LOG_JSON = lib.mkDefault "true";
      PYTHONUNBUFFERED = lib.mkDefault "1";
      OH_CONVERSATIONS_PATH = lib.mkDefault "/workspace/conversations";
      OH_BASH_EVENTS_DIR = lib.mkDefault "/workspace/bash_events";
      OH_SESSION_API_KEYS_0 = lib.mkDefault cfg.sessionApiKey;
      OPENHANDS_SANDBOX_ID = lib.mkDefault cfg.id;
    };

    volumes = {
      "workspace-volume" = lib.mkDefault { persistentVolumeClaim.claimName = "oh-workspace-${cfg.id}"; };
      tmp = lib.mkDefault { emptyDir.medium = "Memory"; };
      dshm = lib.mkDefault { emptyDir = { medium = "Memory"; sizeLimit = "512Mi"; }; };
    };

    volumeMounts = {
      "workspace-volume" = lib.mkDefault { mountPath = "/workspace"; };
      tmp = lib.mkDefault { mountPath = "/tmp"; };
      dshm = lib.mkDefault { mountPath = "/dev/shm"; };
    };
  };
}
