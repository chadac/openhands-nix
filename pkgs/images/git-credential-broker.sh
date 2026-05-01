#!/usr/bin/env bash
# Git credential helper that fetches GitHub credentials from the broker service.
#
# Usage: git config --global credential.helper /path/to/git-credential-broker
#
# When git needs credentials for github.com, this script reads a projected
# ServiceAccount token and calls the broker's /git-credentials endpoint to
# get a fresh GitHub App installation token.
#
# Environment variables:
#   BROKER_URL        — broker service URL (e.g. http://openhands-broker.openhands.svc.cluster.local:8080)
#   BROKER_TOKEN_PATH — path to the projected SA token file

set -euo pipefail

# Only handle "get" requests
if [ "${1:-}" != "get" ]; then
    exit 0
fi

# Read the git credential protocol input
host=""
protocol=""
while IFS='=' read -r key value; do
    case "$key" in
        host) host="$value" ;;
        protocol) protocol="$value" ;;
    esac
done

# Only handle github.com HTTPS requests
if [ "$host" != "github.com" ] || [ "$protocol" != "https" ]; then
    exit 0
fi

BROKER_URL="${BROKER_URL:-}"
BROKER_TOKEN_PATH="${BROKER_TOKEN_PATH:-/var/run/secrets/broker/token}"

if [ -z "$BROKER_URL" ] || [ ! -f "$BROKER_TOKEN_PATH" ]; then
    exit 0
fi

TOKEN=$(cat "$BROKER_TOKEN_PATH")

RESPONSE=$(curl -sf -H "Authorization: Bearer $TOKEN" "$BROKER_URL/git-credentials" 2>/dev/null) || exit 0

USERNAME=$(echo "$RESPONSE" | jq -r '.username // empty' 2>/dev/null) || exit 0
PASSWORD=$(echo "$RESPONSE" | jq -r '.password // empty' 2>/dev/null) || exit 0

if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
    exit 0
fi

echo "protocol=https"
echo "host=github.com"
echo "username=$USERNAME"
echo "password=$PASSWORD"
