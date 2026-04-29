"""Post/update comments on GitHub issues and PRs."""

import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def post_github_comment(
    owner: str, repo: str, issue_number: int, body: str,
) -> int | None:
    """Post a comment on a GitHub issue or PR. Returns comment ID or None."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/comments",
                headers=_headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            data = resp.json()
            comment_id = data.get("id")
            logger.info("Posted GitHub comment %s on %s/%s#%d", comment_id, owner, repo, issue_number)
            return comment_id
    except httpx.HTTPError as e:
        logger.error("Failed to post GitHub comment on %s/%s#%d: %s", owner, repo, issue_number, e)
        return None


async def update_github_comment(
    owner: str, repo: str, comment_id: int, body: str,
) -> None:
    """Update an existing GitHub comment."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}",
                headers=_headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            logger.debug("Updated GitHub comment %s on %s/%s", comment_id, owner, repo)
    except httpx.HTTPError as e:
        logger.error("Failed to update GitHub comment %s on %s/%s: %s", comment_id, owner, repo, e)


async def get_issue_or_pr(owner: str, repo: str, number: int) -> dict | None:
    """Get an issue or PR by number."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}",
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        logger.error("Failed to get issue %s/%s#%d: %s", owner, repo, number, e)
        return None
