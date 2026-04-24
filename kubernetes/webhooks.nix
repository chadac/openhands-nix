# OpenHands webhooks easykubenix module
#
# Deploys the openhands-webhooks service that receives GitLab/GitHub/Slack/Jira
# webhooks and triggers OpenHands conversations.
#
{ config, lib, ... }:

let
  cfg = config.openhands.webhooks;
  namespace = config.openhands.namespace;
  name = "openhands-webhooks";
in
{
  options.openhands.webhooks = with lib; {
    enable = mkEnableOption "OpenHands webhooks service";

    image = mkOption {
      type = types.str;
      description = "Webhooks container image";
    };

    imageTag = mkOption {
      type = types.str;
      default = "latest";
    };

    imagePullPolicy = mkOption {
      type = types.str;
      default = "Always";
    };

    domain = mkOption {
      type = types.str;
      description = "Domain for the webhooks endpoint";
    };

    certArn = mkOption {
      type = types.str;
      description = "ACM certificate ARN for the ALB";
    };

    openhandsUrl = mkOption {
      type = types.str;
      default = "http://openhands.${namespace}.svc.cluster.local:3000";
      description = "Cluster-internal URL of the OpenHands server";
    };

    mentionPattern = mkOption {
      type = types.str;
      default = "@openhands";
    };

    defaultGitlabRepo = mkOption {
      type = types.str;
      default = "";
    };

    ignoreUsernames = mkOption {
      type = types.str;
      default = "openhands-bot";
    };

    integrations = {
      gitlab = mkOption { type = types.str; default = "true"; };
      slack = mkOption { type = types.str; default = "false"; };
      jira = mkOption { type = types.str; default = "false"; };
    };

    conversationIdleTimeoutMinutes = mkOption {
      type = types.str;
      default = "60";
    };

    secretName = mkOption {
      type = types.str;
      default = "openhands-webhooks-secrets";
      description = "Name of the K8s Secret containing webhook tokens (created by ExternalSecrets)";
    };

    persistence = {
      enable = mkEnableOption "persistent storage for webhooks SQLite DB";
      storageClass = mkOption {
        type = types.str;
        default = "";
      };
      size = mkOption {
        type = types.str;
        default = "1Gi";
      };
    };

    resources = {
      requests = {
        cpu = mkOption { type = types.str; default = "100m"; };
        memory = mkOption { type = types.str; default = "128Mi"; };
      };
      limits = {
        memory = mkOption { type = types.str; default = "256Mi"; };
      };
    };
  };

  config = lib.mkIf cfg.enable {
    kubernetes.resources.${namespace} = {
      # --- PVC (optional) ---
      PersistentVolumeClaim = lib.mkIf cfg.persistence.enable {
        "${name}-data" = {
          spec = {
            accessModes = [ "ReadWriteOnce" ];
            resources.requests.storage = cfg.persistence.size;
          } // lib.optionalAttrs (cfg.persistence.storageClass != "") {
            storageClassName = cfg.persistence.storageClass;
          };
        };
      };

      # --- Deployment ---
      Deployment.${name} = {
        metadata.labels.app = name;
        spec = {
          replicas = 1;
          strategy.type = "Recreate";
          selector.matchLabels.app = name;
          template = {
            metadata = {
              labels.app = name;
              annotations."karpenter.sh/do-not-disrupt" = "true";
            };
            spec = {
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
                    OPENHANDS_API_URL.value = cfg.openhandsUrl;
                    OPENHANDS_URL.value = "https://${config.openhands.server.domain}";
                    MENTION_PATTERN.value = cfg.mentionPattern;
                    DEFAULT_GITLAB_REPO.value = cfg.defaultGitlabRepo;
                    IGNORE_USERNAMES.value = cfg.ignoreUsernames;
                    GITLAB_ENABLED.value = cfg.integrations.gitlab;
                    SLACK_ENABLED.value = cfg.integrations.slack;
                    JIRA_ENABLED.value = cfg.integrations.jira;
                    CONVERSATION_IDLE_TIMEOUT_MINUTES.value = cfg.conversationIdleTimeoutMinutes;
                    SANDBOX_K8S_NAMESPACE.value = namespace;
                    GITLAB_WEBHOOK_SECRET.valueFrom.secretKeyRef = { name = cfg.secretName; key = "gitlab-webhook-secret"; optional = true; };
                    GITLAB_TOKEN.valueFrom.secretKeyRef = { name = cfg.secretName; key = "gitlab-token"; optional = true; };
                    SLACK_SIGNING_SECRET.valueFrom.secretKeyRef = { name = cfg.secretName; key = "slack-signing-secret"; optional = true; };
                    SLACK_BOT_TOKEN.valueFrom.secretKeyRef = { name = cfg.secretName; key = "slack-bot-token"; optional = true; };
                    RELAY_API_KEY.valueFrom.secretKeyRef = { name = cfg.secretName; key = "relay-api-key"; };
                  } // lib.optionalAttrs cfg.persistence.enable {
                    WEBHOOKS_DB_PATH.value = "/data/webhooks.db";
                  });
                  livenessProbe = {
                    httpGet = { path = "/health"; port = "http"; };
                    periodSeconds = 20;
                  };
                  readinessProbe = {
                    httpGet = { path = "/health"; port = "http"; };
                    periodSeconds = 10;
                  };
                  resources = {
                    requests = {
                      cpu = cfg.resources.requests.cpu;
                      memory = cfg.resources.requests.memory;
                    };
                    limits.memory = cfg.resources.limits.memory;
                  };
                  volumeMounts = lib.mkNamedList (
                    lib.optionalAttrs cfg.persistence.enable {
                      data.mountPath = "/data";
                    }
                  );
                };
              };
              volumes = lib.mkNamedList (
                lib.optionalAttrs cfg.persistence.enable {
                  data.persistentVolumeClaim.claimName = "${name}-data";
                }
              );
            };
          };
        };
      };

      # --- Service ---
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

      # --- Ingress (ALB) ---
      Ingress.${name} = {
        metadata.annotations = {
          "kubernetes.io/ingress.class" = "alb";
          "alb.ingress.kubernetes.io/scheme" = "internet-facing";
          "alb.ingress.kubernetes.io/target-type" = "ip";
          "alb.ingress.kubernetes.io/listen-ports" = builtins.toJSON [{ HTTPS = 443; }];
          "alb.ingress.kubernetes.io/certificate-arn" = cfg.certArn;
          "alb.ingress.kubernetes.io/ssl-redirect" = "443";
          "alb.ingress.kubernetes.io/healthcheck-path" = "/health";
          "alb.ingress.kubernetes.io/group.name" = "openhands";
        };
        spec = {
          ingressClassName = "alb";
          rules = [
            {
              host = cfg.domain;
              http.paths = [
                {
                  path = "/";
                  pathType = "Prefix";
                  backend.service = {
                    name = name;
                    port.number = 8080;
                  };
                }
              ];
            }
          ];
        };
      };
    };
  };
}
