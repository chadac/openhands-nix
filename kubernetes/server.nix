# OpenHands server kubenix module
#
# Deploys the OpenHands server (UI + API) as a Kubernetes Deployment with:
#   - ServiceAccount with IRSA annotation for Bedrock access
#   - RBAC for managing sandbox Jobs/Services/PVCs/Ingresses
#   - ConfigMap for default settings
#   - Service (ClusterIP)
#   - Ingress (ALB with optional Cognito OIDC auth)
#
{ config, lib, kubenix, ... }:

let
  cfg = config.openhands.server;
  namespace = config.openhands.namespace;
  name = "openhands";
in
{
  imports = [ kubenix.modules.k8s ];

  options.openhands.server = with lib; {
    enable = mkEnableOption "OpenHands server";

    image = mkOption {
      type = types.str;
      description = "Server container image";
    };

    imageTag = mkOption {
      type = types.str;
      default = "latest";
    };

    imagePullPolicy = mkOption {
      type = types.str;
      default = "Always";
    };

    replicas = mkOption {
      type = types.int;
      default = 1;
    };

    irsaRoleArn = mkOption {
      type = types.str;
      description = "IAM role ARN for IRSA (Bedrock access)";
    };

    llm = {
      model = mkOption {
        type = types.str;
        default = "bedrock/us.anthropic.claude-opus-4-6-v1";
      };
      awsRegion = mkOption {
        type = types.str;
        default = "us-east-1";
      };
    };

    domain = mkOption {
      type = types.str;
      description = "Domain for the OpenHands UI (e.g. openhands.dev.aeonai.com)";
    };

    certArn = mkOption {
      type = types.str;
      description = "ACM certificate ARN for the ALB";
    };

    cognito = {
      enable = mkEnableOption "Cognito OIDC auth on ALB";
      userPoolArn = mkOption {
        type = types.str;
        default = "";
      };
      userPoolClientId = mkOption {
        type = types.str;
        default = "";
      };
      userPoolDomain = mkOption {
        type = types.str;
        default = "";
      };
    };

    sandbox = {
      image = mkOption {
        type = types.str;
        description = "Agent-server image for sandbox pods";
      };
      imageTag = mkOption {
        type = types.str;
        default = "latest";
      };
      nixPackages = mkOption {
        type = types.str;
        default = "nixpkgs#awscli2 nixpkgs#kubectl nixpkgs#python312 nixpkgs#nodejs_22 nixpkgs#jq nixpkgs#ripgrep nixpkgs#gh";
        description = "Nix packages to install in sandbox pods at startup";
      };
      resourceRequests = mkOption {
        type = types.str;
        default = ''{"cpu": "4", "memory": "16Gi"}'';
      };
      resourceLimits = mkOption {
        type = types.str;
        default = ''{"cpu": "4", "memory": "16Gi"}'';
      };
      startupTimeout = mkOption {
        type = types.int;
        default = 600;
      };
    };

    defaultSettings = {
      agent = mkOption {
        type = types.str;
        default = "CodeActAgent";
      };
      maxIterations = mkOption {
        type = types.int;
        default = 100;
      };
    };

    resources = {
      requests = {
        cpu = mkOption { type = types.str; default = "500m"; };
        memory = mkOption { type = types.str; default = "1Gi"; };
      };
      limits = {
        memory = mkOption { type = types.str; default = "4Gi"; };
      };
    };

    secretName = mkOption {
      type = types.str;
      default = "openhands-secrets";
      description = "Name of the K8s Secret containing tokens (created by ExternalSecrets)";
    };

    sandboxEnvSecretName = mkOption {
      type = types.str;
      default = "openhands-sandbox-env";
      description = "Name of the K8s Secret mounted on sandbox pods";
    };
  };

  config = lib.mkIf cfg.enable {
    kubernetes.resources = {
      # --- Namespace ---
      namespaces.${namespace} = {};

      # --- ServiceAccount ---
      serviceAccounts.${name} = {
        metadata = {
          inherit namespace;
          annotations = {
            "eks.amazonaws.com/role-arn" = cfg.irsaRoleArn;
          };
        };
      };

      # --- RBAC: sandbox manager ---
      roles.openhands-sandbox-manager = {
        metadata.namespace = namespace;
        rules = [
          {
            apiGroups = [ "batch" ];
            resources = [ "jobs" ];
            verbs = [ "create" "get" "list" "watch" "delete" "update" "patch" ];
          }
          {
            apiGroups = [ "" ];
            resources = [ "services" ];
            verbs = [ "create" "get" "list" "watch" "delete" ];
          }
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [ "get" "list" "watch" ];
          }
          {
            apiGroups = [ "" ];
            resources = [ "pods/log" ];
            verbs = [ "get" ];
          }
          {
            apiGroups = [ "" ];
            resources = [ "persistentvolumeclaims" ];
            verbs = [ "create" "get" "list" "watch" "delete" ];
          }
          {
            apiGroups = [ "networking.k8s.io" ];
            resources = [ "ingresses" ];
            verbs = [ "create" "get" "list" "watch" "delete" ];
          }
        ];
      };

      roleBindings.openhands-sandbox-manager = {
        metadata.namespace = namespace;
        roleRef = {
          apiGroup = "rbac.authorization.k8s.io";
          kind = "Role";
          name = "openhands-sandbox-manager";
        };
        subjects = [{
          kind = "ServiceAccount";
          inherit name namespace;
        }];
      };

      # --- ConfigMap: default settings ---
      configMaps.openhands-default-settings = {
        metadata.namespace = namespace;
        data."settings.json" = builtins.toJSON {
          language = "en";
          agent = cfg.defaultSettings.agent;
          max_iterations = cfg.defaultSettings.maxIterations;
          security_analyzer = null;
          confirmation_mode = false;
          llm_model = cfg.llm.model;
          llm_api_key = "unused";
          llm_base_url = null;
          remote_runtime_resource_factor = 1;
          enable_default_condenser = true;
          enable_sound_notifications = false;
          enable_proactive_conversation_starters = true;
          user_consents_to_analytics = false;
        };
      };

      # --- Deployment ---
      deployments.${name} = {
        metadata = {
          inherit namespace;
          labels.app = name;
        };
        spec = {
          replicas = cfg.replicas;
          strategy.type = "Recreate";
          selector.matchLabels.app = name;
          template = {
            metadata = {
              labels.app = name;
              annotations."karpenter.sh/do-not-disrupt" = "true";
            };
            spec = {
              serviceAccountName = name;
              initContainers = [{
                name = "seed-settings";
                image = "busybox:1.36";
                command = [ "sh" "-c" ];
                args = [
                  ''
                    mkdir -p /home
                    if [ ! -f /home/settings.json ]; then
                      cp /defaults/settings.json /home/settings.json
                      echo "Seeded default settings.json"
                    fi
                  ''
                ];
                volumeMounts = [
                  { name = "openhands-home"; mountPath = "/home"; }
                  { name = "default-settings"; mountPath = "/defaults"; }
                ];
              }];
              containers = [{
                inherit name;
                image = "${cfg.image}:${cfg.imageTag}";
                imagePullPolicy = cfg.imagePullPolicy;
                ports = [{
                  name = "http";
                  containerPort = 3000;
                  protocol = "TCP";
                }];
                env = [
                  { name = "RUNTIME"; value = "kubernetes"; }
                  { name = "SANDBOX_K8S_NAMESPACE"; value = namespace; }
                  { name = "SANDBOX_K8S_IMAGE"; value = "${cfg.sandbox.image}:${cfg.sandbox.imageTag}"; }
                  { name = "SANDBOX_K8S_IMAGE_PULL_POLICY"; value = "Always"; }
                  { name = "SANDBOX_K8S_RESOURCE_REQUESTS"; value = cfg.sandbox.resourceRequests; }
                  { name = "SANDBOX_K8S_RESOURCE_LIMITS"; value = cfg.sandbox.resourceLimits; }
                  { name = "SANDBOX_NIX_PACKAGES"; value = cfg.sandbox.nixPackages; }
                  { name = "SANDBOX_K8S_ENV_SECRET"; value = cfg.sandboxEnvSecretName; }
                  { name = "OH_APP_CONVERSATION_SANDBOX_STARTUP_TIMEOUT"; value = toString cfg.sandbox.startupTimeout; }
                  { name = "LLM_MODEL"; value = cfg.llm.model; }
                  { name = "LLM_API_KEY"; value = "unused"; }
                  { name = "LLM_AWS_REGION_NAME"; value = cfg.llm.awsRegion; }
                  { name = "AWS_DEFAULT_REGION"; value = cfg.llm.awsRegion; }
                  {
                    name = "GITHUB_TOKEN";
                    valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "github-token";
                      optional = true;
                    };
                  }
                ];
                startupProbe = {
                  httpGet = { path = "/"; port = "http"; };
                  failureThreshold = 30;
                  periodSeconds = 5;
                };
                livenessProbe = {
                  httpGet = { path = "/"; port = "http"; };
                  periodSeconds = 20;
                };
                readinessProbe = {
                  httpGet = { path = "/"; port = "http"; };
                  periodSeconds = 10;
                };
                resources = {
                  requests = {
                    cpu = cfg.resources.requests.cpu;
                    memory = cfg.resources.requests.memory;
                  };
                  limits = {
                    memory = cfg.resources.limits.memory;
                  };
                };
                volumeMounts = [
                  { name = "workspace"; mountPath = "/opt/workspace_base"; }
                  { name = "openhands-home"; mountPath = "/root/.openhands"; }
                ];
              }];
              volumes = [
                { name = "workspace"; emptyDir = {}; }
                { name = "openhands-home"; emptyDir = {}; }
                {
                  name = "default-settings";
                  configMap.name = "openhands-default-settings";
                }
              ];
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
            port = 3000;
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
            "alb.ingress.kubernetes.io/healthcheck-path" = "/";
            "alb.ingress.kubernetes.io/group.name" = "openhands";
          } // lib.optionalAttrs cfg.cognito.enable {
            "alb.ingress.kubernetes.io/auth-type" = "cognito";
            "alb.ingress.kubernetes.io/auth-idp-cognito" = builtins.toJSON {
              userPoolARN = cfg.cognito.userPoolArn;
              userPoolClientID = cfg.cognito.userPoolClientId;
              userPoolDomain = cfg.cognito.userPoolDomain;
            };
            "alb.ingress.kubernetes.io/auth-on-unauthenticated-request" = "authenticate";
            "alb.ingress.kubernetes.io/auth-scope" = "openid email profile";
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
                port.number = 3000;
              };
            }];
          }];
        };
      };
    };
  };
}
