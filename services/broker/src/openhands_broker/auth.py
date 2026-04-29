"""Authenticate sandbox requests via K8s ServiceAccount token validation.

Each sandbox pod has a projected ServiceAccount token with audience
``openhands-broker``. The broker validates the token via K8s TokenReview
and extracts the sandbox ID from the SA name.

SA naming convention: ``sandbox-{sandbox_id}``
"""

import logging
import re

from fastapi import HTTPException, Request

from .config import settings

logger = logging.getLogger(__name__)

_SA_PATTERN = re.compile(r"^system:serviceaccount:([^:]+):sandbox-(.+)$")

_k8s_authn = None


def _get_k8s_authn():
    """Lazily load the K8s AuthenticationV1Api client."""
    global _k8s_authn
    if _k8s_authn is None:
        from kubernetes import client, config
        config.load_incluster_config()
        _k8s_authn = client.AuthenticationV1Api()
    return _k8s_authn


async def _token_review(token: str) -> str | None:
    """Validate a token via K8s TokenReview API.

    Returns the authenticated username (e.g. ``system:serviceaccount:openhands:sandbox-abc123``)
    or None if the token is invalid.
    """
    try:
        from kubernetes import client
        api = _get_k8s_authn()
        review = client.V1TokenReview(
            spec=client.V1TokenReviewSpec(
                token=token,
                audiences=[settings.token_audience],
            )
        )
        result = api.create_token_review(review)
        if result.status.authenticated:
            return result.status.user.username
        return None
    except Exception as e:
        logger.warning("TokenReview failed: %s", e)
        return None


async def verify_sandbox_token(request: Request) -> str:
    """FastAPI dependency: validate Bearer token and return sandbox_id.

    Raises:
        HTTPException 401: Missing or invalid token.
        HTTPException 403: Token valid but SA doesn't match sandbox pattern.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]
    username = await _token_review(token)
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    match = _SA_PATTERN.match(username)
    if not match:
        raise HTTPException(
            status_code=403,
            detail=f"Token belongs to '{username}', not a sandbox ServiceAccount",
        )

    sandbox_id = match.group(2)
    logger.debug("Authenticated sandbox: %s (SA: %s)", sandbox_id, username)
    return sandbox_id


async def resolve_conversation_id(sandbox_id: str) -> str | None:
    """Resolve a sandbox ID to a conversation ID.

    Currently returns None (global credentials don't need conversation context).
    Will be implemented when per-conversation credential scoping is added —
    at that point the broker config will include openhands_api_url and this
    will query the conversations API to map sandbox_id -> conversation_id.
    """
    # TODO: implement when per-conversation scoping is needed
    return None
