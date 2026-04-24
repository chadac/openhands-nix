# nix-csi kubenix module
#
# Deploys the nix-csi CSI driver as a DaemonSet. This pre-provisions /nix
# on cluster nodes so pods can mount Nix store paths without baking them
# into container images.
#
# Components:
#   - CSIDriver resource
#   - DaemonSet (init container bootstraps Nix store, main container runs CSI plugin)
#   - ServiceAccount + RBAC (ClusterRole + Role)
#   - ConfigMap (nix.conf + node-env.nix)
#
{ config, lib, kubenix, ... }:

let
  cfg = config.openhands.nixCsi;
  namespace = cfg.namespace;
in
{
  imports = [ kubenix.modules.k8s ];

  options.openhands.nixCsi = with lib; {
    enable = mkEnableOption "nix-csi CSI driver";

    namespace = mkOption {
      type = types.str;
      default = "nix-csi";
      description = "Namespace for the nix-csi DaemonSet and supporting resources";
    };

    image = mkOption {
      type = types.str;
      default = "ghcr.io/lillecarl/nix-csi/scratch";
    };

    imageTag = mkOption {
      type = types.str;
      default = "1.0.1";
    };

    initImage = mkOption {
      type = types.str;
      default = "nixos/nix:latest";
      description = "Nix image used to bootstrap the store on the host";
    };

    nodeEnvExpr = mkOption {
      type = types.str;
      default = ''
        let
          src = builtins.fetchGit {
            url = "https://github.com/Lillecarl/nix-csi";
            ref = "main";
          };
          d = import src {};
          pkgs = import d.inputs.nixpkgs {
            system = "x86_64-linux";
            overlays = [ (import (src + "/pkgs")) ];
          };
        in
        pkgs.callPackage (src + "/environments/node") { dinix = d.inputs.dinix; }
      '';
      description = "Nix expression to build the node environment";
    };

    csiDriverName = mkOption {
      type = types.str;
      default = "nix.csi.store";
    };

    hostMountPath = mkOption {
      type = types.str;
      default = "/var/lib/nix-csi";
    };

    nodeBuildTimeout = mkOption {
      type = types.int;
      default = 1800;
    };

    rsyncConcurrency = mkOption {
      type = types.int;
      default = 1;
    };

    nixConfig = mkOption {
      type = types.str;
      default = ''
        experimental-features = nix-command flakes read-only-local-store
        sandbox = false
        trusted-users = root nix
        allowed-users = *
        builders-use-substitutes = true
        warn-dirty = false
        store = daemon
        extra-substituters = https://nix-csi.cachix.org
        extra-trusted-public-keys = nix-csi.cachix.org-1:JxGgLNeaRvzmRWo5+jawcSvKAEfFSQNW7aabxrRun0w=
      '';
    };

    registrarImage = mkOption {
      type = types.str;
      default = "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.15.0";
    };

    livenessImage = mkOption {
      type = types.str;
      default = "registry.k8s.io/sig-storage/livenessprobe:v2.17.0";
    };
  };

  config = lib.mkIf cfg.enable {
    kubernetes.resources = {
      # --- Namespace ---
      namespaces.${namespace} = {};

      # --- CSIDriver ---
      cSIDrivers.${cfg.csiDriverName} = {
        spec = {
          attachRequired = false;
          podInfoOnMount = true;
          volumeLifecycleModes = [ "Ephemeral" ];
          fsGroupPolicy = "File";
          requiresRepublish = false;
          storageCapacity = false;
        };
      };

      # --- ServiceAccount ---
      serviceAccounts.nix-csi = {
        metadata.namespace = namespace;
      };

      # --- ClusterRole ---
      clusterRoles.nix-csi = {
        rules = [
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [ "get" "list" "watch" ];
          }
          {
            apiGroups = [ "events.k8s.io" ];
            resources = [ "events" ];
            verbs = [ "get" "list" "watch" "create" "update" "patch" ];
          }
        ];
      };

      clusterRoleBindings.nix-csi = {
        roleRef = {
          apiGroup = "rbac.authorization.k8s.io";
          kind = "ClusterRole";
          name = "nix-csi";
        };
        subjects = [{
          kind = "ServiceAccount";
          name = "nix-csi";
          inherit namespace;
        }];
      };

      # --- Role (namespace-scoped) ---
      roles.nix-csi = {
        metadata.namespace = namespace;
        rules = [
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [ "get" "list" "watch" ];
          }
          {
            apiGroups = [ "" ];
            resources = [ "secrets" "configmaps" ];
            verbs = [ "get" "list" "create" "patch" "delete" ];
          }
        ];
      };

      roleBindings.nix-csi = {
        metadata.namespace = namespace;
        roleRef = {
          apiGroup = "rbac.authorization.k8s.io";
          kind = "Role";
          name = "nix-csi";
        };
        subjects = [{
          kind = "ServiceAccount";
          name = "nix-csi";
          inherit namespace;
        }];
      };

      # --- ConfigMap ---
      configMaps.nix-node = {
        metadata.namespace = namespace;
        data = {
          "nix.conf" = cfg.nixConfig;
          "node-env.nix" = cfg.nodeEnvExpr;
        };
      };

      # --- DaemonSet ---
      daemonSets.nix-node = {
        metadata = {
          inherit namespace;
          labels.app = "nix-csi";
        };
        spec = {
          selector.matchLabels.app = "nix-csi-node";
          updateStrategy = {
            type = "RollingUpdate";
            rollingUpdate.maxUnavailable = 1;
          };
          template = {
            metadata.labels.app = "nix-csi-node";
            spec = {
              serviceAccountName = "nix-csi";
              priorityClassName = "system-node-critical";
              tolerations = [{
                key = "node-role.kubernetes.io/control-plane";
                operator = "Exists";
                effect = "NoSchedule";
              }];

              initContainers = [{
                name = "initcopy";
                image = cfg.initImage;
                imagePullPolicy = "Always";
                command = [ "/bin/sh" "-c" ];
                args = [
                  ''
                    set -eux
                    nix build --impure \
                      -f /etc/nix/node-env.nix \
                      --extra-experimental-features "nix-command flakes" \
                      --option substituters "https://cache.nixos.org https://nix-csi.cachix.org" \
                      --option trusted-public-keys "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= nix-csi.cachix.org-1:JxGgLNeaRvzmRWo5+jawcSvKAEfFSQNW7aabxrRun0w=" \
                      --option require-sigs false \
                      --max-jobs auto \
                      --option sandbox false \
                      --store /nix-volume \
                      --out-link /nix-volume/nix/var/result \
                      --fallback
                    mkdir -p /nix-volume/nix/var/nix/gcroots/nix-csi
                    mkdir -p /nix-volume/nix/var/nix-csi/volumes
                  ''
                ];
                securityContext.privileged = true;
                volumeMounts = [
                  { name = "nix-store"; mountPath = "/nix-volume"; }
                  { name = "nix-config"; mountPath = "/etc/nix"; }
                ];
                resources = {
                  requests = { cpu = "500m"; memory = "512Mi"; };
                };
              }];

              containers = [
                # nix-csi node plugin
                {
                  name = "nix-node";
                  image = "${cfg.image}:${cfg.imageTag}";
                  imagePullPolicy = "Always";
                  command = [ "dinit" "--log-file" "/var/log/dinit.log" "--quiet" "csi" ];
                  env = [
                    { name = "CSI_ENDPOINT"; value = "unix:///csi/csi.sock"; }
                    { name = "NODE_ID"; valueFrom.fieldRef.fieldPath = "spec.nodeName"; }
                    { name = "KUBE_NODE_NAME"; valueFrom.fieldRef.fieldPath = "spec.nodeName"; }
                    { name = "KUBE_POD_NAME"; valueFrom.fieldRef.fieldPath = "metadata.name"; }
                    { name = "KUBE_POD_UID"; valueFrom.fieldRef.fieldPath = "metadata.uid"; }
                    { name = "KUBE_NAMESPACE"; valueFrom.fieldRef.fieldPath = "metadata.namespace"; }
                    { name = "BUILDERS_ENABLED"; value = "false"; }
                    { name = "CACHE_ENABLED"; value = "false"; }
                    { name = "NIX_BUILD_TIMEOUT"; value = toString cfg.nodeBuildTimeout; }
                    { name = "RSYNC_CONCURRENCY"; value = toString cfg.rsyncConcurrency; }
                    { name = "HOME"; value = "/nix/var/nix-csi/root"; }
                  ];
                  securityContext.privileged = true;
                  volumeMounts = [
                    { name = "kubelet-dir"; mountPath = "/var/lib/kubelet"; mountPropagation = "Bidirectional"; }
                    { name = "csi-socket-dir"; mountPath = "/csi"; }
                    { name = "nix-store"; mountPath = "/nix"; subPath = "nix"; mountPropagation = "Bidirectional"; }
                    { name = "nix-config"; mountPath = "/etc/nix"; }
                  ];
                  resources = {
                    requests = { cpu = "100m"; memory = "128Mi"; };
                  };
                }
                # CSI node driver registrar
                {
                  name = "csi-node-driver-registrar";
                  image = cfg.registrarImage;
                  args = [
                    "--csi-address=/csi/csi.sock"
                    "--kubelet-registration-path=/var/lib/kubelet/plugins/${cfg.csiDriverName}/csi.sock"
                  ];
                  volumeMounts = [
                    { name = "csi-socket-dir"; mountPath = "/csi"; }
                    { name = "registration-dir"; mountPath = "/registration"; }
                  ];
                  resources = {
                    requests = { cpu = "10m"; memory = "10Mi"; };
                  };
                }
                # Liveness probe
                {
                  name = "livenessprobe";
                  image = cfg.livenessImage;
                  args = [ "--csi-address=/csi/csi.sock" ];
                  volumeMounts = [
                    { name = "csi-socket-dir"; mountPath = "/csi"; }
                  ];
                  resources = {
                    requests = { cpu = "10m"; memory = "10Mi"; };
                  };
                }
              ];

              volumes = [
                { name = "kubelet-dir"; hostPath = { path = "/var/lib/kubelet"; type = "Directory"; }; }
                { name = "csi-socket-dir"; hostPath = { path = "/var/lib/kubelet/plugins/${cfg.csiDriverName}"; type = "DirectoryOrCreate"; }; }
                { name = "registration-dir"; hostPath = { path = "/var/lib/kubelet/plugins_registry"; type = "Directory"; }; }
                { name = "nix-store"; hostPath = { path = cfg.hostMountPath; type = "DirectoryOrCreate"; }; }
                { name = "nix-config"; configMap.name = "nix-node"; }
              ];
            };
          };
        };
      };
    };
  };
}
