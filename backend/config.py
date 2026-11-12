from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / ".env"


class ConfigError(RuntimeError):
    """Raised when .env is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: Optional[int]
    log_level: str
    google_api_key: str
    agent_model: str
    agent_timeout: float
    agent_max_steps: int
    agent_requests_per_minute: int
    daily_job_limit: int
    self_ping_url: Optional[str]
    self_ping_seconds: int
    port: Optional[int]


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number.") from exc


def load_settings() -> Settings:
    load_dotenv(ENV_PATH)

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is required.")

    google_api_key = (
        (os.getenv("GOOGLE_API_KEY") or "").strip()
        or (os.getenv("GEMINI_API_KEY") or "").strip()
    )
    if not google_api_key:
        raise ConfigError("GOOGLE_API_KEY (or GEMINI_API_KEY) is required.")

    owner_raw = (os.getenv("OWNER_TELEGRAM_ID") or "").strip()
    owner_id: Optional[int] = None
    if owner_raw:
        try:
            owner_id = int(owner_raw)
        except ValueError as exc:
            raise ConfigError("OWNER_TELEGRAM_ID must be a numeric user id.") from exc

    log_level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()

    port_raw = (os.getenv("PORT") or "").strip()
    port: Optional[int] = None
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError("PORT must be an integer.") from exc

    self_ping_url = (
        (os.getenv("SELF_PING_URL") or "").strip()
        or (os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    ).rstrip("/") or None

    return Settings(
        bot_token=token,
        owner_id=owner_id,
        log_level=log_level,
        google_api_key=google_api_key,
        agent_model=(os.getenv("AGENT_MODEL") or "gemini-3.6-flash").strip(),
        agent_timeout=_env_float("AGENT_TIMEOUT", 600.0),
        # Research, write, validate, and a small correction allowance. More
        # turns are expensive and can burst through a 5-RPM free-tier quota.
        agent_max_steps=max(10, _env_int("AGENT_MAX_STEPS", 10)),
        agent_requests_per_minute=max(1, _env_int("AGENT_REQUESTS_PER_MINUTE", 4)),
        daily_job_limit=max(1, _env_int("DAILY_JOB_LIMIT", 3)),
        self_ping_url=self_ping_url,
        self_ping_seconds=max(120, _env_int("SELF_PING_SECONDS", 840)),
        port=port,
    )
