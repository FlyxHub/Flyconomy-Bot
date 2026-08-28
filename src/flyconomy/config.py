"""Application configuration, loaded from the environment."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime settings.

    Every field is read from an environment variable of the same name, prefixed
    with ``FLYCONOMY_``, or from a ``.env`` file in the working directory.
    """

    model_config = SettingsConfigDict(
        env_prefix="FLYCONOMY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    discord_token: SecretStr = Field(
        min_length=1,
        description="Bot token from the Discord developer portal.",
    )
    database_path: Path = Field(
        default=Path("data/bot.db"),
        description="Path to the SQLite database file. Created if missing.",
    )
    command_prefix: str = Field(
        default="$",
        min_length=1,
        description="Prefix for classic text commands. Slash commands ignore this.",
    )
    timezone: str = Field(
        default="America/Chicago",
        description="IANA timezone used for embed timestamps.",
    )
    log_level: LogLevel = Field(default="INFO", description="Root logging level.")
    max_bet: Annotated[int, Field(gt=0)] = Field(
        default=100_000,
        description=(
            "Table limit: the most a member may stake on a single wager. Caps how "
            "far a doubling strategy can escalate, and bounds the damage any "
            "mispriced game can do before it is noticed."
        ),
    )
    rate_limit_actions: Annotated[int, Field(gt=0)] = Field(
        default=6,
        description="Game commands a member may run per rate_limit_seconds.",
    )
    rate_limit_seconds: Annotated[float, Field(gt=0)] = Field(
        default=10.0,
        description="Length of the rate limiting window, in seconds.",
    )
    dev_guild_id: Annotated[int, Field(gt=0)] | None = Field(
        default=None,
        description=(
            "Guild to sync slash commands to on startup. Guild syncs are instant, "
            "so set this while developing. Leave unset in production to sync globally."
        ),
    )
    # NoDecode stops pydantic-settings from trying to read this as JSON, which
    # lets the validator below accept a plain comma-separated list.
    always_mine_user_ids: Annotated[frozenset[int], NoDecode] = Field(
        default_factory=frozenset,
        description=(
            "Members whose mine attempts always succeed. Comma-separated in the "
            "environment. Preserved from the original bot, which hardcoded one ID."
        ),
    )

    @field_validator("dev_guild_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat a blank value as unset.

        A commented-out setting in ``.env.example`` is usually left in place with
        an empty value, such as ``FLYCONOMY_DEV_GUILD_ID=``. Reading that as
        "not set" is what the reader means, and avoids failing startup over a
        line nobody filled in.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("always_mine_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        """Accept a comma-separated string of IDs from the environment."""
        if isinstance(value, str):
            return frozenset(int(part) for part in value.split(",") if part.strip())
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Reject a timezone the standard library cannot resolve."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"unknown IANA timezone: {value!r}"
            raise ValueError(msg) from exc
        return value


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment.

    Returns:
        The validated settings.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    # Field values come from the environment, not from arguments.
    return Settings()
