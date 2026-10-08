import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLUME_", env_file=".env", extra="ignore")

    database_path: Path = Path("data/plume.sqlite3")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_api_keys: str = ""  # Comma-separated list of API keys for rotation
    gemini_model: str = Field(default="gemini-3.5-flash", min_length=1)
    gemini_fallback_model: str = "gemini-3.5-flash-lite"
    max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    provider_timeout_seconds: float = Field(default=120, ge=1, le=120)
    context_turns: int = Field(default=20, ge=1, le=100)
    context_days: int = Field(default=7, ge=1, le=30)
    context_characters: int = Field(default=24000, ge=1000, le=100000)
    daily_team_requests: int = Field(default=200, ge=1, le=10000)
    daily_user_requests: int = Field(default=40, ge=1, le=1000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    bridge_token: SecretStr = SecretStr("")
    team_config_path: Path = Path("data/team.json")
    drive_token_path: Path = Path("secrets/google-drive-token.json")
    incoming_path: Path = Path("data/incoming")
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    document_search_enabled: bool = True
    document_sync_seconds: int = Field(default=120, ge=30, le=3600)
    document_max_files: int = Field(default=20, ge=1, le=50)
    document_max_total_bytes: int = Field(default=200 * 1024 * 1024, ge=1024)
    document_model: str = "gemini-3.5-flash-lite"
    context_path: Path = Path("context/local")
    calendar_token_path: Path = Path("secrets/google-calendar-token.json")
    search_model: str = "gemini-2.5-flash"
    search_provider: Literal["ddgs", "gemini"] = "ddgs"
    default_timezone: str = "Africa/Cairo"
    week_start: int = Field(default=5, ge=0, le=6)


class TeamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_phone: str = Field(pattern=r"^[1-9][0-9]{7,14}$")
    group_name: str = Field(min_length=1)
    group_id: str = Field(default="", pattern=r"^$|^[0-9-]+@g\.us$")
    drive_folder_id: str = Field(default="", pattern=r"^[A-Za-z0-9_-]*$")


def load_team(settings: Settings) -> TeamConfig | None:
    if not settings.team_config_path.is_file():
        return None
    return TeamConfig.model_validate(json.loads(settings.team_config_path.read_text("utf-8")))
