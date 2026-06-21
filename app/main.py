"""FastAPI application entrypoint for event-driven-devin."""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import load_settings
from app.dashboard import router as dashboard_router
from app.logging_config import setup_logging
from app.store import TaskStore
from app.tunnel import get_tunnel_url, get_webhook_url
from app.webhook import router as webhook_router
from app.webhook_register import register_webhook


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    config_path = os.environ.get("EDD_CONFIG_PATH", "/app/config.json")
    settings = load_settings(config_path)

    setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    app = FastAPI(
        title="Event-Driven Devin",
        description="Automated issue remediation via Devin sessions",
        version="0.1.0",
    )

    # Attach shared state
    app.state.settings = settings
    app.state.store = TaskStore(data_dir=settings.data_dir)

    # Register routes
    app.include_router(webhook_router)
    app.include_router(dashboard_router)

    # Static files
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.on_event("startup")
    async def _on_startup() -> None:
        """Discover tunnel URL and auto-register webhook on startup."""
        # Wait briefly for ngrok to initialize
        await asyncio.sleep(2)

        tunnel_url = await get_tunnel_url()
        webhook_url = get_webhook_url(tunnel_url)

        if webhook_url:
            logger.info(
                "Tunnel active",
                extra={"webhook_url": webhook_url},
            )
            registered = await register_webhook(settings, webhook_url)
            if registered:
                logger.info("Webhook auto-registered with GitHub")
            else:
                logger.info(
                    "Webhook not auto-registered (configure manually)",
                    extra={"webhook_url": webhook_url},
                )
        else:
            logger.info("No tunnel detected - webhook will not receive external events")

    logger.info(
        "Application started",
        extra={
            "target_repo": settings.target_repo,
            "trigger_label": settings.trigger_label,
            "data_dir": settings.data_dir,
        },
    )

    return app


app = create_app()
