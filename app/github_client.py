"""GitHub API client for reading issues from the target repository."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


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
