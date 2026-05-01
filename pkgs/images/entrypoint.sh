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

# --- OpenVSCode Server lazy setup ---
# VSCodeService checks /openhands/.openvscode-server/bin/openvscode-server
# at startup. We point it to the lazy wrapper so it passes the check, and
# the wrapper blocks until the real binary is installed via nix profile.
setup_lazy_vscode() {
    if [ -d /openhands/.openvscode-server ]; then
        # Full variant — already set up with real binary in image
        return 0
    fi
    local wrapper
    wrapper="$(command -v openvscode-server 2>/dev/null || true)"
    if [ -n "$wrapper" ]; then
        mkdir -p /openhands/.openvscode-server/bin
        ln -sf "$wrapper" /openhands/.openvscode-server/bin/openvscode-server
        echo "[entrypoint] Lazy OpenVSCode Server wrapper installed"
    fi
}

# --- Pre-warm Chromium ---
# browser-use's init timeout is 30s. The lazy-chromium wrapper downloads
# Chromium from the Nix binary cache on first use (~200MB), which can take
# longer than 30s. Pre-warm it in the background so it's ready when needed.
warm_chromium_background() {
    local chromium_bin
    chromium_bin="$(command -v chromium 2>/dev/null || true)"
    if [ -z "$chromium_bin" ]; then
        return 0
    fi

    (
        echo "[entrypoint] Pre-warming Chromium in background..."
        if "$chromium_bin" --version >/dev/null 2>&1; then
            echo "[entrypoint] Chromium pre-warmed successfully"
        else
            echo "[entrypoint] WARNING: Chromium pre-warm failed"
        fi
    ) &
}

# --- Main ---

# Set up writable Nix store if needed (must be synchronous — overlay mount
# needs to happen before the server, nix profile install, or chromium runs).
# Also needed for lazy-chromium download even without NIX_PACKAGES.
if [ -n "${NIX_PACKAGES:-}" ] || command -v chromium >/dev/null 2>&1; then
    setup_overlay_store || true
fi

if [ -n "${NIX_PACKAGES:-}" ]; then
    install_packages_background
fi

# Set up lazy VS Code wrapper before server starts
setup_lazy_vscode

# Pre-warm Chromium for browser-use (background, non-blocking)
warm_chromium_background

# Ensure nix profile bin is on PATH for the server process
export PATH="$HOME/.nix-profile/bin:$PATH"

# Configure git credential helper for broker-based GitHub auth
if [ -n "${BROKER_URL:-}" ]; then
    git config --global credential.helper broker
    echo "[entrypoint] Git credential helper configured (broker: $BROKER_URL)"
fi

echo "[entrypoint] Starting agent-server on ${HOST}:${PORT}"
exec python -m openhands.agent_server --host "$HOST" --port "$PORT" "$@"
