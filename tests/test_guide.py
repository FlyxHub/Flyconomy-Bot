"""Tests for the published economy guide.

Two halves. The first is about the guide's text: that it splits into messages
Discord will accept, and that it still describes the economy the code actually
implements — a guide that lies about the odds is worse than no guide, and it
lies silently, so these are the only thing that catches it.

The second is about publishing: that an unchanged guide costs nothing, that a
changed section is edited rather than reposted, and that anything which would
leave the sections out of order reposts the whole thing instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from flyconomy import economy, guide
from flyconomy.cogs.base import BaseCog
from flyconomy.cogs.guide import Guide
from flyconomy.config import Settings
from flyconomy.database import Database, GuidePost
from tests.test_cog_behavior import FakeBot
from tests.test_commands import ALL_MEMBER_COMMANDS

#: The channel the fakes below publish to.
CHANNEL = 555_555_555_555_555_555


def _http_error(kind: type[discord.HTTPException], status: int, reason: str):
    """Build one of Discord's HTTP errors without a real response behind it."""
    return kind(SimpleNamespace(status=status, reason=reason), reason)


class FakeMessage:
    """A posted message the cog can edit or delete."""

    def __init__(self, channel: FakeChannel, message_id: int, content: str) -> None:
        self._channel = channel
        self.id = message_id
        self.content = content
        self.edits = 0

    async def edit(self, *, content: str) -> None:
        self.content = content
        self.edits += 1

    async def delete(self) -> None:
        if self._channel.refuse_deletes:
            raise _http_error(discord.Forbidden, 403, "Missing Permissions")
        await self._channel.drop(self.id)
        self._channel.deleted.append(self.id)


class FakeChannel(discord.abc.Messageable):
    """A channel that records what was sent, edited, and deleted.

    Subclasses the real ``Messageable`` so the ``isinstance`` check in
    ``BaseCog.resolve_channel`` sees it as something that can be posted to.

    Attributes:
        refuse_deletes: Makes every delete raise, standing in for a channel the
            bot can post to but not tidy up.
    """

    def __init__(self) -> None:
        self.messages: dict[int, FakeMessage] = {}
        self.order: list[int] = []
        self.deleted: list[int] = []
        self.refuse_deletes = False
        self._next_id = 1_000

    async def _get_channel(self) -> Any:  # pragma: no cover - never reached
        return self

    async def send(  # type: ignore[override]
        self, content: str | None = None, **_: Any
    ) -> FakeMessage:
        self._next_id += 1
        message = FakeMessage(self, self._next_id, content or "")
        self.messages[message.id] = message
        self.order.append(message.id)
        return message

    async def fetch_message(self, message_id: int, /) -> FakeMessage:  # type: ignore[override]
        message = self.messages.get(message_id)
        if message is None:
            raise _http_error(discord.NotFound, 404, "Unknown Message")
        return message

    async def drop(self, message_id: int) -> None:
        """Remove a message, the way somebody deleting it by hand would."""
        self.messages.pop(message_id, None)
        if message_id in self.order:
            self.order.remove(message_id)

    @property
    def contents(self) -> list[str]:
        """What the channel reads as, top to bottom."""
        return [self.messages[message_id].content for message_id in self.order]


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(tmp_path / "bot.db")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def settings() -> Settings:
    return Settings(discord_token="placeholder", guide_channel_id=CHANNEL)


def make_cog(db: Database, settings: Settings, channels: dict[int, FakeChannel]) -> Guide:
    """Build the guide cog without starting its startup publish.

    The task is what publishes on a real boot; the tests drive ``publish``
    directly so nothing is scheduled behind them.
    """
    cog = Guide.__new__(Guide)
    BaseCog.__init__(cog, FakeBot(db, settings, channels=channels))  # type: ignore[arg-type]
    return cog


@pytest.fixture
def cog(db: Database, settings: Settings, channel: FakeChannel) -> Guide:
    return make_cog(db, settings, {CHANNEL: channel})


class TestGuideSource:
    def test_the_file_splits_into_sections(self):
        assert len(guide.load_sections()) > 1

    def test_every_section_fits_in_one_discord_message(self):
        for index, section in enumerate(guide.load_sections()):
            assert len(section) <= guide.MESSAGE_LIMIT, (
                f"section {index + 1} is {len(section)} characters, "
                f"over Discord's {guide.MESSAGE_LIMIT} cap"
            )

    def test_the_maintainer_preamble_is_never_posted(self):
        # Everything above the first separator is a note to whoever edits the
        # file, so it must not reach the channel.
        assert "FLYCONOMY_GUIDE_CHANNEL_ID" in guide.read_guide_source()
        assert not any("FLYCONOMY_GUIDE_CHANNEL_ID" in s for s in guide.load_sections())

    def test_no_section_is_blank(self):
        assert all(section.strip() for section in guide.load_sections())

    def test_sections_are_split_on_the_separator_not_the_numbering(self):
        # The numbers in the separators are decorative, so inserting a section
        # must not require renumbering every one after it by hand.
        source = "note\n══ 7 of 9 ══\nfirst\n══ 3 of 4 ══\nsecond\n"
        assert guide.split_sections(source) == ("first", "second")

    def test_a_trailing_separator_adds_no_empty_message(self):
        assert guide.split_sections("note\n══ 1 of 1 ══\nonly\n══ 2 of 2 ══\n") == ("only",)

    def test_a_file_with_no_separators_publishes_nothing(self):
        assert guide.split_sections("just a note, never split") == ()

    def test_the_checksum_tracks_the_text(self):
        assert guide.checksum("same") == guide.checksum("same")
        assert guide.checksum("same") != guide.checksum("different")


class TestGuideStaysTrue:
    """The guide must describe the economy the code actually implements.

    Nothing else checks this. The guide is prose, so a retune that forgets it
    leaves the bot confidently telling members the wrong odds, and the only
    signal is a member noticing.
    """

    def test_every_member_command_is_documented(self):
        text = "\n".join(guide.load_sections())
        undocumented = sorted(name for name in ALL_MEMBER_COMMANDS if f"/{name}" not in text)
        assert not undocumented, f"the guide does not mention: {', '.join(undocumented)}"

    @pytest.mark.parametrize("level", sorted(economy.SECURITY_COST))
    def test_the_security_prices_match_the_rules(self, level):
        text = "\n".join(guide.load_sections())
        assert f"${economy.SECURITY_COST[level]:,}" in text

    @pytest.mark.parametrize("level", sorted(economy.UPGRADE_COST))
    def test_the_miner_prices_match_the_rules(self, level):
        text = "\n".join(guide.load_sections())
        assert f"${economy.UPGRADE_COST[level]:,}" in text

    def test_the_headline_figures_match_the_settings(self):
        text = "\n".join(guide.load_sections())
        settings = Settings(discord_token="placeholder")
        for figure in (
            settings.max_bet,
            settings.max_daily_payout,
            settings.lottery_ticket_price,
            economy.STARTING_BANK,
        ):
            assert f"${figure:,}" in text, f"the guide never mentions ${figure:,}"

    def test_the_transfer_tax_is_described(self):
        text = "\n".join(guide.load_sections())
        settings = Settings(discord_token="placeholder")
        assert f"{settings.transfer_tax_rate:.0%}" in text
        assert f"${economy.MIN_TRANSFER}" in text


class TestFirstPublish:
    async def test_the_whole_guide_is_posted_in_order(self, cog, channel, db):
        outcome = await cog.publish()

        sections = guide.load_sections()
        assert outcome.posted == len(sections)
        assert channel.contents == list(sections)
        assert len(await db.guide_posts()) == len(sections)

    async def test_the_posted_messages_are_recorded_with_their_checksums(self, cog, db):
        await cog.publish()

        for post, section in zip(await db.guide_posts(), guide.load_sections(), strict=True):
            assert post.checksum == guide.checksum(section)
            assert post.channel_id == CHANNEL

    async def test_nothing_is_published_without_a_channel_configured(self, db, channel):
        cog = make_cog(db, Settings(discord_token="placeholder"), {})

        outcome = await cog.publish()

        assert "GUIDE_CHANNEL_ID" in (outcome.problem or "")
        assert channel.contents == []

    async def test_an_unreachable_channel_is_reported_not_raised(self, db, settings):
        # get_channel misses and there is no gateway to fetch through, which is
        # what a deleted channel or a missing permission looks like.
        cog = make_cog(db, settings, {})

        outcome = await cog.publish()

        assert outcome.problem is not None
        assert outcome.posted == 0


class TestRepublish:
    async def test_an_unchanged_guide_costs_no_api_calls(self, cog, channel):
        await cog.publish()
        before = dict(channel.messages)

        outcome = await cog.publish()

        assert outcome.unchanged == len(guide.load_sections())
        assert outcome.edited == 0
        assert outcome.posted == 0
        assert not outcome.changed
        assert all(message.edits == 0 for message in before.values())

    async def test_a_changed_section_is_edited_not_reposted(self, cog, channel, db, monkeypatch):
        await cog.publish()
        original_ids = list(channel.order)

        sections = list(guide.load_sections())
        sections[1] = sections[1] + "\n\nA new line."
        monkeypatch.setattr(guide, "load_sections", lambda: tuple(sections))

        outcome = await cog.publish()

        assert (outcome.edited, outcome.posted) == (1, 0)
        assert outcome.unchanged == len(sections) - 1
        # Same messages, same order: nobody's link to the guide broke.
        assert channel.order == original_ids
        assert channel.contents[1] == sections[1]

    async def test_an_edit_updates_the_stored_checksum(self, cog, db, monkeypatch):
        await cog.publish()

        sections = list(guide.load_sections())
        sections[0] = "Rewritten."
        monkeypatch.setattr(guide, "load_sections", lambda: tuple(sections))
        await cog.publish()

        assert (await db.guide_posts())[0].checksum == guide.checksum("Rewritten.")
        # And a second run now finds it current rather than editing it again.
        assert (await cog.publish()).edited == 0

    async def test_an_added_section_reposts_so_the_order_holds(self, cog, channel, monkeypatch):
        await cog.publish()

        sections = (*guide.load_sections(), "A brand new final section.")
        monkeypatch.setattr(guide, "load_sections", lambda: sections)

        outcome = await cog.publish()

        assert outcome.posted == len(sections)
        assert outcome.edited == 0
        assert channel.contents == list(sections)

    async def test_a_removed_section_leaves_no_orphan_message(self, cog, channel, db, monkeypatch):
        await cog.publish()

        sections = guide.load_sections()[:-1]
        monkeypatch.setattr(guide, "load_sections", lambda: sections)

        await cog.publish()

        assert channel.contents == list(sections)
        assert len(await db.guide_posts()) == len(sections)

    async def test_a_deleted_message_reposts_rather_than_appending(
        self, cog, channel, db, monkeypatch
    ):
        # Replacing just the missing message would put it at the bottom of the
        # channel, so the guide would read out of order from then on.
        await cog.publish()
        await channel.drop(channel.order[1])

        sections = list(guide.load_sections())
        sections[1] = sections[1] + "\n\nEdited while the message was missing."
        monkeypatch.setattr(guide, "load_sections", lambda: tuple(sections))

        outcome = await cog.publish()

        assert outcome.posted == len(sections)
        assert channel.contents == sections

    async def test_changing_the_channel_republishes_there(self, db, channel, monkeypatch):
        settings = Settings(discord_token="placeholder", guide_channel_id=CHANNEL)
        moved = FakeChannel()
        cog = make_cog(db, settings, {CHANNEL: channel, CHANNEL + 1: moved})
        bot = cog.bot
        await cog.publish()

        bot.settings = Settings(discord_token="placeholder", guide_channel_id=CHANNEL + 1)
        outcome = await cog.publish()

        assert outcome.posted == len(guide.load_sections())
        assert moved.contents == list(guide.load_sections())
        assert all(post.channel_id == CHANNEL + 1 for post in await db.guide_posts())

    async def test_repost_moves_the_guide_to_the_bottom(self, cog, channel, db):
        await cog.publish()
        first_ids = list(channel.order)

        outcome = await cog.publish(repost=True)

        assert outcome.posted == len(guide.load_sections())
        assert outcome.removed == len(first_ids)
        assert not set(channel.order) & set(first_ids)
        assert channel.contents == list(guide.load_sections())

    async def test_a_repost_that_cannot_delete_the_old_copy_still_posts(self, cog, channel):
        # The old messages are left alone if Discord refuses to delete them,
        # rather than the new copy never going up.
        await cog.publish()
        channel.refuse_deletes = True

        outcome = await cog.publish(repost=True)

        assert outcome.posted == len(guide.load_sections())
        assert outcome.removed == 0


class TestRecordedPosts:
    async def test_posts_come_back_in_channel_order(self, db):
        await db.replace_guide_posts(
            [
                GuidePost(position=1, channel_id=CHANNEL, message_id=20, checksum="b"),
                GuidePost(position=0, channel_id=CHANNEL, message_id=10, checksum="a"),
            ]
        )

        assert [post.position for post in await db.guide_posts()] == [0, 1]

    async def test_recording_a_position_twice_replaces_it(self, db):
        await db.record_guide_post(GuidePost(0, CHANNEL, 10, "a"))
        await db.record_guide_post(GuidePost(0, CHANNEL, 11, "b"))

        posts = await db.guide_posts()
        assert len(posts) == 1
        assert (posts[0].message_id, posts[0].checksum) == (11, "b")

    async def test_replacing_forgets_everything_that_came_before(self, db):
        await db.replace_guide_posts([GuidePost(i, CHANNEL, 10 + i, "a") for i in range(6)])
        await db.replace_guide_posts([GuidePost(0, CHANNEL, 99, "z")])

        posts = await db.guide_posts()
        assert len(posts) == 1
        assert posts[0].message_id == 99

    async def test_clearing_leaves_nothing_tracked(self, db):
        await db.replace_guide_posts([GuidePost(0, CHANNEL, 10, "a")])
        await db.clear_guide_posts()

        assert await db.guide_posts() == ()
