# openhands-nix

Nix packaging for the [OpenHands](https://github.com/All-Hands-AI/OpenHands) AI agent platform, with custom additions to make the agent sandbox more Nix-friendly.

> **Disclaimer:** This project was largely generated with the help of AI (Claude). It is experimental, likely broken in ways not yet discovered, and provided as-is with no guarantees. Use at your own risk.

## What's here

Nix flake that builds the full OpenHands stack from source, plus custom integrations that replace Docker-based sandboxes with Nix-managed environments:

- **SDK packages** — `openhands-sdk`, `openhands-tools`, `openhands-agent-server`, `openhands-workspace` from [software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) v1.14.0
- **CLI** — the `openhands` TUI from [OpenHands-CLI](https://github.com/OpenHands/OpenHands-CLI) v1.13.1
- **Server** — the `openhands-ai` backend + React frontend from the main [OpenHands](https://github.com/All-Hands-AI/OpenHands) repo
- **Container images** — OCI images built with `dockerTools.buildLayeredImage` (no Dockerfile), with Nix baked in so agents can install packages declaratively
- **Nix workspace backends** — alternative sandbox implementations (Kubernetes, CSI driver, local) that provision environments with `nix profile install` instead of apt/pip/Docker-in-Docker
- **Agent skills** — custom microagent skills that teach the agent to use `nix profile install`, `nix search`, etc. instead of `apt-get` or `brew`, so it knows how to work in its Nix-based sandbox

## Quick start

```bash
# Build the server container image
nix build .#server-image
docker load < result

# Run with docker-compose (needs AWS credentials for Bedrock)
cp .env.example .env  # edit with your config
docker compose up
```

The UI will be at `http://localhost:3000`.

## Packages

| Flake output | Description |
|---|---|
| `cli` | OpenHands CLI (default package) |
| `openhands-sdk` | Core SDK library |
| `openhands-tools` | Agent tool implementations |
| `openhands-agent-server` | FastAPI agent server |
| `openhands-workspace` | Workspace management |
| `openhands-server` | Main server backend |
| `openhands-frontend` | React frontend static build |
| `openhands-nix` | Nix workspace backends (K8s, CSI, local) |
| `server-image` | Server OCI image (UI + API + ProcessSandbox) |
| `agent-server-image` | Agent sandbox OCI image (with Nix for dynamic packages) |

## How the Nix integration works

Upstream OpenHands uses Docker-in-Docker for agent sandboxes — each conversation spins up a container. This project replaces that with Nix:

1. **Container images ship with Nix.** The agent-server image includes the Nix package manager and a pre-populated store. At runtime, an overlay store allows installing additional packages without modifying the read-only base layers.

2. **Agents know how to use Nix.** Custom skills (`skills/`) are injected into the agent's `~/.openhands/skills/` directory. These trigger on keywords like "install", "package", "apt", etc. and instruct the agent to use `nix profile install nixpkgs#<pkg>` instead of traditional package managers.

3. **ProcessSandboxService patches.** The server image patches several upstream bugs in the V1 `ProcessSandboxService` (process health check only accepted `STATUS_RUNNING` not `STATUS_SLEEPING`, stdout/stderr pipes caused deadlocks, empty `working_dir` caused `mkdir` failures).

4. **Workspace backends.** The `openhands-nix` package provides alternative workspace implementations — a Kubernetes backend that provisions pods with Nix packages via the entrypoint, a CSI driver backend that uses [nix-csi](https://github.com/chadac/nix-csi) for instant package provisioning, and a local backend that wraps commands in `nix shell`.

## Project structure

```
flake.nix                  # Entry point
overlays/python.nix        # Python package overlay (litellm, otel, fastmcp, etc.)
pkgs/
  sdk/default.nix          # 4 packages from software-agent-sdk monorepo
  cli/default.nix          # OpenHands CLI
  server/default.nix       # openhands-ai backend + React frontend
  kubernetes-workspace/    # Nix workspace backends
  images/
    server.nix             # Server container image
    agent-server.nix       # Agent sandbox container image
    entrypoint.sh          # Container entrypoint (overlay store setup)
    aws-credentials-server.py  # Sidecar for AWS credential forwarding
skills/                    # Nix-specific agent skills
docker-compose.yml         # Local dev setup
```

## Known limitations

- No browser/Playwright support (disabled via `enable_browser=False`)
- No Vertex AI / Google Cloud AI Platform support
- Many upstream Python version pins are relaxed — things may break on updates
- The ProcessSandboxService patches are fragile string replacements against upstream code
- WebSocket message delivery from the UI has rough edges
- litellm versions get yanked from PyPI regularly; builds may fail if the pinned version disappears
