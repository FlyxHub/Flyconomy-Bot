"""Tests for error translation and bot wiring."""

from __future__ import annotations

from typing import Any

import discord
import pytest
from discord.ext import commands

from flyconomy import embeds
from flyconomy.bot import EXTENSIONS, _humanize, build_intents, describe_command_error
from flyconomy.errors import FlyconomyError, InsufficientFundsError


class TestIntents:
    def test_the_privileged_intents_the_features_need_are_requested(self):
        intents = build_intents()
        assert intents.message_content is True
        assert intents.members is True

    def test_unneeded_privileged_intents_are_not_requested(self):
        # Version 1 asked for Intents.all(), which requests presences too.
        assert build_intents().presences is False


class TestErrorMessages:
    def test_a_cooldown_reports_the_wait(self):
        error = commands.CommandOnCooldown(
            commands.Cooldown(1, 3600), 120.0, commands.BucketType.user
        )
        assert describe_command_error(error) == (
            "That command is on cooldown. Try again in 2 minutes."
        )

    def test_a_non_owner_is_told_the_command_is_restricted(self):
        assert describe_command_error(commands.NotOwner()) == (
            "That command is restricted to the bot owner."
        )

    def test_insufficient_funds_reports_the_shortfall(self):
        message = describe_command_error(InsufficientFundsError(40, 100))
        assert message is not None
        assert "40" in message
        assert "100" in message
        assert "funds" in message

    def test_insufficient_coins_names_the_currency(self):
        message = describe_command_error(InsufficientFundsError(1, 5, "Flyxcoin"))
        assert message is not None
        assert "Flyxcoin" in message

    def test_a_wrapped_error_is_unwrapped(self):
        wrapped = commands.CommandInvokeError(InsufficientFundsError(0, 10))
        message = describe_command_error(wrapped)
        assert message is not None
        assert "insufficient" in message.lower()

    def test_a_hybrid_command_run_as_a_slash_command_is_unwrapped(self):
        # discord.py wraps a hybrid command's exception twice on the slash
        # path: app_commands.CommandInvokeError, then commands.HybridCommandError
        # on top of that. Only the classic prefix path wraps once.
        async def callback(interaction: discord.Interaction) -> None:
            raise NotImplementedError

        original = InsufficientFundsError(0, 10)
        app_command: discord.app_commands.Command[Any, ..., Any] = discord.app_commands.Command(
            name="x", description="x", callback=callback
        )
        inner = discord.app_commands.CommandInvokeError(app_command, original)
        wrapped = commands.HybridCommandError(inner)
        message = describe_command_error(wrapped)
        assert message is not None
        assert "insufficient" in message.lower()

    def test_an_unknown_command_produces_no_reply(self):
        assert describe_command_error(commands.CommandNotFound()) == ""

    def test_a_deliberate_error_uses_its_own_message(self):
        assert describe_command_error(FlyconomyError("nope")) == "nope"

    def test_an_unexpected_error_is_left_for_the_caller_to_log(self):
        assert describe_command_error(RuntimeError("boom")) is None

    def test_a_missing_argument_names_the_parameter(self):
        parameter = commands.Parameter(name="amount", kind=commands.Parameter.POSITIONAL_OR_KEYWORD)
        message = describe_command_error(commands.MissingRequiredArgument(parameter))
        assert message == "You need to provide `amount`."


class TestHumanize:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.4, "1 second"),
            (1.0, "1 second"),
            (2.6, "3 seconds"),
            (59.0, "59 seconds"),
            (60.0, "1 minute"),
            (150.0, "2 minutes"),
            (3600.0, "1 hour"),
            (3900.0, "1h 5m"),
            (7200.0, "2 hours"),
            (86400.0, "24 hours"),
        ],
    )
    def test_delays_read_naturally(self, seconds, expected):
        assert _humanize(seconds) == expected


class TestExtensions:
    def test_every_declared_extension_is_importable(self):
        import importlib

        for extension in EXTENSIONS:
            module = importlib.import_module(extension)
            assert hasattr(module, "setup"), f"{extension} has no setup coroutine"


class TestEmbeds:
    def test_money_is_formatted_with_separators(self):
        assert embeds.money(1234567) == "$1,234,567"

    def test_coins_are_formatted_without_a_dollar_sign(self):
        assert embeds.coins(2500) == "2,500"

    def test_an_empty_leaderboard_says_so(self):
        embed = embeds.leaderboard_embed("Top", "By net worth", [], "UTC")
        assert embed.description is not None
        assert "Nobody has any money yet." in embed.description

    def test_a_leaderboard_numbers_every_entry(self):
        from flyconomy.database import LeaderboardEntry

        entries = [LeaderboardEntry(user_id=index, amount=100 - index) for index in range(3)]
        embed = embeds.leaderboard_embed("Top", "By net worth", entries, "UTC")
        assert embed.description is not None
        assert "1. $100 - <@0>" in embed.description
        assert "3. $98 - <@2>" in embed.description

    def test_ranks_are_positional_not_value_based(self):
        # Version 1 used list.index(), so tied amounts shared a rank number.
        from flyconomy.database import LeaderboardEntry

        entries = [LeaderboardEntry(user_id=index, amount=50) for index in range(3)]
        embed = embeds.leaderboard_embed("Top", "By net worth", entries, "UTC")
        assert embed.description is not None
        for rank in (1, 2, 3):
            assert f"{rank}. $50" in embed.description

    def test_an_error_embed_is_visibly_an_error(self):
        embed = embeds.error_embed("nope")
        assert embed.description == "nope"
        assert embed.color == embeds.ERROR_COLOR

    def test_timestamps_are_timezone_aware(self):
        assert embeds.now("UTC").tzinfo is not None

    def test_the_circulation_embed_values_the_supply(self):
        embed = embeds.circulation_embed(3, 10_000, "UTC")
        assert embed.fields[0].value == "$10,000"
        assert embed.fields[1].value == "3"
        assert embed.fields[2].value == "$30,000"

    def test_the_circulation_embed_uses_the_price_it_is_given(self):
        embed = embeds.circulation_embed(3, 5_000, "UTC")
        assert embed.fields[0].value == "$5,000"
        assert embed.fields[2].value == "$15,000"

    def test_the_ticker_reads_flat_with_no_prior_price(self):
        assert embeds.flx_ticker(10_000, 0) == "$10,000"

    def test_the_ticker_shows_a_rise(self):
        text = embeds.flx_ticker(10_500, 10_000)
        assert text.startswith("$10,500")
        assert "+5.0%" in text

    def test_the_ticker_shows_a_fall(self):
        text = embeds.flx_ticker(9_000, 10_000)
        assert text.startswith("$9,000")
        assert "-10.0%" in text

    def test_the_ticker_shows_no_change(self):
        text = embeds.flx_ticker(10_000, 10_000)
        assert "+0.0%" in text

    def test_the_brand_color_is_unchanged_from_version_1(self):
        assert discord.Color(0x13FF00) == embeds.BRAND_COLOR
