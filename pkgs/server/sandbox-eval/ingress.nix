# Per-sandbox Ingress (disabled by default).
#
# Set `ingress.domain` to auto-enable per-sandbox ingresses at
# `<sandbox-id>.<domain>`.
{ config, lib, ... }:

let
  cfg = config.sandbox;
  ing = cfg.ingress;
in
{
  options.sandbox.ingress = with lib; {
    enable = mkEnableOption "per-sandbox Ingress";

    domain = mkOption {
      type = types.str;
      default = "";
      description = "Base domain for per-sandbox ingresses. When set, host defaults to <id>.<domain>.";
    };

    host = mkOption {
      type = types.str;
      default = "";
      description = "Hostname for the Ingress rule (e.g. sandbox-<id>.example.com)";
    };

    ingressClassName = mkOption {
      type = types.nullOr types.str;
      default = null;
    };

    annotations = mkOption {
      type = types.attrsOf types.str;
      default = {};
    };

    tls = mkOption {
      type = types.listOf (types.attrsOf types.anything);
      default = [];
      description = "TLS configuration blocks for the Ingress";
    };

    path = mkOption {
      type = types.str;
      default = "/";
    };

    pathType = mkOption {
      type = types.str;
      default = "Prefix";
    };
  };

  config = lib.mkMerge [
    # Auto-enable ingress and set host when domain is configured
    (lib.mkIf (ing.domain != "") {
      sandbox.ingress.enable = lib.mkDefault true;
      sandbox.ingress.host = lib.mkDefault "${cfg.id}.${ing.domain}";
    })

    (lib.mkIf ing.enable {
    kubernetes.resources.ingresses."oh-sandbox-${cfg.id}" = {
      metadata = {
        namespace = cfg.namespace;
        labels = cfg.labels;
        annotations = {
          "external-dns.alpha.kubernetes.io/hostname" = ing.host;
        } // ing.annotations;
      };
      spec = {
        rules = [{
          host = ing.host;
          http.paths = [{
            path = ing.path;
            pathType = ing.pathType;
            backend.service = {
              name = "oh-sandbox-${cfg.id}";
              port.number = cfg.port;
            };
          }];
        }];
      } // lib.optionalAttrs (ing.ingressClassName != null) {
        ingressClassName = ing.ingressClassName;
      } // lib.optionalAttrs (ing.tls != []) {
        tls = ing.tls;
      };
    };
  })
  ];
}
