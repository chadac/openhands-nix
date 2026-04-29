# OpenHands broker easykubenix module
#
# Credential-injecting transparent proxy that sandboxes use to access
# external APIs (GitHub, GitLab, Jira, Slack) without holding secrets
# directly. Authenticates sandbox pods via K8s TokenReview.
#
{ config, lib, ... }:

let
  cfg = config.openhands.broker;
  namespace = config.openhands.namespace;
  name = "openhands-broker";
in
{
  options.openhands.broker = with lib; {
    enable = mkEnableOption "OpenHands credential broker";

    image = mkOption {
      type = types.str;
      description = "Broker container image";
    };

    imageTag = mkOption {
      type = types.str;
      default = "latest";
    };

    imagePullPolicy = mkOption {
      type = types.str;
      default = "Always";
    };

    tokenAudience = mkOption {
      type = types.str;
      default = "openhands-broker";
      description = "Expected audience in sandbox projected SA tokens";
    };

    sandboxNamespace = mkOption {
      type = types.str;
      default = namespace;
    };

    # Upstream API URLs (defaults)
    githubUpstream = mkOption {
      type = types.str;
      default = "https://api.github.com";
    };

    gitlabUpstream = mkOption {
      type = types.str;
      default = "https://gitlab.com";
    };

    jiraUpstream = mkOption {
      type = types.str;
      default = "https://api.atlassian.com";
    };

    slackUpstream = mkOption {
      type = types.str;
      default = "https://slack.com";
    };

    secretName = mkOption {
      type = types.str;
      default = "openhands-services-secrets";
      description = "K8s Secret with API tokens/credentials";
    };

    serviceAccountName = mkOption {
      type = types.str;
      default = "openhands-broker";
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

      # --- ClusterRole (TokenReview for auth) ---
      ClusterRole."${name}-tokenreview" = {
        rules = [
          {
            apiGroups = [ "authentication.k8s.io" ];
            resources = [ "tokenreviews" ];
            verbs = [ "create" ];
          }
        ];
      };

      ClusterRoleBinding."${name}-tokenreview" = {
        roleRef = {
          apiGroup = "rbac.authorization.k8s.io";
          kind = "ClusterRole";
          name = "${name}-tokenreview";
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
                  env = lib.mkNamedList {
                    TOKEN_AUDIENCE.value = cfg.tokenAudience;
                    SANDBOX_K8S_NAMESPACE.value = cfg.sandboxNamespace;
                    GITHUB_UPSTREAM_URL.value = cfg.githubUpstream;
                    GITLAB_UPSTREAM_URL.value = cfg.gitlabUpstream;
                    JIRA_UPSTREAM_URL.value = cfg.jiraUpstream;
                    SLACK_UPSTREAM_URL.value = cfg.slackUpstream;
                    GITHUB_TOKEN.valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "github-token";
                      optional = true;
                    };
                    GITLAB_TOKEN.valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "gitlab-token";
                      optional = true;
                    };
                    SLACK_BOT_TOKEN.valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "slack-bot-token";
                      optional = true;
                    };
                    ATLASSIAN_CLIENT_ID.valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "atlassian-client-id";
                      optional = true;
                    };
                    ATLASSIAN_CLIENT_SECRET.valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "atlassian-client-secret";
                      optional = true;
                    };
                    ATLASSIAN_CLOUD_ID.valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "atlassian-cloud-id";
                      optional = true;
                    };
                  };
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
                };
              };
            };
          };
        };
      };

      # --- Service (cluster-internal) ---
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
