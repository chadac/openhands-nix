#!/usr/bin/env bash
# Lazy OpenVSCode Server wrapper — waits for the background nix profile
# install to complete, then sets up the expected directory structure and
# execs the real binary.
#
# The agent-server's VSCodeService expects the binary at:
#   /openhands/.openvscode-server/bin/openvscode-server
#
# This wrapper is installed as "openvscode-server" in the image. On first
# invocation it waits for the real binary to appear in the nix profile,
# creates the /openhands/.openvscode-server symlink tree, and execs it.

set -euo pipefail

VSCODE_ROOT="/openhands/.openvscode-server"
PROFILE_BIN="$HOME/.nix-profile/bin/openvscode-server"
NIXPKGS_REF="nixpkgs#openvscode-server"

setup_vscode_root() {
    # Find the actual nix store path for openvscode-server
    local store_path
    store_path="$(realpath "$PROFILE_BIN")"
    # Go from .../bin/openvscode-server to the package root
    store_path="$(dirname "$(dirname "$store_path")")"

    local lib_dir="${store_path}/lib/openvscode-server"
    if [ ! -d "$lib_dir" ]; then
        echo "[openvscode-server] WARNING: lib dir not found at $lib_dir" >&2
        return 1
    fi

    # Create the expected directory structure
    mkdir -p "$VSCODE_ROOT"
    # Symlink all files from the nix store lib directory
    ln -sf "$lib_dir"/* "$VSCODE_ROOT/" 2>/dev/null || true
    # Ensure bin directory has the binary
    mkdir -p "$VSCODE_ROOT/bin"
    ln -sf "$PROFILE_BIN" "$VSCODE_ROOT/bin/openvscode-server"
}

# Wait for the nix profile install to make openvscode-server available
wait_for_binary() {
    if [ -x "$PROFILE_BIN" ]; then
        return 0
    fi

    echo "[openvscode-server] Waiting for background nix install to complete..." >&2

    # If openvscode-server is not in the profile yet, fetch it directly.
    # This handles the case where VSCodeService starts before nix profile
    # install finishes, or where it wasn't included in NIX_PACKAGES.
    if ! nix profile install --no-write-lock-file --impure "$NIXPKGS_REF" 2>&1; then
        echo "[openvscode-server] ERROR: Failed to install openvscode-server" >&2
        exit 1
    fi

    if [ ! -x "$PROFILE_BIN" ]; then
        echo "[openvscode-server] ERROR: Binary not found after install" >&2
        exit 1
    fi
}

wait_for_binary
setup_vscode_root
exec "$PROFILE_BIN" "$@"
