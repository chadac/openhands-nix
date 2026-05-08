# Per-sandbox Job running the agent-server container.
{ config, lib, ... }:

let
  cfg = config.sandbox;

  # Map attrs to K8s list format, injecting the key as `name`
  envList = lib.mapAttrsToList (name: value: { inherit name value; }) cfg.env;
  volumeList = lib.mapAttrsToList (name: spec: { inherit name; } // spec) cfg.volumes;
  volumeMountList = lib.mapAttrsToList (name: spec: { inherit name; } // spec) cfg.volumeMounts;
  initContainerList = lib.mapAttrsToList (name: spec: { inherit name; } // spec) cfg.initContainers;
in
{
  config.kubernetes.resources.jobs."oh-sandbox-${cfg.id}" = {
    metadata = {
      namespace = cfg.namespace;
      labels = cfg.labels // {
        "openhands.ai/session-key-hash" = cfg.sessionApiKey;
      };
    };
    spec = {
      backoffLimit = 0;
      ttlSecondsAfterFinished = 300;
      template = {
        metadata = {
          labels = cfg.labels;
          annotations."karpenter.sh/do-not-disrupt" = "true";
        };
        spec = {
          serviceAccountName = "sandbox-${cfg.id}";
          restartPolicy = "Never";
          initContainers = initContainerList;
          containers = [{
            name = "agent-server";
            image = cfg.image;
            imagePullPolicy = cfg.imagePullPolicy;
            ports = [{ containerPort = cfg.port; name = "http"; protocol = "TCP"; }];
            env = envList;
            volumeMounts = volumeMountList;
            resources = {
              requests = cfg.resourceRequests;
            } // lib.optionalAttrs (cfg.resourceLimits != {}) {
              limits = cfg.resourceLimits;
            };
            readinessProbe = {
              httpGet = { path = "/health"; port = cfg.port; };
              initialDelaySeconds = 10;
              periodSeconds = 5;
              timeoutSeconds = 3;
              failureThreshold = 60;
            };
          }];
          volumes = volumeList;
        };
      };
    };
  };
}
