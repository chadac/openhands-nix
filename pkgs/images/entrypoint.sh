#!/usr/bin/env bash
# Entrypoint for the OpenHands agent-server container.
#
# Dynamically installs Nix packages in the background while the
# agent-server starts immediately. This lets the server respond to
# health checks right away instead of blocking on package downloads.
#
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

# --- Background package installation ---
install_packages_background() {
    local packages="${NIX_PACKAGES:-}"
    if [ -z "$packages" ]; then
        return 0
    fi

    echo "[entrypoint] Installing packages in background: $packages"

    (
        # shellcheck disable=SC2086
        if nix profile install --no-write-lock-file --impure $packages 2>&1; then
            echo "[entrypoint] Background package installation complete"
        else
            echo "[entrypoint] WARNING: Background package installation failed"
        fi
    ) &
}

# --- Main ---

# Set up writable Nix store if needed (must be synchronous — overlay mount
# needs to happen before the server or nix profile install runs)
if [ -n "${NIX_PACKAGES:-}" ]; then
    setup_overlay_store || true
    install_packages_background
fi

# Ensure nix profile bin is on PATH for the server process
export PATH="$HOME/.nix-profile/bin:$PATH"

echo "[entrypoint] Starting agent-server on ${HOST}:${PORT}"
exec python -m openhands.agent_server --host "$HOST" --port "$PORT" "$@"
