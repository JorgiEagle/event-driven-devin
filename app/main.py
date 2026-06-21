"""FastAPI application entrypoint for event-driven-devin."""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import load_settings
from app.dashboard import router as dashboard_router
from app.logging_config import setup_logging
from app.store import TaskStore
from app.webhook import router as webhook_router


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
