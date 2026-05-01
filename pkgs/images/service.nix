# Build OCI container images for OpenHands microservices (webhooks, lifecycle, broker).
#
# These are lightweight FastAPI services — much smaller than the server image.
# Each gets its own Python environment with only the deps it needs.
#
# Usage:
#   nix build .#webhooks-image
#   nix run .#webhooks-image.copyToRegistry
#
{ pkgs, lib, pythonPackages, openhands-common, openhands-webhooks, openhands-lifecycle, openhands-broker, n2c }:

let
  mkServiceImage = {
    name,
    package,
    port ? 8080,
    tag ? "latest",
    entrypointModule,
    extraPackages ? [],
  }:
  let
    servicePython = pythonPackages.python.withPackages (ps: [
      package
      ps.uvicorn
    ]);

    systemPackages = with pkgs; [ coreutils bash cacert ] ++ extraPackages;

    rootfs = pkgs.runCommand "${name}-rootfs" {} ''
      mkdir -p $out/tmp $out/etc
      chmod 1777 $out/tmp
      echo "nobody:x:65534:65534:nobody:/:/bin/false" > $out/etc/passwd
      echo "nobody:x:65534:" > $out/etc/group
    '';

    entrypoint = pkgs.writeShellApplication {
      name = "${name}-entrypoint";
      runtimeInputs = [ servicePython ] ++ systemPackages;
      text = ''
        PORT="''${PORT:-${toString port}}"
        HOST="''${HOST:-0.0.0.0}"
        echo "[entrypoint] Starting ${name} on ''${HOST}:''${PORT}"
        exec python -m uvicorn ${entrypointModule} \
          --host "$HOST" --port "$PORT" "$@"
      '';
    };

    rootBinEnv = pkgs.buildEnv {
      name = "${name}-env";
      paths = [ servicePython entrypoint ] ++ systemPackages;
      pathsToLink = [ "/bin" "/etc/ssl" ];
    };
  in
  n2c.buildImage {
    inherit name tag;
    maxLayers = 40;
    copyToRoot = [ rootfs rootBinEnv ];
    config = {
      Entrypoint = [ "${entrypoint}/bin/${name}-entrypoint" ];
      ExposedPorts = { "${toString port}/tcp" = {}; };
      WorkingDir = "/";
      User = "nobody";
      Env = [
        "PATH=${lib.makeBinPath ([ servicePython entrypoint ] ++ systemPackages)}:/usr/bin:/bin"
        "PORT=${toString port}"
        "HOST=0.0.0.0"
        "PYTHONDONTWRITEBYTECODE=1"
        "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      ];
    };
  };
in
{
  webhooks-image = mkServiceImage {
    name = "openhands-webhooks";
    package = openhands-webhooks;
    entrypointModule = "openhands_webhooks.app:app";
  };

  lifecycle-image = mkServiceImage {
    name = "openhands-lifecycle";
    package = openhands-lifecycle;
    entrypointModule = "openhands_lifecycle.app:app";
  };

  broker-image = mkServiceImage {
    name = "openhands-broker";
    package = openhands-broker;
    entrypointModule = "openhands_broker.app:app";
  };
}
