#!/usr/bin/env bash
# review-app — CLI for managing companion dev environments from inside an OpenHands sandbox.
#
# Usage:
#   review-app create   Create companion environment (API + frontend + PostgreSQL)
#   review-app status   Check environment status
#   review-app wait     Wait until environment is ready
#   review-app restart  Restart environment pods
#   review-app destroy  Tear down environment
#   review-app logs <component>  Show logs (api, frontend, postgres)
#   review-app url      Print the review app URL
#
# Environment variables (set automatically in sandbox pods):
#   OPENHANDS_SANDBOX_ID       — sandbox identifier
#   OPENHANDS_CONVERSATION_ID  — conversation identifier (derived from URL if not set)
#   ENV_MANAGER_TOKEN_PATH     — path to projected ServiceAccount token
#   ENV_MANAGER_URL            — env-manager base URL (default: http://env-manager.openhands.svc.cluster.local:8080)

set -euo pipefail

ENV_MANAGER_URL="${ENV_MANAGER_URL:-http://env-manager.openhands.svc.cluster.local:8080}"

# Derive conversation ID from OPENHANDS_CONVERSATION_URL if OPENHANDS_CONVERSATION_ID isn't set
get_conversation_id() {
  if [ -n "${OPENHANDS_CONVERSATION_ID:-}" ]; then
    echo "$OPENHANDS_CONVERSATION_ID"
    return
  fi
  if [ -n "${OPENHANDS_CONVERSATION_URL:-}" ]; then
    # Extract last path segment: https://host/conversations/CONV_ID
    echo "${OPENHANDS_CONVERSATION_URL##*/}"
    return
  fi
  echo "Error: OPENHANDS_CONVERSATION_ID or OPENHANDS_CONVERSATION_URL must be set" >&2
  exit 1
}

get_sandbox_id() {
  if [ -n "${OPENHANDS_SANDBOX_ID:-}" ]; then
    echo "$OPENHANDS_SANDBOX_ID"
    return
  fi
  echo "Error: OPENHANDS_SANDBOX_ID must be set" >&2
  exit 1
}

get_token() {
  local token_path="${ENV_MANAGER_TOKEN_PATH:-/var/run/secrets/env-manager/token}"
  if [ ! -f "$token_path" ]; then
    echo "Error: Token not found at $token_path" >&2
    exit 1
  fi
  cat "$token_path"
}

auth_header() {
  echo "Authorization: Bearer $(get_token)"
}

# Get a GitHub token from the credential broker for API calls.
get_github_token() {
  local broker_url="${BROKER_URL:-}"
  local broker_token_path="${BROKER_TOKEN_PATH:-/var/run/secrets/broker/token}"

  if [ -n "$broker_url" ] && [ -f "$broker_token_path" ]; then
    local broker_token creds
    broker_token=$(cat "$broker_token_path")
    creds=$(curl -sf -H "Authorization: Bearer $broker_token" "$broker_url/git-credentials" 2>/dev/null) || true
    if [ -n "$creds" ]; then
      local password
      password=$(echo "$creds" | python3 -c "import sys,json; print(json.load(sys.stdin).get('password',''))" 2>/dev/null) || true
      if [ -n "$password" ]; then
        echo "$password"
        return
      fi
    fi
  fi

  # Fall back to GITHUB_TOKEN env var
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "$GITHUB_TOKEN"
    return
  fi

  echo "Error: No GitHub token available (broker not reachable, GITHUB_TOKEN not set)" >&2
  exit 1
}

# Extract GitHub repo slug (owner/repo) from the git remote URL.
get_repo_slug() {
  local code_dir="/workspace/code"
  local remote_url
  remote_url=$(cd "$code_dir" && git remote get-url origin 2>/dev/null) || {
    echo "Error: Could not get git remote URL" >&2
    exit 1
  }
  # Handle both HTTPS and SSH URLs:
  #   https://github.com/owner/repo.git  or  https://@github.com/owner/repo.git
  #   git@github.com:owner/repo.git
  echo "$remote_url" | sed -E 's|^https?://(@)?github\.com/||; s|^git@github\.com:||; s|\.git$||'
}

# Detect the PR number associated with the current git branch.
# Uses GitHub API directly (no `gh` CLI required).
get_pr_number() {
  local code_dir="/workspace/code"
  if [ ! -d "$code_dir/.git" ]; then
    echo "Error: No git repository found at $code_dir" >&2
    exit 1
  fi

  local branch repo_slug token result pr_number
  branch=$(cd "$code_dir" && git rev-parse --abbrev-ref HEAD 2>/dev/null) || {
    echo "Error: Could not determine current git branch" >&2
    exit 1
  }
  repo_slug=$(get_repo_slug)
  token=$(get_github_token)

  result=$(curl -sf -H "Authorization: token $token" \
    -H "Accept: application/vnd.github.v3+json" \
    "https://api.github.com/repos/${repo_slug}/pulls?head=${repo_slug%%/*}:${branch}&state=open" 2>/dev/null) || {
    echo "Error: Failed to query GitHub API for PRs" >&2
    exit 1
  }

  pr_number=$(echo "$result" | python3 -c "
import sys, json
prs = json.load(sys.stdin)
if not prs:
    sys.exit(1)
print(prs[0]['number'])
" 2>/dev/null) || {
    echo "Error: No pull request found for branch '${branch}'." >&2
    echo "Create a PR first: cd /workspace/code && git push -u origin ${branch}" >&2
    exit 1
  }

  echo "$pr_number"
}

CONV_ID=""
SANDBOX_ID=""

cmd_create() {
  CONV_ID=$(get_conversation_id)
  SANDBOX_ID=$(get_sandbox_id)
  local pr_number pr_title
  pr_number=$(get_pr_number)
  local repo_slug token
  repo_slug=$(get_repo_slug)
  token=$(get_github_token)
  pr_title=$(curl -sf -H "Authorization: token $token" \
    -H "Accept: application/vnd.github.v3+json" \
    "https://api.github.com/repos/${repo_slug}/pulls/${pr_number}" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")

  echo "Creating companion environment for PR #${pr_number}..."
  local result payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({
    'sandbox_id': sys.argv[1],
    'pr_number': int(sys.argv[2]),
    'pr_title': sys.argv[3],
}))
" "$SANDBOX_ID" "$pr_number" "$pr_title")
  result=$(curl -sf -X POST "${ENV_MANAGER_URL}/environments/${CONV_ID}" \
    -H "$(auth_header)" \
    -H "Content-Type: application/json" \
    -d "${payload}" 2>&1) || {
    echo "Error creating environment:" >&2
    echo "$result" >&2
    exit 1
  }

  local domain status
  domain=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['domain'])" 2>/dev/null || echo "unknown")
  status=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unknown")

  echo "Status:  $status"
  echo "URL:     https://${domain}"
  echo "API:     https://${domain}/api"
  echo ""
  echo "To browse without auth, set cookie: review-auth=openhands-review-2026"
  echo "Run 'review-app wait' to wait until the environment is ready."
}

cmd_status() {
  CONV_ID=$(get_conversation_id)

  local result
  result=$(curl -sf "${ENV_MANAGER_URL}/environments/${CONV_ID}" 2>&1) || {
    echo "No environment found for conversation ${CONV_ID}" >&2
    exit 1
  }

  local domain status pr_number created_at
  domain=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['domain'])")
  status=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  pr_number=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['pr_number'])")
  created_at=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created_at','unknown'))")

  echo "PR:      #${pr_number}"
  echo "Status:  $status"
  echo "URL:     https://${domain}"
  echo "Created: $created_at"
}

cmd_wait() {
  CONV_ID=$(get_conversation_id)

  echo "Waiting for environment to be ready..."
  local attempts=0
  local max_attempts=36  # 6 minutes at 10s intervals
  while [ $attempts -lt $max_attempts ]; do
    local result
    result=$(curl -sf "${ENV_MANAGER_URL}/environments/${CONV_ID}" 2>/dev/null) || {
      echo "  Environment not found yet..."
      sleep 10
      attempts=$((attempts + 1))
      continue
    }

    local status
    status=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unknown")

    if [ "$status" = "running" ]; then
      local domain
      domain=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['domain'])")
      echo "Environment ready!"
      echo "URL: https://${domain}"
      return 0
    fi

    echo "  Status: $status (waiting...)"
    sleep 10
    attempts=$((attempts + 1))
  done

  echo "Timed out waiting for environment to be ready." >&2
  exit 1
}

cmd_restart() {
  CONV_ID=$(get_conversation_id)

  echo "Restarting environment pods..."
  curl -sf -X POST "${ENV_MANAGER_URL}/environments/${CONV_ID}/restart" \
    -H "$(auth_header)" > /dev/null || {
    echo "Error restarting environment" >&2
    exit 1
  }
  echo "Restart initiated. Run 'review-app wait' to wait for it to come back up."
}

cmd_destroy() {
  CONV_ID=$(get_conversation_id)

  echo "Destroying environment..."
  curl -sf -X DELETE "${ENV_MANAGER_URL}/environments/${CONV_ID}" \
    -H "$(auth_header)" > /dev/null || {
    echo "Error destroying environment" >&2
    exit 1
  }
  echo "Environment destroyed."
}

cmd_logs() {
  CONV_ID=$(get_conversation_id)
  local component="${1:-}"

  if [ -z "$component" ]; then
    echo "Usage: review-app logs <api|frontend|postgres>" >&2
    exit 1
  fi

  local result
  result=$(curl -sf "${ENV_MANAGER_URL}/environments/${CONV_ID}/logs/${component}?tail=100" 2>&1) || {
    echo "Error fetching logs for ${component}" >&2
    exit 1
  }

  echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['logs'])"
}

cmd_url() {
  CONV_ID=$(get_conversation_id)

  local result
  result=$(curl -sf "${ENV_MANAGER_URL}/environments/${CONV_ID}" 2>&1) || {
    echo "No environment found" >&2
    exit 1
  }

  local domain
  domain=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['domain'])")
  echo "https://${domain}"
}

cmd_cookie() {
  echo "review-auth=openhands-review-2026"
}

cmd_help() {
  echo "review-app — Manage companion dev environments"
  echo ""
  echo "Usage: review-app <command> [args]"
  echo ""
  echo "Commands:"
  echo "  create              Create companion environment (requires PR)"
  echo "  status              Check environment status"
  echo "  wait                Wait until environment is ready"
  echo "  restart             Restart environment pods"
  echo "  destroy             Tear down environment"
  echo "  logs <component>    Show logs (api, frontend, postgres)"
  echo "  url                 Print the review app URL"
  echo "  cookie              Print auth cookie for browser access"
  echo "  help                Show this help"
}

case "${1:-help}" in
  create)  cmd_create ;;
  status)  cmd_status ;;
  wait)    cmd_wait ;;
  restart) cmd_restart ;;
  destroy) cmd_destroy ;;
  logs)    shift; cmd_logs "$@" ;;
  url)     cmd_url ;;
  cookie)  cmd_cookie ;;
  help|--help|-h) cmd_help ;;
  *)
    echo "Unknown command: $1" >&2
    cmd_help >&2
    exit 1
    ;;
esac
