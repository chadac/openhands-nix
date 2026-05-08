# Per-sandbox ClusterIP Service.
{ config, ... }:

let
  cfg = config.sandbox;
in
{
  config.kubernetes.resources.services."oh-sandbox-${cfg.id}" = {
    metadata = {
      namespace = cfg.namespace;
      labels = cfg.labels;
    };
    spec = {
      selector = { "openhands.ai/sandbox-id" = cfg.id; };
      ports = [{
        port = cfg.port;
        targetPort = cfg.port;
        name = "http";
        protocol = "TCP";
      }];
    };
  };
}
