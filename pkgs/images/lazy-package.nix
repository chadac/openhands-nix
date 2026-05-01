# mkLazyPackage — generate lazy shims for a Nix package's binaries.
#
# Instead of including a large package in the image (adding hundreds of MB),
# this creates lightweight shell scripts that download the real package from
# the Nix binary cache on first invocation, then exec the real binary.
#
# Usage:
#   mkLazyPackage {
#     name = "chromium";
#     flakeRef = "nixpkgs#ungoogled-chromium";
#     binaries = [ "chromium" ];  # which binaries to shim
#   }
#
# The resulting derivation has bin/<name> for each binary in `binaries`.
# On first call, the shim runs `nix build --no-link --print-out-paths <flakeRef>`,
# caches the store path, and execs the real binary. Subsequent calls use the cache.
#
# Requirements:
#   - `nix` must be on PATH at runtime
#   - The nix store must be writable (overlay or volume mount)
{ pkgs }:

{ name
, flakeRef
, binaries
}:

let
  shimScript = binary: ''
    set -euo pipefail

    CACHE_FILE="/tmp/.lazy-nix-${name}"

    get_store_path() {
        if [ -f "$CACHE_FILE" ]; then
            local cached
            cached="$(cat "$CACHE_FILE")"
            if [ -d "$cached" ]; then
                echo "$cached"
                return 0
            fi
        fi

        echo "[lazy-nix] Fetching ${name} from Nix binary cache (first use)..." >&2
        local store_path
        local nix_log="/tmp/.lazy-nix-${name}.log"
        if ! store_path="$(nix build --no-link --print-out-paths "${flakeRef}" 2>"$nix_log")"; then
            echo "[lazy-nix] ERROR: Failed to fetch ${name}" >&2
            cat "$nix_log" >&2
            exit 1
        fi
        if [ -z "$store_path" ] || [ ! -d "$store_path" ]; then
            echo "[lazy-nix] ERROR: nix build succeeded but output path invalid: $store_path" >&2
            exit 1
        fi

        echo "$store_path" > "$CACHE_FILE"
        echo "[lazy-nix] Ready: $store_path" >&2
        echo "$store_path"
    }

    STORE_PATH="$(get_store_path)"
    exec "''${STORE_PATH}/bin/${binary}" "$@"
  '';

  mkShim = binary: pkgs.writeShellScriptBin binary (shimScript binary);
in
pkgs.symlinkJoin {
  name = "lazy-${name}";
  paths = map mkShim binaries;
}
