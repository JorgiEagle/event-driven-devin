"""Auto-register or update the GitHub webhook for the target repository."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


async def register_webhook(settings: Settings, webhook_url: str) -> bool:
    """Register or update the webhook on the target repository.

    Requires github_token with admin:repo_hook scope.
    Returns True if registration succeeded, False otherwise.
    """
    if not settings.github_token:
        logger.info("No github_token configured, skipping webhook auto-registration")
        return False

    if not settings.target_repo:
        logger.warning("No target_repo configured, cannot register webhook")
        return False

    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    base_url = f"https://api.github.com/repos/{settings.target_repo}/hooks"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Check if webhook already exists
        existing_hook_id = await _find_existing_hook(client, base_url, headers, webhook_url)

        if existing_hook_id:
            # Update existing webhook
            success = await _update_hook(client, base_url, existing_hook_id, headers, webhook_url, settings)
            if success:
                logger.info(
                    "Updated existing webhook",
                    extra={"webhook_url": webhook_url, "hook_id": existing_hook_id},
                )
            return success
        else:
            # Create new webhook
            success = await _create_hook(client, base_url, headers, webhook_url, settings)
            if success:
                logger.info(
                    "Registered new webhook",
                    extra={"webhook_url": webhook_url, "repo": settings.target_repo},
                )
            return success


async def _find_existing_hook(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    webhook_url: str,
) -> int | None:
    """Find an existing webhook that points to our URL (or any event-driven-devin hook)."""
    try:
        response = await client.get(base_url, headers=headers)
        if response.status_code != 200:
            return None

        hooks = response.json()
        for hook in hooks:
            config = hook.get("config", {})
            url = config.get("url", "")
            # Match if same URL or if it's an ngrok URL pointing to /webhook/github
            if url == webhook_url or url.endswith("/webhook/github"):
                return hook.get("id")
    except Exception as exc:
        logger.warning("Failed to list webhooks", extra={"error": str(exc)})

    return None


async def _create_hook(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    webhook_url: str,
    settings: Settings,
) -> bool:
    """Create a new webhook on the repository."""
    payload: dict[str, Any] = {
        "name": "web",
        "active": True,
        "events": ["issues"],
        "config": {
            "url": webhook_url,
            "content_type": "json",
        },
    }
    if settings.github_webhook_secret:
        payload["config"]["secret"] = settings.github_webhook_secret

    try:
        response = await client.post(base_url, headers=headers, json=payload)
        if response.status_code in (201, 200):
            return True
        logger.warning(
            "Failed to create webhook",
            extra={"status_code": response.status_code, "response": response.text[:300]},
        )
    except Exception as exc:
        logger.warning("Webhook creation request failed", extra={"error": str(exc)})

    return False


async def _update_hook(
    client: httpx.AsyncClient,
    base_url: str,
    hook_id: int,
    headers: dict[str, str],
    webhook_url: str,
    settings: Settings,
) -> bool:
    """Update an existing webhook's URL."""
    payload: dict[str, Any] = {
        "active": True,
        "events": ["issues"],
        "config": {
            "url": webhook_url,
            "content_type": "json",
        },
    }
    if settings.github_webhook_secret:
        payload["config"]["secret"] = settings.github_webhook_secret

    try:
        response = await client.patch(f"{base_url}/{hook_id}", headers=headers, json=payload)
        if response.status_code == 200:
            return True
        logger.warning(
            "Failed to update webhook",
            extra={"status_code": response.status_code, "hook_id": hook_id},
        )
    except Exception as exc:
        logger.warning("Webhook update request failed", extra={"error": str(exc)})

    return False
