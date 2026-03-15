from __future__ import annotations

from typing import Literal

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env before pydantic-settings initializes so file values win
# over empty shell environment variables (e.g. ANTHROPIC_API_KEY="").
load_dotenv(override=True)

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseSettings):
    database_url: str = "sqlite:///fantasai.db"
    anthropic_api_key: str = ""
    odds_api_key: str = ""
    openweather_api_key: str = ""
    env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        upper = v.upper()
        if upper not in VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {VALID_LOG_LEVELS}, got '{v}'")
        return upper

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith(("postgresql://", "postgresql+psycopg2://", "sqlite:///")):
            raise ValueError(
                "database_url must start with 'postgresql://', "
                "'postgresql+psycopg2://', or 'sqlite:///'"
            )
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.env == "production"


settings = Settings()
