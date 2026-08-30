"""Tests for settings loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from flyconomy.config import Settings, load_settings

_TOKEN = "not-a-real-token"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    """Isolate each test from the developer's own environment and .env file."""
    for name in list(__import__("os").environ):
        if name.startswith("FLYCONOMY_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


class TestDefaults:
    def test_only_the_token_is_required(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        settings = load_settings()
        assert settings.discord_token.get_secret_value() == _TOKEN
        assert settings.database_path == Path("data/bot.db")
        assert settings.command_prefix == "$"
        assert settings.timezone == "America/Chicago"
        assert settings.log_level == "INFO"
        assert settings.dev_guild_id is None
        assert settings.always_mine_user_ids == frozenset()

    def test_a_missing_token_is_rejected(self):
        with pytest.raises(ValidationError, match="discord_token"):
            load_settings()

    def test_the_token_is_not_shown_when_settings_are_printed(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        assert _TOKEN not in repr(load_settings())


class TestOverrides:
    def test_values_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_COMMAND_PREFIX", "!")
        monkeypatch.setenv("FLYCONOMY_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("FLYCONOMY_DATABASE_PATH", "/srv/flyconomy.db")

        settings = load_settings()
        assert settings.command_prefix == "!"
        assert settings.log_level == "DEBUG"
        assert settings.database_path == Path("/srv/flyconomy.db")

    def test_values_come_from_a_dotenv_file(self, tmp_path):
        (tmp_path / ".env").write_text(
            f"FLYCONOMY_DISCORD_TOKEN={_TOKEN}\nFLYCONOMY_TIMEZONE=UTC\n", encoding="utf-8"
        )
        settings = load_settings()
        assert settings.timezone == "UTC"

    def test_an_empty_prefix_is_rejected(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_COMMAND_PREFIX", "")
        with pytest.raises(ValidationError):
            load_settings()

    def test_an_unknown_log_level_is_rejected(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_LOG_LEVEL", "CHATTY")
        with pytest.raises(ValidationError):
            load_settings()


class TestTimezone:
    def test_a_known_timezone_is_accepted(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_TIMEZONE", "Europe/Berlin")
        assert load_settings().timezone == "Europe/Berlin"

    def test_an_unknown_timezone_is_rejected_at_startup(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_TIMEZONE", "Mars/Olympus_Mons")
        with pytest.raises(ValidationError, match="unknown IANA timezone"):
            load_settings()


class TestAlwaysMineUserIds:
    def test_a_single_id_is_parsed(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_ALWAYS_MINE_USER_IDS", "989732316123389957")
        assert load_settings().always_mine_user_ids == frozenset({989732316123389957})

    def test_a_comma_separated_list_is_parsed(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_ALWAYS_MINE_USER_IDS", "1, 2 ,3")
        assert load_settings().always_mine_user_ids == frozenset({1, 2, 3})

    def test_an_empty_value_yields_no_ids(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_ALWAYS_MINE_USER_IDS", "")
        assert load_settings().always_mine_user_ids == frozenset()


class TestCreatorTax:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        settings = load_settings()
        assert settings.creator_tax_user_id is None
        assert settings.creator_tax_rate == pytest.approx(0.05)

    def test_a_blank_user_id_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_CREATOR_TAX_USER_ID", "")
        assert load_settings().creator_tax_user_id is None

    def test_a_user_id_must_be_positive(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_CREATOR_TAX_USER_ID", "0")
        with pytest.raises(ValidationError):
            load_settings()

    def test_rake_and_tax_may_not_exceed_the_whole_take(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_LOTTERY_RAKE", "0.8")
        monkeypatch.setenv("FLYCONOMY_CREATOR_TAX_RATE", "0.3")
        with pytest.raises(ValidationError, match="lottery_rake"):
            load_settings()

    def test_rake_and_tax_may_add_up_to_exactly_the_whole_take(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_LOTTERY_RAKE", "0.7")
        monkeypatch.setenv("FLYCONOMY_CREATOR_TAX_RATE", "0.3")
        settings = load_settings()
        assert settings.lottery_rake == pytest.approx(0.7)
        assert settings.creator_tax_rate == pytest.approx(0.3)


class TestImmutability:
    def test_settings_cannot_be_changed_after_loading(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        settings = load_settings()
        with pytest.raises(ValidationError):
            settings.command_prefix = "!"  # type: ignore[misc]

    def test_a_blank_dev_guild_id_reads_as_unset(self, monkeypatch):
        # .env.example ships this key with an empty value, so copying it
        # verbatim must not fail startup.
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_DEV_GUILD_ID", "")
        assert load_settings().dev_guild_id is None

    def test_an_empty_token_is_rejected(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", "")
        with pytest.raises(ValidationError, match="discord_token"):
            load_settings()

    def test_the_shipped_example_file_loads_once_a_token_is_added(self, tmp_path):
        # Guards the documented setup path: copy .env.example, add a token, run.
        example = Path(__file__).resolve().parents[1] / ".env.example"
        content = example.read_text(encoding="utf-8").replace(
            "FLYCONOMY_DISCORD_TOKEN=", f"FLYCONOMY_DISCORD_TOKEN={_TOKEN}"
        )
        (tmp_path / ".env").write_text(content, encoding="utf-8")

        settings = load_settings()
        assert settings.discord_token.get_secret_value() == _TOKEN
        assert settings.dev_guild_id is None
        assert settings.always_mine_user_ids == frozenset()

    def test_a_dev_guild_id_must_be_positive(self, monkeypatch):
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", _TOKEN)
        monkeypatch.setenv("FLYCONOMY_DEV_GUILD_ID", "0")
        with pytest.raises(ValidationError):
            load_settings()

    def test_settings_can_be_built_directly_for_tests(self):
        settings = Settings(discord_token=_TOKEN)
        assert settings.command_prefix == "$"
