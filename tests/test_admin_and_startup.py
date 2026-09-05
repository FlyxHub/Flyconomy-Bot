"""Tests for the owner-only commands, logging setup, and process exit codes."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import discord
import pytest
from discord.ext import commands
from pydantic import ValidationError

from flyconomy import __main__ as entrypoint
from flyconomy import economy
from flyconomy.cogs.admin import Admin
from flyconomy.cogs.guide import GuideOutcome
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.logging_config import configure_logging
from tests.conftest import ALICE, BOB
from tests.test_cog_behavior import FakeContext, FakeUser


class FakeAdminBot:
    """The bot surface the admin cog reads."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        owner: bool = True,
        cogs: dict[str, object] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self._owner = owner
        self.synced = 0
        self._cogs = cogs or {}

    def get_cog(self, name: str) -> object | None:
        return self._cogs.get(name)

    async def is_owner(self, _user: object) -> bool:
        return self._owner

    async def sync_commands(self) -> int:
        self.synced += 1
        return 19


class TypingContext(FakeContext):
    """A context that also supports `async with ctx.typing()`."""

    @asynccontextmanager
    async def typing(self) -> AsyncIterator[None]:
        yield


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(tmp_path / "bot.db")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(discord_token="placeholder")


@pytest.fixture
def ctx() -> TypingContext:
    return TypingContext(author=FakeUser(id=ALICE))


class TestOwnerGate:
    async def test_the_owner_passes_the_check(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings, owner=True))
        assert await cog.cog_check(ctx) is True

    async def test_a_non_owner_is_rejected(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings, owner=False))
        with pytest.raises(commands.NotOwner):
            await cog.cog_check(ctx)


class TestAdminCommands:
    async def test_adminme_grants_the_admin_miner(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))

        await cog.adminme.callback(cog, ctx)

        assert (await db.get_account(ALICE)).miner == economy.ADMIN_MINER_LEVEL

    async def test_adminmine_mints_coins(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))

        await cog.adminmine.callback(cog, ctx, 5)

        assert (await db.get_account(ALICE)).crypto == 5

    async def test_adminmine_can_remove_coins(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))
        await db.add_crypto(ALICE, 5)

        await cog.adminmine.callback(cog, ctx, -3)

        assert (await db.get_account(ALICE)).crypto == 2

    async def test_reset_deletes_an_account(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))
        await db.add_wallet(BOB, 5_000)

        await cog.reset.callback(cog, ctx, FakeUser(id=BOB))

        assert await db.find_account(BOB) is None
        assert "reset" in ctx.last.lower()

    async def test_resetting_an_unknown_member_says_so(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))

        await cog.reset.callback(cog, ctx, FakeUser(id=BOB))

        assert "does not have an account" in ctx.last.lower()

    async def test_purge_deletes_by_raw_id(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))
        await db.add_wallet(BOB, 5_000)

        await cog.purge.callback(cog, ctx, str(BOB))

        assert await db.find_account(BOB) is None
        assert "their account" in ctx.last

    async def test_purge_accepts_a_mention(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))
        await db.add_wallet(BOB, 5_000)

        await cog.purge.callback(cog, ctx, f"<@{BOB}>")

        assert await db.find_account(BOB) is None

    async def test_purge_deletes_an_id_no_member_converter_could_resolve(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))
        await db.add_wallet(1, 5)

        await cog.purge.callback(cog, ctx, "<@1>")

        assert await db.find_account(1) is None
        assert "1" in ctx.last

    async def test_purge_reports_a_lottery_entry_it_removed(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))
        await db.add_bank(BOB, 5_000)
        await db.enter_lottery(BOB, 1_000)

        await cog.purge.callback(cog, ctx, str(BOB))

        assert "1 lottery entry" in ctx.last
        assert await db.lottery_entrants() == []

    async def test_purge_says_when_an_id_is_not_in_the_database(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))

        await cog.purge.callback(cog, ctx, str(BOB))

        assert "not in the database" in ctx.last

    async def test_purge_rejects_an_argument_that_is_not_an_id(self, db, settings, ctx):
        cog = Admin(FakeAdminBot(db, settings))

        with pytest.raises(commands.BadArgument):
            await cog.purge.callback(cog, ctx, "nobody")

    async def test_sync_republishes_and_reports_the_scope(self, db, settings, ctx):
        bot = FakeAdminBot(db, settings)
        cog = Admin(bot)

        await cog.sync.callback(cog, ctx)

        assert bot.synced == 1
        assert "19" in ctx.last
        assert "globally" in ctx.last

    async def test_sync_names_the_dev_guild_when_one_is_set(self, db, ctx):
        configured = Settings(discord_token="placeholder", dev_guild_id=1234)
        cog = Admin(FakeAdminBot(db, configured))

        await cog.sync.callback(cog, ctx)

        assert "1234" in ctx.last


class FakeGuideCog:
    """Stands in for the guide cog the `$guide` command drives."""

    def __init__(self, outcome: GuideOutcome) -> None:
        self.outcome = outcome
        self.calls: list[bool] = []

    async def publish(self, *, repost: bool = False) -> GuideOutcome:
        self.calls.append(repost)
        return self.outcome


class TestGuideCommand:
    @staticmethod
    def _cog(db, settings, outcome: GuideOutcome) -> tuple[Admin, FakeGuideCog]:
        guide_cog = FakeGuideCog(outcome)
        return Admin(FakeAdminBot(db, settings, cogs={"Guide": guide_cog})), guide_cog

    async def test_a_bare_call_edits_rather_than_reposts(self, db, settings, ctx):
        admin, guide_cog = self._cog(db, settings, GuideOutcome(edited=2, unchanged=4))

        await admin.guide.callback(admin, ctx, None)

        assert guide_cog.calls == [False]
        assert "2 edited" in ctx.last
        assert "4 unchanged" in ctx.last

    async def test_repost_asks_for_a_repost(self, db, settings, ctx):
        admin, guide_cog = self._cog(db, settings, GuideOutcome(posted=6, removed=6))

        await admin.guide.callback(admin, ctx, "repost")

        assert guide_cog.calls == [True]
        assert "6 posted" in ctx.last

    async def test_the_action_is_case_insensitive(self, db, settings, ctx):
        admin, guide_cog = self._cog(db, settings, GuideOutcome(posted=6))

        await admin.guide.callback(admin, ctx, "REPOST")

        assert guide_cog.calls == [True]

    async def test_an_unknown_action_is_refused_rather_than_treated_as_bare(
        self, db, settings, ctx
    ):
        # A typo must not quietly do the milder thing and report success.
        admin, guide_cog = self._cog(db, settings, GuideOutcome())

        with pytest.raises(commands.BadArgument):
            await admin.guide.callback(admin, ctx, "repsot")

        assert guide_cog.calls == []

    async def test_an_unchanged_guide_says_so(self, db, settings, ctx):
        admin, _ = self._cog(db, settings, GuideOutcome(unchanged=6))

        await admin.guide.callback(admin, ctx, None)

        assert "already current" in ctx.last

    async def test_a_problem_is_reported_to_the_owner(self, db, settings, ctx):
        admin, _ = self._cog(db, settings, GuideOutcome(problem="the channel is on fire"))

        await admin.guide.callback(admin, ctx, None)

        assert "the channel is on fire" in ctx.last


class TestLogging:
    def test_configuring_logging_sets_the_level_and_one_handler(self):
        configure_logging("DEBUG")
        root = logging.getLogger()
        try:
            assert root.level == logging.DEBUG
            assert len(root.handlers) == 1
        finally:
            configure_logging("INFO")

    def test_reconfiguring_does_not_stack_handlers(self):
        configure_logging("INFO")
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == 1

    def test_the_discord_gateway_log_stays_quiet_at_debug(self):
        configure_logging("DEBUG")
        try:
            assert logging.getLogger("discord").level >= logging.INFO
        finally:
            configure_logging("INFO")

    def test_the_http_log_is_capped_until_the_root_level_asks_for_debug(self):
        # discord.py logs its proactive rate-limit waits on this logger, which
        # is noise at INFO but the whole point of turning DEBUG on.
        configure_logging("INFO")
        assert logging.getLogger("discord.http").level == logging.WARNING

        configure_logging("DEBUG")
        try:
            assert logging.getLogger("discord.http").level == logging.DEBUG
        finally:
            configure_logging("INFO")


def _stub_asyncio_run(monkeypatch, raises: BaseException | None = None) -> None:
    """Replace asyncio.run so main() never opens a gateway connection.

    The coroutine main() built is closed rather than dropped, so Python does not
    report it as never awaited.
    """

    def fake_run(coro: Any) -> None:
        coro.close()
        if raises is not None:
            raise raises

    monkeypatch.setattr(entrypoint.asyncio, "run", fake_run)


class TestExitCodes:
    def test_invalid_configuration_exits_2(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLYCONOMY_DISCORD_TOKEN", raising=False)
        assert entrypoint.main() == 2

    def test_a_rejected_token_exits_3(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", "placeholder")
        _stub_asyncio_run(monkeypatch, discord.LoginFailure())
        assert entrypoint.main() == 3

    def test_missing_intents_exit_2(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", "placeholder")
        _stub_asyncio_run(monkeypatch, discord.PrivilegedIntentsRequired(shard_id=None))
        assert entrypoint.main() == 2

    def test_a_clean_shutdown_exits_0(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", "placeholder")
        _stub_asyncio_run(monkeypatch)
        assert entrypoint.main() == 0

    def test_an_interrupt_exits_0(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FLYCONOMY_DISCORD_TOKEN", "placeholder")
        _stub_asyncio_run(monkeypatch, KeyboardInterrupt())
        assert entrypoint.main() == 0

    def test_the_settings_error_is_reported_not_raised(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLYCONOMY_DISCORD_TOKEN", raising=False)
        # main() must translate the error, never let it escape to a traceback.
        try:
            code = entrypoint.main()
        except ValidationError:  # pragma: no cover - the failure this guards
            pytest.fail("main() leaked a ValidationError instead of exiting cleanly")
        assert code == 2
