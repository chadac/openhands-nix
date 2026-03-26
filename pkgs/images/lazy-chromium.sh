#!/usr/bin/env bash
# Lazy Chromium wrapper — downloads Chromium from the Nix binary cache
# on first invocation, then execs the real binary.
#
# This keeps the sandbox image small (~200MB smaller) while still
# supporting browser-use when the agent needs web browsing.
# Subsequent calls (and other pods sharing the nix store via nix-csi)
# reuse the cached binary instantly.

set -euo pipefail

CACHE_FILE="/tmp/.chromium-nix-path"
# Pin to a specific nixpkgs commit for reproducibility
NIXPKGS_REF="nixpkgs#ungoogled-chromium"

get_chromium_path() {
    if [ -f "$CACHE_FILE" ]; then
        local cached
        cached="$(cat "$CACHE_FILE")"
        if [ -x "${cached}/bin/chromium" ]; then
            echo "$cached"
            return 0
        fi
    fi

    echo "[chromium] Fetching Chromium from Nix binary cache (first use)..." >&2
    local store_path
    store_path="$(nix build --no-link --print-out-paths "$NIXPKGS_REF" 2>&1)"
    if [ -z "$store_path" ] || [ ! -x "${store_path}/bin/chromium" ]; then
        echo "[chromium] ERROR: Failed to fetch Chromium from Nix cache" >&2
        echo "[chromium] nix build output: $store_path" >&2
        exit 1
    fi

    echo "$store_path" > "$CACHE_FILE"
    echo "[chromium] Ready: ${store_path}/bin/chromium" >&2
    echo "$store_path"
}

CHROMIUM_DIR="$(get_chromium_path)"
exec "${CHROMIUM_DIR}/bin/chromium" "$@"
