# ExternalSecrets easykubenix module
#
# Creates ServiceAccount, SecretStore, and ExternalSecret resources
# to sync secrets from AWS Secrets Manager into Kubernetes.
#
# Prerequisites:
#   - ExternalSecrets operator must be installed separately
#     (e.g. via helm install external-secrets external-secrets/external-secrets)
#   - The CRDs (ClusterSecretStore, ExternalSecret) must exist in the cluster
#
{ config, lib, ... }:

let
  cfg = config.openhands.externalSecrets;
  namespace = config.openhands.namespace;
in
{
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
    # Register CRD apiVersions so easykubenix knows how to render them
    kubernetes.apiMappings = {
      ClusterSecretStore = "external-secrets.io/v1beta1";
      ExternalSecret = "external-secrets.io/v1beta1";
    };

    kubernetes.resources = {
      # ServiceAccount for ExternalSecrets operator (in its own namespace)
      none.Namespace.external-secrets = {};

      external-secrets.ServiceAccount.external-secrets = {
        metadata.annotations."eks.amazonaws.com/role-arn" = cfg.irsaRoleArn;
      };

      # ClusterSecretStore (cluster-scoped)
      none.ClusterSecretStore.aws-secrets-manager = {
        spec.provider.aws = {
          service = "SecretsManager";
          region = cfg.awsRegion;
          auth.jwt.serviceAccountRef = {
            name = "external-secrets";
            namespace = "external-secrets";
          };
        };
      };
    } // lib.mapAttrs' (_name: secret: {
      # ExternalSecret resources go in the openhands namespace
      name = namespace;
      value.ExternalSecret.${_name} = {
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
      };
    }) cfg.secrets;
  };
}
