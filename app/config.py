"""Application configuration loaded from config.json and environment.

Precedence (highest wins): environment variables > config.json > defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime settings for the event-driven-devin service."""

    # GitHub
    github_webhook_secret: str = ""
    github_token: str = ""
    target_repo: str = ""  # e.g. "owner/repo"

    # Devin
    devin_api_token: str = ""

    # App
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: str = "./data"
    log_level: str = "INFO"

    # Label that triggers automation
    trigger_label: str = "assign-devin"

    model_config = {"env_prefix": "EDD_"}


def load_settings(config_path: Optional[str] = None) -> Settings:
    """Load settings: defaults < config.json < env vars.

    Env vars (EDD_*) always win over config.json values.
    """
    file_values: dict = {}
    path = Path(config_path) if config_path else Path("/app/config.json")
    if path.exists():
        with open(path) as f:
            file_values = json.load(f)

    # Let env vars override file values
    env_prefix = "EDD_"
    for key in list(file_values.keys()):
        env_key = env_prefix + key.upper()
        if env_key in os.environ:
            file_values.pop(key)

    return Settings(**file_values)
