"""The bot client: startup, extension loading, and centralized error handling."""

from __future__ import annotations

import logging
import time
from typing import Any, Final

import discord
from discord.ext import commands

from flyconomy import __version__, embeds
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import (
    BetTooLargeError,
    DailyBuyLimitError,
    FlyconomyError,
    InsufficientFundsError,
    RateLimitedError,
)
from flyconomy.ratelimit import SlidingWindowLimiter
from flyconomy.views import announce_achievements

log = logging.getLogger(__name__)

#: Extensions loaded at startup, in order.
EXTENSIONS: Final[tuple[str, ...]] = (
    "flyconomy.cogs.economy",
    "flyconomy.cogs.mining",
    "flyconomy.cogs.market",
    "flyconomy.cogs.gambling",
    "flyconomy.cogs.lottery",
    "flyconomy.cogs.tournament",
    "flyconomy.cogs.events",
    "flyconomy.cogs.guide",
    "flyconomy.cogs.admin",
    "flyconomy.cogs.api",
)

#: A command that took at least this long, start to finish, gets logged. Slow
#: enough to matter for Discord's 3-second interaction deadline, rare enough
#: in normal operation that it shouldn't fire on a healthy bot.
_SLOW_COMMAND_SECONDS: Final = 1.0


def _log_if_slow(ctx: commands.Context[commands.Bot]) -> None:
    """Log a command whose total time, start to finish, was worth noticing.

    ``flyconomy_started_at`` is set in ``BaseCog.cog_check``, right as the
    command is accepted, so the elapsed time here covers the whole rest of the
    invocation: the command body's database calls and the final ``ctx.send``.
    """
    started = getattr(ctx, "flyconomy_started_at", None)
    if started is None:
        return
    elapsed = time.monotonic() - started
    if elapsed >= _SLOW_COMMAND_SECONDS:
        log.warning("Command %s took %.2fs to finish", ctx.command, elapsed)


def build_intents() -> discord.Intents:
    """Return the gateway intents the bot needs.

    Version 1 requested every intent. This asks only for the two privileged
    intents the features actually require: message content, so classic ``$``
    commands are readable, and members, so a name can be resolved to a member.

    Returns:
        The configured intents.
    """
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    return intents


class _HelpCommand(commands.DefaultHelpCommand):
    """The stock help, except that it will not describe a hidden command.

    ``hidden`` only keeps a command out of the listing; asked for by name,
    the stock help prints its whole docstring to anyone.
    """

    async def send_command_help(self, command: commands.Command[Any, ..., Any], /) -> None:
        """Answer for a hidden command exactly as for one that does not exist."""
        if command.hidden:
            await self.send_error_message(self.command_not_found(command.qualified_name))
            return
        await super().send_command_help(command)

    def subcommand_not_found(self, command: commands.Command[Any, ..., Any], string: str, /) -> str:
        """Keep ``$help market x`` from confirming the hidden command exists."""
        if command.hidden:
            return self.command_not_found(command.qualified_name)
        return super().subcommand_not_found(command, string)


class FlyconomyBot(commands.Bot):
    """The Flyconomy Discord client.

    Attributes:
        settings: Validated runtime configuration.
        db: The open database. Available from :meth:`setup_hook` onward.
    """

    def __init__(self, settings: Settings) -> None:
        """Configure the client from ``settings`` without connecting yet."""
        super().__init__(
            command_prefix=commands.when_mentioned_or(settings.command_prefix),
            intents=build_intents(),
            help_command=_HelpCommand(no_category="Commands"),
            activity=discord.Game(name="the Flyconomy"),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.settings = settings
        self.db: Database
        # One budget per member across every game command. A per-command
        # cooldown would be sidestepped by rotating between commands.
        self.limiter = SlidingWindowLimiter(
            rate=settings.rate_limit_actions, per=settings.rate_limit_seconds
        )
        # Pure slash commands bypass on_command_error, so the tree needs the
        # same handler. Hybrid commands route through on_command_error.
        self.tree.on_error = self.on_app_command_error  # type: ignore[method-assign]
        self.after_invoke(self._report_command_duration)

    async def _report_command_duration(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """after_invoke hook: log a command that took a while to finish, then award badges.

        Every command is a chance to have reached a milestone -- a win, a
        robbery, a balance that crossed a line -- so checking here covers them
        all from one place, the same way errors are translated in one place.
        """
        _log_if_slow(ctx)
        await announce_achievements(self.db, ctx.author, ctx)

    # ------------------------------------------------------------ lifecycle --

    async def setup_hook(self) -> None:
        """Open the database, load extensions, and register slash commands.

        discord.py calls this once, after login but before the first gateway
        event, so commands are ready before any member can invoke them.
        """
        self.db = await Database.connect(self.settings.database_path)
        log.info("Database ready at %s", self.settings.database_path)

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.debug("Loaded extension %s", extension)
        log.info("Loaded %d extensions", len(EXTENSIONS))

        await self.sync_commands()

    async def sync_commands(self) -> int:
        """Publish slash commands to Discord.

        A guild sync applies immediately and is the right choice while
        developing. A global sync can take up to an hour to appear.

        Returns:
            The number of commands published.
        """
        if self.settings.dev_guild_id is not None:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally", len(synced))
        return len(synced)

    async def close(self) -> None:
        """Close the gateway connection and then the database."""
        try:
            await super().close()
        finally:
            db = getattr(self, "db", None)
            if db is not None:
                await db.close()
                log.info("Database closed")

    # --------------------------------------------------------------- events --

    async def on_ready(self) -> None:
        """Log a successful connection.

        Discord may fire this more than once if the gateway session resumes, so
        it must stay free of setup work.
        """
        log.info(
            "Connected as %s (id=%s) running Flyconomy %s in %d guild(s)",
            self.user,
            getattr(self.user, "id", "unknown"),
            __version__,
            len(self.guilds),
        )

    async def on_command_error(  # type: ignore[override]
        self, context: commands.Context[commands.Bot], exception: commands.CommandError
    ) -> None:
        """Report a command failure to the member and log the unexpected ones.

        Args:
            context: Invocation context.
            exception: The raised error.
        """
        _log_if_slow(context)
        if isinstance(exception, commands.NotOwner) and getattr(context.command, "hidden", False):
            # "Restricted to the owner" would confirm the command exists; stay
            # as silent as an unknown command does.
            return
        message = describe_command_error(exception)
        if message is None:
            log.error("Unhandled error in command %s", context.command, exc_info=exception)
            message = "Something went wrong running that command. The error has been logged."
        if not message:
            return

        # Context.send handles both a plain channel and an interaction response,
        # including one that has already been deferred.
        await context.send(embed=embeds.error_embed(message), ephemeral=True)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        """Report a slash command failure to the member.

        Args:
            interaction: The interaction that failed.
            error: The raised error.
        """
        message = describe_command_error(error)
        if message is None:
            log.error("Unhandled error in slash command %s", interaction.command, exc_info=error)
            message = "Something went wrong running that command. The error has been logged."
        if not message:
            return

        embed = embeds.error_embed(message)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


def describe_command_error(error: BaseException) -> str | None:
    """Turn an exception into a member-facing message.

    Args:
        error: The exception raised while running a command.

    Returns:
        A message to show the member, or ``None`` when the error is unexpected
        and the caller should log it and show a generic failure instead.
    """
    # A hybrid command invoked as a slash command wraps its exception twice:
    # app_commands.CommandInvokeError, then commands.HybridCommandError on top
    # of that. The classic prefix path only wraps once, in
    # commands.CommandInvokeError. All three expose the cause as `.original`.
    if isinstance(
        error,
        commands.CommandInvokeError
        | discord.app_commands.CommandInvokeError
        | commands.HybridCommandError,
    ):
        return describe_command_error(error.original)

    match error:
        case commands.CommandOnCooldown():
            return f"That command is on cooldown. Try again in {_humanize(error.retry_after)}."
        case discord.app_commands.CommandOnCooldown():
            return f"That command is on cooldown. Try again in {_humanize(error.retry_after)}."
        case commands.NotOwner():
            return "That command is restricted to the bot owner."
        case discord.app_commands.MissingPermissions() | commands.MissingPermissions():
            return "You do not have permission to use that command."
        case commands.MissingRequiredArgument():
            return f"You need to provide `{error.param.name}`."
        case commands.UserInputError() | discord.app_commands.TransformerError():
            # Every way a member can mistype a command: bad values, unclosed
            # quotes, extra arguments, or a slash command naming a member from
            # a DM, where there are no members to name.
            return f"That argument is not valid: {error}"
        case commands.CommandNotFound():
            # Chat noise that starts with the prefix is not worth a reply.
            return ""
        case RateLimitedError():
            return (
                f"You are using commands too quickly. Try again in {_humanize(error.retry_after)}."
            )
        case DailyBuyLimitError():
            if error.remaining:
                return (
                    f"You can only buy {error.limit:,} Flyxcoin a day, and you have "
                    f"{error.remaining:,} left today. Try again tomorrow for the rest."
                )
            return (
                f"You have bought your {error.limit:,} Flyxcoin for today. "
                "The limit resets at midnight."
            )
        case BetTooLargeError():
            return f"The table limit is {error.limit:,}. You cannot stake {error.bet:,} on one bet."
        case InsufficientFundsError():
            return (
                f"You have insufficient {error.currency}. "
                f"You need {error.requested:,} but only have {error.available:,}."
            )
        case FlyconomyError():
            return str(error)
        case OverflowError():
            # An amount past SQLite's 64-bit integers, which no balance can be.
            return "That amount is too large."
        case _:
            return None


_SECONDS_PER_MINUTE: Final = 60
_MINUTES_PER_HOUR: Final = 60


def _humanize(seconds: float) -> str:
    """Render a retry delay as a short phrase, such as ``2 minutes`` or ``1h 5m``."""
    total = max(1, round(seconds))
    if total < _SECONDS_PER_MINUTE:
        return f"{total} second{'s' if total != 1 else ''}"

    minutes = total // _SECONDS_PER_MINUTE
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    hours, leftover_minutes = divmod(minutes, _MINUTES_PER_HOUR)
    if leftover_minutes:
        return f"{hours}h {leftover_minutes}m"
    return f"{hours} hour{'s' if hours != 1 else ''}"
