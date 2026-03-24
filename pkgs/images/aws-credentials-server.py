"""Tiny HTTP server that serves AWS credentials from a local profile.

Designed to run as a sidecar container, serving credentials via the
AWS_CONTAINER_CREDENTIALS_FULL_URI protocol (http://localhost:<port>/credentials).

Usage:
  AWS_PROFILE=claude-code python aws-credentials-server.py

Environment variables:
  AWS_PROFILE       - AWS profile to read credentials from (default: "default")
  CREDENTIALS_PORT  - Port to listen on (default: 9911)
"""

import json
import os
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import boto3
import botocore.session


PROFILE = os.environ.get("AWS_PROFILE", "default")
PORT = int(os.environ.get("CREDENTIALS_PORT", "9911"))

# Cache credentials and refresh before expiry
_cache = {"credentials": None, "lock": threading.Lock()}


def get_credentials():
    with _cache["lock"]:
        creds = _cache["credentials"]
        # Refresh if expired or within 5 minutes of expiry
        if creds and creds.get("Expiration"):
            exp = datetime.fromisoformat(creds["Expiration"].replace("Z", "+00:00"))
            if (exp - datetime.now(timezone.utc)).total_seconds() > 300:
                return creds

        session = botocore.session.Session(profile=PROFILE)
        resolver = session.get_component("credential_provider")
        provider = resolver.load_credentials()
        if provider is None:
            raise RuntimeError(f"Could not load credentials for profile '{PROFILE}'")

        resolved = provider.get_frozen_credentials()
        result = {
            "AccessKeyId": resolved.access_key,
            "SecretAccessKey": resolved.secret_key,
        }
        if resolved.token:
            result["Token"] = resolved.token

        # The container credentials protocol requires an Expiration field.
        # botocore raises KeyError('Expiration') if it's missing.
        # Use the actual expiry if available, otherwise default to 15 min.
        if hasattr(provider, '_expiry_time') and provider._expiry_time:
            result["Expiration"] = provider._expiry_time.isoformat()
        else:
            result["Expiration"] = (
                datetime.now(timezone.utc) + timedelta(minutes=15)
            ).isoformat()

        _cache["credentials"] = result
        return result


class CredentialHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/credentials":
            try:
                creds = get_credentials()
                body = json.dumps(creds).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress request logging to keep sidecar quiet
        pass


if __name__ == "__main__":
    print(f"[aws-creds] Serving credentials for profile '{PROFILE}' on :{PORT}", flush=True)
    # Validate credentials on startup
    try:
        creds = get_credentials()
        key_prefix = creds["AccessKeyId"][:8] + "..."
        print(f"[aws-creds] Credentials loaded OK (key: {key_prefix})", flush=True)
    except Exception as e:
        print(f"[aws-creds] WARNING: Could not load credentials on startup: {e}", flush=True)

    server = HTTPServer(("0.0.0.0", PORT), CredentialHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
