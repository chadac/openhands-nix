# OpenHands API Reference

OpenHands runs two HTTP servers: the **main server** (port 3000) that orchestrates
conversations and K8s resources, and per-sandbox **agent-servers** (port 8000) that
execute commands and run agents.

## Main Server API (port 3000)

The main server manages conversation lifecycle. All endpoints are prefixed with `/api`.

### Conversations

#### Create Conversation

```
POST /api/conversations
```

Creates a new conversation and triggers sandbox (K8s Job + Service + Ingress) creation.

**Request body:** `{}` (empty JSON object)

**Response:**
```json
{
  "conversation_id": "abc123...",
  "sandbox_id": "def456...",
  "sandbox": {
    "id": "def456...",
    "status": "STARTING"
  }
}
```

The sandbox starts asynchronously. Poll `GET /api/conversations/{id}` until
`sandbox.status` is `"RUNNING"`.

#### Get Conversation

```
GET /api/conversations/{conversation_id}
```

Returns conversation details and current sandbox status.

**Response:**
```json
{
  "conversation_id": "abc123...",
  "sandbox_id": "def456...",
  "sandbox": {
    "id": "def456...",
    "status": "RUNNING"
  }
}
```

**Sandbox statuses:** `STARTING`, `RUNNING`, `PAUSED`, `ERROR`, `MISSING`

If the sandbox's K8s Job is missing but its workspace PVC still exists, the server
auto-recreates the sandbox (conversation recovery).

#### Delete Conversation

```
DELETE /api/conversations/{conversation_id}
```

Deletes the conversation and cleans up K8s resources (Job, Service, Ingress).
The workspace PVC is left intact for potential recovery.

Returns `200`/`204` on success, `404` if already deleted.

### Configuration

#### Get Available Models

```
GET /api/options/models
```

Returns available LLM model configurations. Also used as a health check endpoint
(see docker-compose health checks).

### WebSocket (Socket.IO)

The frontend communicates with agents via Socket.IO at `/socket.io/`. This is
the primary real-time channel for the UI conversation loop.

---

## Agent-Server API (port 8000, per sandbox)

Each sandbox runs an agent-server that exposes REST endpoints for command
execution and (optionally) direct conversation.

### Routing & Path Prefix

When accessed through the ALB Ingress, requests arrive with a path prefix:
`/sandbox/{sandbox_id}/...`. The `StripPrefixMiddleware` removes this prefix
before FastAPI routes see it. The agent-server's `OH_WEB_URL` env var controls
the expected prefix.

When accessed via `kubectl port-forward` or cluster-internal Service DNS
(`oh-sandbox-{id}.{namespace}.svc.cluster.local:8000`), no prefix stripping
is needed.

### Health

#### Health Check

```
GET /health
```

Returns `200` when the agent-server is ready to accept requests.
Used by K8s readiness probes (initial delay 10s, period 5s, failure threshold 60).

#### Server Info

```
GET /server_info
```

Returns server metadata. May return `404` if not implemented by the sandbox image.

### Bash Command Execution

The bash API uses an async start-then-poll pattern.

#### Start Command

```
POST /api/bash/start_bash_command
```

**Request body:**
```json
{
  "command": "echo hello && ls /workspace",
  "timeout": 10
}
```

**Response:**
```json
{
  "id": "cmd-uuid-here"
}
```

The command runs asynchronously. Poll for output using the returned `id`.

#### Poll Command Output

```
GET /api/bash/bash_events/search
```

**Query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `command_id__eq` | string | Filter by command ID (required) |
| `kind__eq` | string | Event type filter, e.g. `"BashOutput"` |
| `limit` | integer | Max events to return |

**Response:**
```json
{
  "items": [
    {
      "stdout": "hello\nfile1.txt\n",
      "stderr": null,
      "exit_code": 0,
      "kind": "BashOutput"
    }
  ]
}
```

- `exit_code` is `null` while the command is still running.
- Once `exit_code` is set, the command is complete.
- `stdout` accumulates across events; concatenate all items.

#### Example: Run a command and get output

```python
import httpx, time

client = httpx.Client(base_url="http://localhost:8000", timeout=30)

# Start
r = client.post("/api/bash/start_bash_command", json={"command": "whoami", "timeout": 5})
cmd_id = r.json()["id"]

# Poll
while True:
    r = client.get("/api/bash/bash_events/search", params={
        "command_id__eq": cmd_id, "kind__eq": "BashOutput", "limit": 100,
    })
    for item in r.json().get("items", []):
        if item.get("exit_code") is not None:
            print(f"stdout: {item.get('stdout', '')}")
            print(f"exit_code: {item['exit_code']}")
            break
    else:
        time.sleep(0.5)
        continue
    break
```

### Conversation API (Optional)

Some sandbox images expose a direct conversation endpoint for agent interaction.

#### Send Message

```
POST /api/conversations
```

**Request body:**
```json
{
  "messages": [
    {"role": "user", "content": "What is 2+2?"}
  ]
}
```

**Response (inline):**
```json
{
  "id": "conv-uuid",
  "messages": [
    {"role": "user", "content": "What is 2+2?"},
    {"role": "assistant", "content": "4"}
  ]
}
```

If the response isn't inline, poll with the returned `id`:

```
GET /api/conversations/{id}
```

Returns `404`/`405` if the sandbox image doesn't support this API.

---

## Kubernetes Resources Per Sandbox

Creating a conversation provisions these K8s resources:

| Resource | Name Pattern | Purpose |
|----------|-------------|---------|
| Job | `oh-sandbox-{sandbox_id}` | Runs the agent-server container |
| Service | `oh-sandbox-{sandbox_id}` | ClusterIP with ports 8000 (http) and 8001 (vscode) |
| Ingress | `oh-sandbox-{sandbox_id}` | ALB path routing at `/sandbox/{id}/` (if external host configured) |
| PVC | `oh-workspace-{sandbox_id}` | 10Gi EBS gp3 for `/workspace` persistence |

All resources carry these labels:
```
openhands.ai/managed-by: openhands-nix-kubernetes
openhands.ai/sandbox-id: {sandbox_id}
openhands.ai/session-key-hash: {hash}
```

### Ingress Annotations

Sandbox Ingresses use `auth-type: none` — authentication is handled by session
API keys (`OH_SESSION_API_KEYS_0`) rather than OIDC/Cognito. The main server
Ingress uses Cognito auth.

---

## Authentication

### Main Server
ALB Ingress with Cognito OIDC. Bypassed via `kubectl port-forward` for testing.

### Agent-Server (Sandboxes)
Session API key — a 43-character base62 random string set in
`OH_SESSION_API_KEYS_0`. The key's SHA256 hash (truncated to 16 chars) is used
as a K8s label for lookup.

---

## Environment Variables

### Main Server Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RUNTIME` | — | Sandbox backend: `"kubernetes"`, `"docker"`, `"local"`, `"nix"` |
| `SERVE_FRONTEND` | — | Whether to serve the React frontend |
| `LLM_MODEL` | — | Model ID, e.g. `"bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0"` |
| `LLM_AWS_REGION_NAME` | — | AWS region for Bedrock calls |
| `SANDBOX_K8S_NAMESPACE` | `"openhands"` | K8s namespace for sandbox resources |
| `SANDBOX_K8S_IMAGE` | — | Agent-server container image |
| `SANDBOX_K8S_EXTERNAL_HOST` | — | ALB hostname for sandbox Ingress creation |
| `SANDBOX_K8S_INGRESS_GROUP` | — | ALB Ingress group name |
| `SANDBOX_K8S_INGRESS_CLASS` | `"alb-external"` | Ingress class |
| `SANDBOX_K8S_SERVICE_ACCOUNT` | — | Service account for sandbox pods |
| `SANDBOX_K8S_RESOURCE_REQUESTS` | `{"cpu":"250m","memory":"512Mi"}` | JSON resource requests |
| `SANDBOX_K8S_RESOURCE_LIMITS` | — | JSON resource limits |
| `SANDBOX_K8S_NODE_SELECTOR` | — | JSON node selector |
| `SANDBOX_K8S_TOLERATIONS` | — | JSON tolerations array |
| `SANDBOX_K8S_IMAGE_PULL_SECRETS` | — | Comma-separated secret names |
| `SANDBOX_HOST_PORT` | `3000` | Main server port (for webhook callbacks) |
| `SANDBOX_STARTUP_GRACE_SECONDS` | `300` | Grace period before sandbox marked ERROR |
| `SANDBOX_NIX_PACKAGES` | — | Space-separated Nix packages for agent tools |

### Agent-Server Configuration (set per sandbox)

| Variable | Description |
|----------|-------------|
| `PORT` | Listen port (always `8000`) |
| `HOST` | Listen address (always `0.0.0.0`) |
| `OH_SESSION_API_KEYS_0` | Session API key for authentication |
| `OH_WEB_URL` | External URL including path prefix (triggers StripPrefixMiddleware) |
| `OH_CONVERSATIONS_PATH` | Path for conversation data (`/workspace/conversations`) |
| `OH_BASH_EVENTS_DIR` | Path for bash event logs (`/workspace/bash_events`) |
| `OH_WEBHOOKS_0_BASE_URL` | Webhook callback URL to main server |
| `NIX_PACKAGES` | Space-separated Nix flake refs installed at pod startup |

---

## Data Models

### SandboxStatus (enum)

| Value | Meaning |
|-------|---------|
| `STARTING` | Pod is Pending or containers are initializing |
| `RUNNING` | Pod is Running with all containers ready |
| `PAUSED` | Job suspended (pod terminated, PVC retained) |
| `ERROR` | Pod failed or exceeded startup grace period |
| `MISSING` | Job not found (may trigger auto-recreation if PVC exists) |

### NixEnvironment

Configuration for Nix package provisioning in sandboxes:

```python
class NixEnvironment(BaseModel):
    packages: list[str]        # Nix installables, e.g. ["nixpkgs#nodejs"]
    flake_ref: str | None      # Flake reference for dev shell
    nix_expr: str | None       # Raw Nix expression
    nixpkgs_ref: str           # Nixpkgs flake ref (default: nixos-unstable)
```

### Workspace Backends

| Backend | Class | Description |
|---------|-------|-------------|
| Kubernetes | `KubernetesWorkspace` | Creates K8s Job, installs Nix via `NIX_PACKAGES` env |
| Nix CSI | `NixCSIWorkspace` | K8s Job with CSI ephemeral volume (pre-built Nix closures) |
| Local | `LocalNixEnvironment` | Wraps commands in `nix shell` locally |
