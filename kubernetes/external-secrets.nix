# ExternalSecrets kubenix module
#
# Deploys the ExternalSecrets operator (via Helm) and creates
# SecretStore + ExternalSecret resources to sync secrets from
# AWS Secrets Manager into Kubernetes.
#
# NOTE: This module uses kubenix's Helm integration to deploy the
# external-secrets operator chart, and raw k8s resources for the
# SecretStore and ExternalSecret CRDs.
#
{ config, lib, kubenix, ... }:

let
  cfg = config.openhands.externalSecrets;
  namespace = config.openhands.namespace;
in
{
  imports = [ kubenix.modules.k8s ];

  options.openhands.externalSecrets = with lib; {
    enable = mkEnableOption "ExternalSecrets operator + AWS Secrets Manager sync";

    irsaRoleArn = mkOption {
      type = types.str;
      description = "IAM role ARN for the ExternalSecrets service account (IRSA)";
    };

    awsRegion = mkOption {
      type = types.str;
      default = "us-east-1";
    };

    secretsManagerPrefix = mkOption {
      type = types.str;
      default = "dev/openhands";
      description = "AWS Secrets Manager path prefix (e.g. dev/openhands)";
    };

    # Map of ExternalSecret name -> AWS secret key -> K8s secret data key
    secrets = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          secretName = mkOption {
            type = types.str;
            description = "Name of the K8s Secret to create";
          };
          awsSecretName = mkOption {
            type = types.str;
            description = "Name of the secret in AWS Secrets Manager";
          };
          keys = mkOption {
            type = types.attrsOf types.str;
            description = "Map of K8s secret key -> AWS secret JSON key";
            example = { github-token = "github_token"; };
          };
        };
      });
      default = {};
      description = "ExternalSecret definitions to create";
    };
  };

  config = lib.mkIf cfg.enable {
    kubernetes.resources = {
      # --- ServiceAccount for ExternalSecrets operator ---
      serviceAccounts.external-secrets = {
        metadata = {
          namespace = "external-secrets";
          annotations = {
            "eks.amazonaws.com/role-arn" = cfg.irsaRoleArn;
          };
        };
      };

      namespaces.external-secrets = {};

      # --- Custom resources (SecretStore + ExternalSecrets) ---
      # These use the kubernetes.customResources mechanism since they're CRDs.
    };

    # SecretStore and ExternalSecret are CRDs — we define them as custom types.
    # For now, we output them as part of the manifest and they'll be applied
    # after the external-secrets operator is running.
    #
    # TODO: Use kubenix's CRD support once external-secrets CRDs are generated.
    # For the initial deployment, the operator + CRDs should be installed first
    # (e.g. via `helm install external-secrets external-secrets/external-secrets`)
    # and then these resources can be applied.
    kubernetes.customResources.secret-stores = lib.mkIf cfg.enable [{
      apiVersion = "external-secrets.io/v1beta1";
      kind = "ClusterSecretStore";
      metadata.name = "aws-secrets-manager";
      spec = {
        provider.aws = {
          service = "SecretsManager";
          region = cfg.awsRegion;
          auth.jwt.serviceAccountRef = {
            name = "external-secrets";
            namespace = "external-secrets";
          };
        };
      };
    }];

    kubernetes.customResources.external-secrets = lib.mkIf cfg.enable
      (lib.mapAttrsToList (name: secret: {
        apiVersion = "external-secrets.io/v1beta1";
        kind = "ExternalSecret";
        metadata = {
          inherit (config.openhands) namespace;
          inherit name;
        };
        spec = {
          refreshInterval = "1h";
          secretStoreRef = {
            name = "aws-secrets-manager";
            kind = "ClusterSecretStore";
          };
          target = {
            name = secret.secretName;
            creationPolicy = "Owner";
          };
          data = lib.mapAttrsToList (k8sKey: awsKey: {
            secretKey = k8sKey;
            remoteRef = {
              key = "${cfg.secretsManagerPrefix}/${secret.awsSecretName}";
              property = awsKey;
            };
          }) secret.keys;
        };
      }) cfg.secrets);
  };
}
