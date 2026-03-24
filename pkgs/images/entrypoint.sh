#!/usr/bin/env bash
# Entrypoint for the OpenHands agent-server container.
#
# Dynamically installs Nix packages before starting the agent-server.
# Package requests come via the NIX_PACKAGES environment variable
# (space-separated list of installables, e.g. "nixpkgs#nodejs nixpkgs#ripgrep").
#
# The container ships with a read-only Nix store in the image layers.
# We set up a local overlay store so new packages layer on top without
# copying the entire base store.

set -euo pipefail

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

# --- Nix store overlay setup ---
# The image's /nix/store is read-only (OCI layers). We use an overlay
# filesystem to make it writable for new package installs.
setup_overlay_store() {
    if [ -w /nix/store ]; then
        # Already writable (e.g. running with a volume mount) — nothing to do
        return 0
    fi

    echo "[entrypoint] Setting up Nix store overlay..."
    mkdir -p /nix/overlay-upper /nix/overlay-work /nix/overlay-store

    # Mount overlayfs: lower=read-only image store, upper=writable layer
    if mount -t overlay overlay \
        -o lowerdir=/nix/store,upperdir=/nix/overlay-upper,workdir=/nix/overlay-work \
        /nix/overlay-store 2>/dev/null; then
        # Bind-mount the overlay back to /nix/store
        mount --bind /nix/overlay-store /nix/store
        echo "[entrypoint] Overlay store mounted successfully"
    else
        echo "[entrypoint] WARNING: Could not mount overlay (need privileged or CAP_SYS_ADMIN)"
        echo "[entrypoint] Falling back to writable tmpfs for new packages"
        # Fallback: copy store paths to tmpfs (works unprivileged but uses more memory)
        mkdir -p /tmp/nix-store-rw
        cp -a /nix/store/* /tmp/nix-store-rw/ 2>/dev/null || true
        mount --bind /tmp/nix-store-rw /nix/store 2>/dev/null || {
            echo "[entrypoint] WARNING: Cannot set up writable store. Dynamic packages disabled."
            return 1
        }
    fi
}

# --- Dynamic package installation ---
install_packages() {
    local packages="${NIX_PACKAGES:-}"
    if [ -z "$packages" ]; then
        echo "[entrypoint] No NIX_PACKAGES specified, skipping dynamic install"
        return 0
    fi

    echo "[entrypoint] Installing packages: $packages"

    # Use nix profile for multi-user or single-user installs
    # shellcheck disable=SC2086
    nix profile install --no-write-lock-file $packages

    # Update PATH to include newly installed packages
    export PATH="$HOME/.nix-profile/bin:$PATH"
    echo "[entrypoint] Package installation complete"
}

# --- Main ---

# Set up writable Nix store if needed
if [ -n "${NIX_PACKAGES:-}" ]; then
    setup_overlay_store || true
    install_packages
fi

echo "[entrypoint] Starting agent-server on ${HOST}:${PORT}"
exec python -m openhands.agent_server --host "$HOST" --port "$PORT" "$@"
