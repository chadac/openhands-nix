# OpenHands lifecycle manager easykubenix module
#
# Background service that cleans up idle conversations, orphaned sandbox
# resources, and old workspace PVCs.
#
{ config, lib, ... }:

let
  cfg = config.openhands.lifecycle;
  namespace = config.openhands.namespace;
  name = "openhands-lifecycle";
in
{
  options.openhands.lifecycle = with lib; {
    enable = mkEnableOption "OpenHands lifecycle manager";

    image = mkOption {
      type = types.str;
      description = "Lifecycle manager container image";
    };

    imageTag = mkOption {
      type = types.str;
      default = "latest";
    };

    imagePullPolicy = mkOption {
      type = types.str;
      default = "Always";
    };

    openhandsApiUrl = mkOption {
      type = types.str;
      default = "http://openhands.${namespace}.svc.cluster.local:3000";
      description = "Cluster-internal URL of the OpenHands server";
    };

    idleTimeoutMinutes = mkOption {
      type = types.int;
      default = 60;
    };

    cleanupIntervalSeconds = mkOption {
      type = types.int;
      default = 120;
    };

    pvcMaxAgeDays = mkOption {
      type = types.int;
      default = 30;
    };

    sandboxNamespace = mkOption {
      type = types.str;
      default = namespace;
    };

    databaseUrl = mkOption {
      type = types.str;
      default = "";
      description = "PostgreSQL DSN. If empty, uses DATABASE_URL from secret.";
    };

    secretName = mkOption {
      type = types.str;
      default = "openhands-services-secrets";
      description = "K8s Secret with DATABASE_URL and other credentials";
    };

    serviceAccountName = mkOption {
      type = types.str;
      default = "openhands-lifecycle";
    };

    resources = {
      requests = {
        cpu = mkOption { type = types.str; default = "50m"; };
        memory = mkOption { type = types.str; default = "64Mi"; };
      };
      limits = {
        memory = mkOption { type = types.str; default = "128Mi"; };
      };
    };
  };

  config = lib.mkIf cfg.enable {
    kubernetes.resources.${namespace} = {
      # --- ServiceAccount ---
      ServiceAccount.${cfg.serviceAccountName} = {};

      # --- Role (manage sandbox resources) ---
      Role."${name}-role" = {
        rules = [
          {
            apiGroups = [ "" ];
            resources = [ "pods" "services" "persistentvolumeclaims" ];
            verbs = [ "get" "list" "delete" ];
          }
          {
            apiGroups = [ "batch" ];
            resources = [ "jobs" ];
            verbs = [ "get" "list" "delete" ];
          }
          {
            apiGroups = [ "networking.k8s.io" ];
            resources = [ "ingresses" ];
            verbs = [ "get" "list" "delete" ];
          }
        ];
      };

      RoleBinding."${name}-rolebinding" = {
        roleRef = {
          apiGroup = "rbac.authorization.k8s.io";
          kind = "Role";
          name = "${name}-role";
        };
        subjects = [
          {
            kind = "ServiceAccount";
            name = cfg.serviceAccountName;
            namespace = namespace;
          }
        ];
      };

      # --- Deployment ---
      Deployment.${name} = {
        metadata.labels.app = name;
        spec = {
          replicas = 1;
          strategy.type = "Recreate";
          selector.matchLabels.app = name;
          template = {
            metadata.labels.app = name;
            spec = {
              serviceAccountName = cfg.serviceAccountName;
              containers = lib.mkNamedList {
                ${name} = {
                  image = "${cfg.image}:${cfg.imageTag}";
                  imagePullPolicy = cfg.imagePullPolicy;
                  ports = lib.mkNamedList {
                    http = {
                      containerPort = 8080;
                      protocol = "TCP";
                    };
                  };
                  env = lib.mkNamedList ({
                    OPENHANDS_API_URL.value = cfg.openhandsApiUrl;
                    IDLE_TIMEOUT_MINUTES.value = toString cfg.idleTimeoutMinutes;
                    CLEANUP_INTERVAL_SECONDS.value = toString cfg.cleanupIntervalSeconds;
                    PVC_MAX_AGE_DAYS.value = toString cfg.pvcMaxAgeDays;
                    SANDBOX_K8S_NAMESPACE.value = cfg.sandboxNamespace;
                    DATABASE_URL.valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "database-url";
                    };
                  } // lib.optionalAttrs (cfg.databaseUrl != "") {
                    DATABASE_URL.value = cfg.databaseUrl;
                  });
                  livenessProbe = {
                    httpGet = { path = "/health"; port = "http"; };
                    periodSeconds = 30;
                  };
                  resources = {
                    requests = {
                      cpu = cfg.resources.requests.cpu;
                      memory = cfg.resources.requests.memory;
                    };
                    limits.memory = cfg.resources.limits.memory;
                  };
                };
              };
            };
          };
        };
      };

      # --- Service (health check only, no ingress) ---
      Service.${name} = {
        metadata.labels.app = name;
        spec = {
          selector.app = name;
          ports = lib.mkNamedList {
            http = {
              port = 8080;
              targetPort = "http";
              protocol = "TCP";
            };
          };
        };
      };
    };
  };
}
