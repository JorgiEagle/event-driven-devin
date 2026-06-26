"""GitHub API client for reading issues from the target repository."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


def _is_devin_bot(login: str) -> bool:
    """Return True if a comment author login looks like a Devin bot."""
    return "devin" in login.lower()


class GitHubClient:
    """Reads issues from the configured target repository via GitHub API."""

    def __init__(self, settings: Settings) -> None:
        self._token = settings.github_token
        self._repo = settings.target_repo
        self._base_url = "https://api.github.com"

    @property
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def list_issues(
        self, state: str = "open", labels: str = "", per_page: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch issues from the target repository.

        Args:
            state: Issue state filter (open, closed, all).
            labels: Comma-separated label names to filter by.
            per_page: Number of issues per page (max 100).

        Returns:
            List of issue dicts from the GitHub API.
        """
        if not self._repo:
            logger.warning("No target_repo configured, cannot fetch issues")
            return []

        params: dict[str, Any] = {
            "state": state,
            "per_page": per_page,
            "sort": "created",
            "direction": "desc",
        }
        if labels:
            params["labels"] = labels

        url = f"{self._base_url}/repos/{self._repo}/issues"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=self._headers, params=params)

                if response.status_code == 401:
                    logger.error(
                        "GitHub API authentication failed",
                        extra={"repo": self._repo, "status_code": 401},
                    )
                    return []
                if response.status_code == 404:
                    logger.error(
                        "Repository not found or no access",
                        extra={"repo": self._repo, "status_code": 404},
                    )
                    return []
                if response.status_code != 200:
                    logger.error(
                        "GitHub API error",
                        extra={
                            "repo": self._repo,
                            "status_code": response.status_code,
                            "response": response.text[:300],
                        },
                    )
                    return []

                issues = response.json()
                # Filter out pull requests (GitHub API returns PRs in /issues)
                return [i for i in issues if "pull_request" not in i]

        except Exception as exc:
            logger.error(
                "GitHub API request failed",
                extra={"repo": self._repo, "error": str(exc)},
            )
            return []

    async def merge_pull_request(
        self, pr_number: int, merge_method: str = "squash"
    ) -> bool:
        """Merge a pull request by number.

        Returns True on success, False on failure.
        """
        if not self._repo:
            return False

        url = f"{self._base_url}/repos/{self._repo}/pulls/{pr_number}/merge"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.put(
                    url,
                    headers=self._headers,
                    json={"merge_method": merge_method},
                )
                if response.status_code == 200:
                    logger.info(
                        "PR merged successfully",
                        extra={
                            "repo": self._repo,
                            "pr_number": pr_number,
                            "merge_method": merge_method,
                        },
                    )
                    return True
                logger.warning(
                    "Failed to merge PR",
                    extra={
                        "repo": self._repo,
                        "pr_number": pr_number,
                        "status_code": response.status_code,
                        "response": response.text[:300],
                    },
                )
        except Exception as exc:
            logger.error(
                "GitHub API merge request failed",
                extra={
                    "repo": self._repo,
                    "pr_number": pr_number,
                    "error": str(exc),
                },
            )
        return False

    async def get_pull_request(self, pr_number: int) -> dict[str, Any] | None:
        """Fetch a single pull request by number."""
        if not self._repo:
            return None

        url = f"{self._base_url}/repos/{self._repo}/pulls/{pr_number}"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=self._headers)
                if response.status_code == 200:
                    return response.json()
                logger.warning(
                    "Failed to fetch PR",
                    extra={
                        "repo": self._repo,
                        "pr_number": pr_number,
                        "status_code": response.status_code,
                    },
                )
        except Exception as exc:
            logger.error(
                "GitHub API request failed",
                extra={
                    "repo": self._repo,
                    "pr_number": pr_number,
                    "error": str(exc),
                },
            )
        return None

    async def add_label(self, issue_number: int, label: str) -> bool:
        """Add a label to an issue (used by the dashboard's Begin button).

        Adding the trigger label causes GitHub to fire an ``issues.labeled``
        webhook, which is the single entry point for task creation.
        Returns True on success.
        """
        if not self._repo:
            return False

        url = f"{self._base_url}/repos/{self._repo}/issues/{issue_number}/labels"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url, headers=self._headers, json={"labels": [label]}
                )
                if response.status_code in (200, 201):
                    logger.info(
                        "Label added to issue",
                        extra={
                            "repo": self._repo,
                            "issue_number": issue_number,
                            "label": label,
                        },
                    )
                    return True
                logger.warning(
                    "Failed to add label to issue",
                    extra={
                        "repo": self._repo,
                        "issue_number": issue_number,
                        "label": label,
                        "status_code": response.status_code,
                        "response": response.text[:300],
                    },
                )
        except Exception as exc:
            logger.error(
                "GitHub API add-label request failed",
                extra={
                    "repo": self._repo,
                    "issue_number": issue_number,
                    "error": str(exc),
                },
            )
        return False

    async def get_pr_review_comments(self, pr_number: int) -> list[dict[str, Any]]:
        """Fetch Devin Review bot comments on a PR via the GitHub API.

        Combines PR-level reviews, inline review comments, and issue comments,
        keeping only those authored by a Devin bot. Each entry is normalized to
        ``{"author": ..., "body": ..., "url": ...}``.
        """
        if not self._repo:
            return []

        endpoints = [
            f"{self._base_url}/repos/{self._repo}/pulls/{pr_number}/reviews",
            f"{self._base_url}/repos/{self._repo}/pulls/{pr_number}/comments",
            f"{self._base_url}/repos/{self._repo}/issues/{pr_number}/comments",
        ]

        comments: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for url in endpoints:
                    response = await client.get(url, headers=self._headers)
                    if response.status_code != 200:
                        continue
                    for item in response.json():
                        user = item.get("user") or {}
                        login = user.get("login", "")
                        body = (item.get("body") or "").strip()
                        if not body:
                            continue
                        if not _is_devin_bot(login):
                            continue
                        comments.append({
                            "author": login,
                            "body": body,
                            "url": item.get("html_url", ""),
                        })
        except Exception as exc:
            logger.error(
                "GitHub API review-comments request failed",
                extra={
                    "repo": self._repo,
                    "pr_number": pr_number,
                    "error": str(exc),
                },
            )
        return comments

    async def get_issue(self, issue_number: int) -> dict[str, Any] | None:
        """Fetch a single issue by number."""
        if not self._repo:
            return None

        url = f"{self._base_url}/repos/{self._repo}/issues/{issue_number}"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=self._headers)
                if response.status_code == 200:
                    return response.json()
                logger.warning(
                    "Failed to fetch issue",
                    extra={
                        "repo": self._repo,
                        "issue_number": issue_number,
                        "status_code": response.status_code,
                    },
                )
        except Exception as exc:
            logger.error(
                "GitHub API request failed",
                extra={"repo": self._repo, "issue_number": issue_number, "error": str(exc)},
            )
        return None
