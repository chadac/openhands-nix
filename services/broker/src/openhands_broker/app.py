"""OpenHands Broker — credential-injecting transparent proxy for agents.

Agents use standard CLI tools (gh, glab, jira, curl) configured to route
through the broker. The broker validates the calling sandbox's identity
via K8s ServiceAccount tokens, resolves the conversation context, and
injects appropriate credentials before forwarding to upstream APIs.
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Request, Response

from openhands_common import db

from .auth import verify_sandbox_token, resolve_conversation_id
from .config import db_settings, settings
from .github_app import get_installation_token, is_app_configured
from .proxy import proxy_request
from .providers.github import GitHubInjector
from .providers.gitlab import GitLabInjector
from .providers.jira import JiraInjector
from .providers.slack import SlackInjector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)

_github = GitHubInjector()
_gitlab = GitLabInjector()
_jira = JiraInjector()
_slack = SlackInjector()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db(db_settings)
    yield
    await db.close_db()


app = FastAPI(title="OpenHands Broker", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Transparent proxy routes
#
# Each route catches all sub-paths under the provider prefix and proxies
# them to the upstream API with injected credentials.
# ---------------------------------------------------------------------------


@app.api_route(
    "/github/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_github(
    request: Request, path: str,
    sandbox_id: str = Depends(verify_sandbox_token),
) -> Response:
    """Proxy requests to the GitHub API."""
    conv_id = await resolve_conversation_id(sandbox_id)
    return await proxy_request(
        request, settings.github_api_url, path, _github, conv_id,
    )


@app.api_route(
    "/gitlab/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_gitlab(
    request: Request, path: str,
    sandbox_id: str = Depends(verify_sandbox_token),
) -> Response:
    """Proxy requests to the GitLab API."""
    conv_id = await resolve_conversation_id(sandbox_id)
    return await proxy_request(
        request, settings.gitlab_api_url, path, _gitlab, conv_id,
    )


@app.api_route(
    "/jira/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_jira(
    request: Request, path: str,
    sandbox_id: str = Depends(verify_sandbox_token),
) -> Response:
    """Proxy requests to the Jira API."""
    conv_id = await resolve_conversation_id(sandbox_id)
    return await proxy_request(
        request, settings.jira_base_url, path, _jira, conv_id,
    )


@app.api_route(
    "/slack/{path:path}",
    methods=["GET", "POST"],
)
async def proxy_slack(
    request: Request, path: str,
    sandbox_id: str = Depends(verify_sandbox_token),
) -> Response:
    """Proxy requests to the Slack API."""
    conv_id = await resolve_conversation_id(sandbox_id)
    return await proxy_request(
        request, settings.slack_api_url, path, _slack, conv_id,
    )


@app.get("/git-credentials")
async def git_credentials(
    sandbox_id: str = Depends(verify_sandbox_token),
) -> Response:
    """Return a fresh GitHub installation token for git credential helpers.

    The sandbox's git credential helper calls this endpoint to get a token
    for HTTPS git operations. Returns the username and password in a format
    the credential helper script can parse.
    """
    if not is_app_configured() or not settings.github_app_installation_id:
        return Response(content="GitHub App not configured", status_code=503)

    token = await get_installation_token(settings.github_app_installation_id)
    if not token:
        return Response(content="Failed to generate installation token", status_code=502)

    import json
    return Response(
        content=json.dumps({
            "protocol": "https",
            "host": "github.com",
            "username": "x-access-token",
            "password": token,
        }),
        media_type="application/json",
    )


def main():
    uvicorn.run(
        "openhands_broker.app:app",
        host="0.0.0.0",
        port=8082,
        log_level="info",
    )
