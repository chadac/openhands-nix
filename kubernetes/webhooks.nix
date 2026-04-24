# OpenHands webhooks kubenix module
#
# Deploys the openhands-webhooks service that receives GitLab/GitHub/Slack/Jira
# webhooks and triggers OpenHands conversations.
#
{ config, lib, kubenix, ... }:

let
  cfg = config.openhands.webhooks;
  namespace = config.openhands.namespace;
  name = "openhands-webhooks";
in
{
  imports = [ kubenix.modules.k8s ];

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
    kubernetes.resources = {
      # --- PVC (optional) ---
      persistentVolumeClaims = lib.mkIf cfg.persistence.enable {
        "${name}-data" = {
          metadata.namespace = namespace;
          spec = {
            accessModes = [ "ReadWriteOnce" ];
            resources.requests.storage = cfg.persistence.size;
          } // lib.optionalAttrs (cfg.persistence.storageClass != "") {
            storageClassName = cfg.persistence.storageClass;
          };
        };
      };

      # --- Deployment ---
      deployments.${name} = {
        metadata = {
          inherit namespace;
          labels.app = name;
        };
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
              containers = [{
                inherit name;
                image = "${cfg.image}:${cfg.imageTag}";
                imagePullPolicy = cfg.imagePullPolicy;
                ports = [{
                  name = "http";
                  containerPort = 8080;
                  protocol = "TCP";
                }];
                env = [
                  { name = "OPENHANDS_API_URL"; value = cfg.openhandsUrl; }
                  { name = "OPENHANDS_URL"; value = "https://${config.openhands.server.domain}"; }
                  { name = "MENTION_PATTERN"; value = cfg.mentionPattern; }
                  { name = "DEFAULT_GITLAB_REPO"; value = cfg.defaultGitlabRepo; }
                  { name = "IGNORE_USERNAMES"; value = cfg.ignoreUsernames; }
                  { name = "GITLAB_ENABLED"; value = cfg.integrations.gitlab; }
                  { name = "SLACK_ENABLED"; value = cfg.integrations.slack; }
                  { name = "JIRA_ENABLED"; value = cfg.integrations.jira; }
                  { name = "CONVERSATION_IDLE_TIMEOUT_MINUTES"; value = cfg.conversationIdleTimeoutMinutes; }
                  { name = "SANDBOX_K8S_NAMESPACE"; value = namespace; }
                  {
                    name = "GITLAB_WEBHOOK_SECRET";
                    valueFrom.secretKeyRef = { name = cfg.secretName; key = "gitlab-webhook-secret"; optional = true; };
                  }
                  {
                    name = "GITLAB_TOKEN";
                    valueFrom.secretKeyRef = { name = cfg.secretName; key = "gitlab-token"; optional = true; };
                  }
                  {
                    name = "SLACK_SIGNING_SECRET";
                    valueFrom.secretKeyRef = { name = cfg.secretName; key = "slack-signing-secret"; optional = true; };
                  }
                  {
                    name = "SLACK_BOT_TOKEN";
                    valueFrom.secretKeyRef = { name = cfg.secretName; key = "slack-bot-token"; optional = true; };
                  }
                  {
                    name = "RELAY_API_KEY";
                    valueFrom.secretKeyRef = { name = cfg.secretName; key = "relay-api-key"; };
                  }
                ] ++ lib.optional cfg.persistence.enable
                  { name = "WEBHOOKS_DB_PATH"; value = "/data/webhooks.db"; };
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
                volumeMounts = lib.optional cfg.persistence.enable
                  { name = "data"; mountPath = "/data"; };
              }];
              volumes = lib.optional cfg.persistence.enable
                { name = "data"; persistentVolumeClaim.claimName = "${name}-data"; };
            };
          };
        };
      };

      # --- Service ---
      services.${name} = {
        metadata = {
          inherit namespace;
          labels.app = name;
        };
        spec = {
          selector.app = name;
          ports = [{
            name = "http";
            port = 8080;
            targetPort = "http";
            protocol = "TCP";
          }];
        };
      };

      # --- Ingress (ALB) ---
      ingresses.${name} = {
        metadata = {
          inherit namespace;
          annotations = {
            "kubernetes.io/ingress.class" = "alb";
            "alb.ingress.kubernetes.io/scheme" = "internet-facing";
            "alb.ingress.kubernetes.io/target-type" = "ip";
            "alb.ingress.kubernetes.io/listen-ports" = builtins.toJSON [{ HTTPS = 443; }];
            "alb.ingress.kubernetes.io/certificate-arn" = cfg.certArn;
            "alb.ingress.kubernetes.io/ssl-redirect" = "443";
            "alb.ingress.kubernetes.io/healthcheck-path" = "/health";
            "alb.ingress.kubernetes.io/group.name" = "openhands";
          };
        };
        spec = {
          ingressClassName = "alb";
          rules = [{
            host = cfg.domain;
            http.paths = [{
              path = "/";
              pathType = "Prefix";
              backend.service = {
                name = name;
                port.number = 8080;
              };
            }];
          }];
        };
      };
    };
  };
}
