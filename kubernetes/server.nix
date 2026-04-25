# OpenHands server easykubenix module
#
# Deploys the OpenHands server (UI + API) as a Kubernetes Deployment with:
#   - ServiceAccount with IRSA annotation for Bedrock access
#   - RBAC for managing sandbox Jobs/Services/PVCs/Ingresses
#   - ConfigMap for default settings
#   - Service (ClusterIP)
#   - Ingress (ALB with optional Cognito OIDC auth)
#
{ config, lib, ... }:

let
  cfg = config.openhands.server;
  namespace = config.openhands.namespace;
  name = "openhands";

  useNixCsi = cfg.mode == "nix-csi";

  # Container image + command differ by mode
  containerImage = if useNixCsi then cfg.baseImage else "${cfg.image}:${cfg.imageTag}";
  # In nix-csi mode, the CSI volume is mounted at /nix-csi.  We symlink
  # /nix/store and /nix/var into it so that absolute Nix store paths
  # (shebangs, rpath, etc.) resolve correctly.
  containerCommand = if useNixCsi then [
    "sh" "-c"
    "mkdir -p /nix && ln -sfn /nix-csi/nix/store /nix/store && ln -sfn /nix-csi/nix/var /nix/var && exec /nix/var/result/bin/openhands-server-entrypoint"
  ] else null;

  # Sandbox image env vars (only in image mode)
  sandboxImageEnvs = lib.optionalAttrs (!useNixCsi) {
    SANDBOX_K8S_IMAGE.value = "${cfg.sandbox.image}:${cfg.sandbox.imageTag}";
    SANDBOX_K8S_IMAGE_PULL_POLICY.value = cfg.imagePullPolicy;
  };
in
{
  options.openhands.server = with lib; {
    enable = mkEnableOption "OpenHands server";

    # --- Delivery mode: "image" (container image) or "nix-csi" (CSI volume) ---
    mode = mkOption {
      type = types.enum [ "image" "nix-csi" ];
      default = "image";
      description = ''
        How the server is delivered to the pod:
        - "image": traditional container image (set image/imageTag)
        - "nix-csi": nix-csi CSI driver mounts Nix closure (set flakeRef)
      '';
    };

    # --- Container image mode ---
    image = mkOption {
      type = types.str;
      default = "";
      description = "Server container image (used when mode = \"image\")";
    };

    imageTag = mkOption {
      type = types.str;
      default = "latest";
    };

    imagePullPolicy = mkOption {
      type = types.str;
      default = "Always";
    };

    # --- nix-csi mode ---
    flakeRef = mkOption {
      type = types.str;
      default = "";
      description = "Nix flake reference for the server environment (used when mode = \"nix-csi\")";
    };

    baseImage = mkOption {
      type = types.str;
      default = "busybox:latest";
      description = "Minimal base image for nix-csi mode (needs sh for symlink setup)";
    };

    csiDriverName = mkOption {
      type = types.str;
      default = "nix.csi.store";
    };

    # --- Common options ---
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
        default = "bedrock/us.anthropic.claude-sonnet-4-20250514";
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
        default = "";
        description = "Agent-server image for sandbox pods (used when mode = \"image\")";
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
    kubernetes.resources.${namespace} = {
      # --- ServiceAccount ---
      ServiceAccount.${name} = {
        metadata.annotations."eks.amazonaws.com/role-arn" = cfg.irsaRoleArn;
      };

      # --- RBAC: sandbox manager ---
      Role.openhands-sandbox-manager = {
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

      RoleBinding.openhands-sandbox-manager = {
        roleRef = {
          apiGroup = "rbac.authorization.k8s.io";
          kind = "Role";
          name = "openhands-sandbox-manager";
        };
        subjects = lib.mkNamedList {
          ${name}.kind = "ServiceAccount";
        };
      };

      # --- ConfigMap: default settings ---
      ConfigMap.openhands-default-settings = {
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
      Deployment.${name} = {
        metadata.labels.app = name;
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
              initContainers = lib.mkNumberedList {
                "0" = {
                  name = "seed-settings";
                  image = if useNixCsi then cfg.baseImage else "busybox:1.36";
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
                  volumeMounts = lib.mkNamedList {
                    openhands-home.mountPath = "/home";
                    default-settings.mountPath = "/defaults";
                  };
                };
              };
              containers = lib.mkNamedList ({
                ${name} = {
                  image = containerImage;
                  ports = lib.mkNamedList {
                    http = {
                      containerPort = 3000;
                      protocol = "TCP";
                    };
                  };
                  env = lib.mkNamedList ({
                    RUNTIME.value = "kubernetes";
                    SANDBOX_K8S_NAMESPACE.value = namespace;
                    SANDBOX_K8S_RESOURCE_REQUESTS.value = cfg.sandbox.resourceRequests;
                    SANDBOX_K8S_RESOURCE_LIMITS.value = cfg.sandbox.resourceLimits;
                    SANDBOX_NIX_PACKAGES.value = cfg.sandbox.nixPackages;
                    SANDBOX_K8S_ENV_SECRET.value = cfg.sandboxEnvSecretName;
                    OH_APP_CONVERSATION_SANDBOX_STARTUP_TIMEOUT.value = toString cfg.sandbox.startupTimeout;
                    LLM_MODEL.value = cfg.llm.model;
                    LLM_API_KEY.value = "unused";
                    LLM_AWS_REGION_NAME.value = cfg.llm.awsRegion;
                    AWS_DEFAULT_REGION.value = cfg.llm.awsRegion;
                    ENABLE_BROWSER.value = "false";
                    SKIP_DEPENDENCY_CHECK.value = "1";
                    GITHUB_TOKEN.valueFrom.secretKeyRef = {
                      name = cfg.secretName;
                      key = "github-token";
                      optional = true;
                    };
                  } // sandboxImageEnvs);
                  startupProbe = {
                    httpGet = { path = "/"; port = "http"; };
                    failureThreshold = if useNixCsi then 60 else 30;
                    periodSeconds = if useNixCsi then 10 else 5;
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
                    limits.memory = cfg.resources.limits.memory;
                  };
                  volumeMounts = lib.mkNamedList ({
                    workspace.mountPath = "/opt/workspace_base";
                    openhands-home.mountPath = "/root/.openhands";
                  } // lib.optionalAttrs useNixCsi {
                    nix-env = { mountPath = "/nix-csi"; readOnly = true; };
                  });
                } // lib.optionalAttrs (containerCommand != null) {
                  command = containerCommand;
                } // lib.optionalAttrs (!useNixCsi) {
                  imagePullPolicy = cfg.imagePullPolicy;
                };
              });
              volumes = lib.mkNamedList ({
                workspace.emptyDir = {};
                openhands-home.emptyDir = {};
                default-settings.configMap.name = "openhands-default-settings";
              } // lib.optionalAttrs useNixCsi {
                nix-env.csi = {
                  driver = cfg.csiDriverName;
                  readOnly = true;
                  volumeAttributes.flakeRef = cfg.flakeRef;
                };
              });
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
              port = 3000;
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
                    port.number = 3000;
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
