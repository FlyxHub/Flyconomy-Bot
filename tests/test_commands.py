"""Tests that the command surface is registered and complete.

These build the real bot and load the real extensions, so a broken decorator or
a cog that fails to register is caught here rather than at startup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from discord.ext import commands

from flyconomy.bot import EXTENSIONS, FlyconomyBot
from flyconomy.config import Settings

#: Commands version 1 offered to everyone, with the aliases it accepted.
V1_MEMBER_COMMANDS = {
    "balance": ("bal",),
    "deposit": ("dep",),
    "withdraw": (),
    "beg": (),
    "daily": (),
    "rob": (),
    "leaderboard": ("lb",),
    "wallets": (),
    "mine": (),
    "upgrade": (),
    "flx": (),
    "coinflip": ("cf",),
    "rps": (),
    "dice": (),
    "roulette": (),
}

#: Commands version 1 restricted to the bot owner.
V1_OWNER_COMMANDS = ("adminme", "adminmine", "reset")

#: Games added after version 1. Kept separate so the assertions above stay a
#: statement about what the rewrite preserved.
ADDED_COMMANDS = {
    "slots": ("slot",),
    "war": (),
}

ALL_MEMBER_COMMANDS = V1_MEMBER_COMMANDS | ADDED_COMMANDS


@pytest.fixture
async def bot() -> AsyncIterator[FlyconomyBot]:
    """Build a bot with every extension loaded, without connecting to Discord."""
    client = FlyconomyBot(Settings(discord_token="placeholder"))
    for extension in EXTENSIONS:
        await client.load_extension(extension)
    try:
        yield client
    finally:
        await client.close()


class TestMemberCommands:
    @pytest.mark.parametrize("name", sorted(V1_MEMBER_COMMANDS))
    async def test_every_version_1_command_still_exists(self, bot: FlyconomyBot, name: str):
        assert bot.get_command(name) is not None, f"{name} is missing"

    @pytest.mark.parametrize("name", sorted(V1_MEMBER_COMMANDS))
    async def test_every_version_1_command_is_also_a_slash_command(
        self, bot: FlyconomyBot, name: str
    ):
        command = bot.get_command(name)
        assert isinstance(command, commands.HybridCommand | commands.HybridGroup)

    @pytest.mark.parametrize(("name", "aliases"), sorted(V1_MEMBER_COMMANDS.items()))
    async def test_version_1_aliases_still_work(
        self, bot: FlyconomyBot, name: str, aliases: tuple[str, ...]
    ):
        command = bot.get_command(name)
        assert command is not None
        for alias in aliases:
            assert alias in command.aliases
            assert bot.get_command(alias) is command

    async def test_every_member_command_has_help_text(self, bot: FlyconomyBot):
        for name in V1_MEMBER_COMMANDS:
            command = bot.get_command(name)
            assert command is not None
            assert command.help, f"{name} has no docstring"


class TestAddedGames:
    @pytest.mark.parametrize("name", sorted(ADDED_COMMANDS))
    async def test_the_new_games_are_registered(self, bot: FlyconomyBot, name: str):
        assert bot.get_command(name) is not None

    @pytest.mark.parametrize("name", sorted(ADDED_COMMANDS))
    async def test_the_new_games_work_as_slash_commands(self, bot: FlyconomyBot, name: str):
        command = bot.get_command(name)
        assert isinstance(command, commands.HybridCommand | commands.HybridGroup)

    @pytest.mark.parametrize(("name", "aliases"), sorted(ADDED_COMMANDS.items()))
    async def test_the_new_games_expose_their_aliases(
        self, bot: FlyconomyBot, name: str, aliases: tuple[str, ...]
    ):
        command = bot.get_command(name)
        assert command is not None
        for alias in aliases:
            assert bot.get_command(alias) is command

    @pytest.mark.parametrize("name", sorted(ADDED_COMMANDS))
    async def test_the_new_games_have_no_cooldown(self, bot: FlyconomyBot, name: str):
        # Casino games are limited by the wallet, not by a timer.
        command = bot.get_command(name)
        assert command is not None
        assert command._buckets._cooldown is None


class TestFlxSubcommands:
    @pytest.mark.parametrize("action", ["buy", "sell", "send"])
    async def test_the_flx_actions_are_subcommands(self, bot: FlyconomyBot, action: str):
        assert bot.get_command(f"flx {action}") is not None

    async def test_flx_answers_with_no_action(self, bot: FlyconomyBot):
        group = bot.get_command("flx")
        assert isinstance(group, commands.HybridGroup)
        assert group.invoke_without_command is True

    async def test_flx_exposes_a_slash_fallback(self, bot: FlyconomyBot):
        names = {command.qualified_name for command in bot.tree.walk_commands()}
        assert "flx info" in names


class TestOwnerCommands:
    @pytest.mark.parametrize("name", V1_OWNER_COMMANDS)
    async def test_every_owner_command_still_exists(self, bot: FlyconomyBot, name: str):
        assert bot.get_command(name) is not None

    @pytest.mark.parametrize("name", V1_OWNER_COMMANDS)
    async def test_owner_commands_are_not_published_as_slash_commands(
        self, bot: FlyconomyBot, name: str
    ):
        published = {command.qualified_name for command in bot.tree.walk_commands()}
        assert name not in published

    async def test_a_sync_command_exists_for_republishing(self, bot: FlyconomyBot):
        assert bot.get_command("sync") is not None

    async def test_the_admin_cog_is_owner_only(self, bot: FlyconomyBot):
        cog = bot.get_cog("Admin")
        assert cog is not None
        assert cog.cog_check is not None


class TestCooldowns:
    @pytest.mark.parametrize(
        ("name", "seconds"),
        [("beg", 3), ("mine", 3600), ("rob", 3600), ("daily", 86400)],
    )
    async def test_version_1_cooldowns_are_unchanged(
        self, bot: FlyconomyBot, name: str, seconds: int
    ):
        command = bot.get_command(name)
        assert command is not None
        assert command._buckets._cooldown is not None
        assert command._buckets._cooldown.per == seconds

    async def test_untimed_commands_have_no_cooldown(self, bot: FlyconomyBot):
        for name in ("balance", "deposit", "withdraw", "upgrade"):
            command = bot.get_command(name)
            assert command is not None
            assert command._buckets._cooldown is None


class TestTree:
    async def test_the_slash_tree_covers_every_member_command(self, bot: FlyconomyBot):
        published = {command.qualified_name for command in bot.tree.walk_commands()}
        for name in ALL_MEMBER_COMMANDS:
            assert any(entry == name or entry.startswith(f"{name} ") for entry in published), (
                f"{name} is not published as a slash command"
            )

    async def test_no_command_name_is_registered_twice(self, bot: FlyconomyBot):
        names = [command.qualified_name for command in bot.tree.walk_commands()]
        assert len(names) == len(set(names))

    async def test_the_prefix_comes_from_settings(self):
        client = FlyconomyBot(Settings(discord_token="placeholder", command_prefix="!"))
        try:
            assert client.settings.command_prefix == "!"
        finally:
            await client.close()
