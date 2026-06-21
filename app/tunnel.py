"""Tunnel URL discovery via ngrok's local API."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# ngrok exposes its API on port 4040 by default.
# In Docker Compose, the service name is "ngrok".
NGROK_API_URLS = [
    "http://ngrok:4040/api/tunnels",  # Docker Compose (service name)
    "http://localhost:4040/api/tunnels",  # Local development
]


async def get_tunnel_url() -> str | None:
    """Query ngrok's local API to discover the public tunnel URL.

    Returns the public HTTPS URL, or None if ngrok is not running.
    """
    async with httpx.AsyncClient(timeout=3.0) as client:
        for api_url in NGROK_API_URLS:
            try:
                response = await client.get(api_url)
                if response.status_code == 200:
                    data = response.json()
                    tunnels = data.get("tunnels", [])
                    for tunnel in tunnels:
                        public_url = tunnel.get("public_url", "")
                        if public_url.startswith("https://"):
                            logger.info(
                                "Discovered tunnel URL",
                                extra={"tunnel_url": public_url},
                            )
                            return public_url
                    # If no HTTPS tunnel, fall back to any URL
                    if tunnels:
                        url = tunnels[0].get("public_url", "")
                        if url:
                            return url
            except Exception:
                continue

    return None


def get_webhook_url(tunnel_url: str | None) -> str | None:
    """Construct the full webhook URL from the tunnel base URL."""
    if not tunnel_url:
        return None
    return f"{tunnel_url.rstrip('/')}/webhook/github"
