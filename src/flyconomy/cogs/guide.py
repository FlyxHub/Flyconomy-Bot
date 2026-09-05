"""Publishes the member-facing economy guide, and keeps it current.

The guide used to be a file somebody pasted into a channel by hand, which meant
it was accurate exactly until the next time the economy was retuned. This posts
it on startup and edits those same messages in place whenever the text changes,
so a deploy is the only step: the channel is never a stale copy of the file.

Two rules shape the whole cog. **Unchanged sections cost nothing** -- each
posted message is stored with a checksum, so a restart that finds the guide
already correct makes no API calls at all, and a restart loop cannot rewrite
the channel over and over. **Order is never sacrificed to save an edit** -- a
new message always lands at the bottom of a channel, so anything that would
leave the sections out of order reposts the guide whole instead of patching it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord.ext import tasks

from flyconomy import guide
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog
from flyconomy.database import GuidePost

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GuideOutcome:
    """What one publish run did.

    Attributes:
        posted: Sections sent as new messages.
        edited: Existing messages rewritten in place.
        unchanged: Sections that already matched, which cost no API call.
        removed: Messages deleted, either stale extras or the old copy that a
            repost replaced.
        problem: Why nothing was published, or ``None`` on a normal run.
    """

    posted: int = 0
    edited: int = 0
    unchanged: int = 0
    removed: int = 0
    problem: str | None = None

    @property
    def changed(self) -> bool:
        """Whether the channel was touched at all."""
        return bool(self.posted or self.edited or self.removed)


class Guide(BaseCog, name="Guide"):
    """Keeps the configured channel holding a current copy of the guide."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog and schedule the startup publish."""
        super().__init__(bot)
        self.publish_loop.start()

    async def cog_unload(self) -> None:
        """Cancel the startup publish if the extension is unloaded first."""
        self.publish_loop.cancel()

    @tasks.loop(count=1)
    async def publish_loop(self) -> None:
        """Publish the guide once, as soon as the bot is connected."""
        try:
            outcome = await self.publish()
        except Exception:
            # A guide that fails to publish must not take the bot down with it;
            # every other command still works without it.
            log.exception("Publishing the economy guide failed")
            return

        if outcome.problem:
            log.info("Economy guide not published: %s", outcome.problem)
        elif outcome.changed:
            log.info(
                "Economy guide published: %d posted, %d edited, %d unchanged, %d removed",
                outcome.posted,
                outcome.edited,
                outcome.unchanged,
                outcome.removed,
            )
        else:
            log.info("Economy guide is already current across %d messages", outcome.unchanged)

    @publish_loop.before_loop
    async def _before_publish_loop(self) -> None:
        """Wait until the bot is connected before publishing.

        A client that never logged in has no gateway and no channel to post to,
        which is the case in unit tests and when startup failed, so the task
        stops rather than publishing into the void.
        """
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug("No gateway connection; the economy guide will not be published")
            self.publish_loop.cancel()

    async def publish(self, *, repost: bool = False) -> GuideOutcome:
        """Bring the configured channel in line with the packaged guide.

        Args:
            repost: Delete and re-send every section instead of editing it.
                Moves the guide to the bottom of the channel, which is the
                point of asking for it.

        Returns:
            What the run did, or an outcome carrying ``problem`` if it could
            not run at all.
        """
        channel_id = self.settings.guide_channel_id
        if channel_id is None:
            return GuideOutcome(problem="FLYCONOMY_GUIDE_CHANNEL_ID is not set")

        sections = guide.load_sections()
        if not sections:
            return GuideOutcome(problem="the guide file has no sections")

        channel = await self.resolve_channel(channel_id)
        if channel is None:
            return GuideOutcome(problem=f"channel {channel_id} cannot be posted to")

        stored = await self.db.guide_posts()
        if repost or not self._is_reusable(stored, sections, channel_id):
            return await self._repost(channel, channel_id, sections, stored)
        return await self._edit_in_place(channel, channel_id, sections, stored)

    @staticmethod
    def _is_reusable(
        stored: tuple[GuidePost, ...], sections: tuple[str, ...], channel_id: int
    ) -> bool:
        """Return whether the recorded messages can be edited rather than replaced.

        They are reusable only when they cover exactly the sections that exist
        now, at the positions those sections sit at, in the channel now
        configured. A section added, one removed, or a channel changed all mean
        that editing toward a match would leave the guide out of order, and
        only a repost fixes that.
        """
        return len(stored) == len(sections) and all(
            post.position == position and post.channel_id == channel_id
            for position, post in enumerate(stored)
        )

    async def _edit_in_place(
        self,
        channel: discord.abc.Messageable,
        channel_id: int,
        sections: tuple[str, ...],
        stored: tuple[GuidePost, ...],
    ) -> GuideOutcome:
        """Rewrite only the messages whose section changed.

        A message that has gone missing sends the whole guide back through
        :meth:`_repost`: replacing just that one would append it to the bottom
        of the channel, leaving the guide readable only out of order.
        """
        edited = unchanged = 0
        for post, section in zip(stored, sections, strict=True):
            digest = guide.checksum(section)
            if digest == post.checksum:
                unchanged += 1
                continue

            try:
                message = await channel.fetch_message(post.message_id)
                await message.edit(content=section)
            except discord.NotFound:
                log.info(
                    "Guide message %d is gone; reposting the guide to keep it in order",
                    post.message_id,
                )
                return await self._repost(channel, channel_id, sections, stored)

            await self.db.record_guide_post(
                GuidePost(
                    position=post.position,
                    channel_id=channel_id,
                    message_id=post.message_id,
                    checksum=digest,
                )
            )
            edited += 1

        return GuideOutcome(edited=edited, unchanged=unchanged)

    async def _repost(
        self,
        channel: discord.abc.Messageable,
        channel_id: int,
        sections: tuple[str, ...],
        stored: tuple[GuidePost, ...],
    ) -> GuideOutcome:
        """Delete every tracked message and send the guide again, in order.

        The old rows are cleared before the new messages are sent, so a failure
        partway through leaves the bot tracking nothing rather than tracking
        messages it has already deleted.
        """
        removed = await self._delete_tracked(channel, stored)
        await self.db.clear_guide_posts()

        posts = []
        for position, section in enumerate(sections):
            message = await channel.send(section)
            posts.append(
                GuidePost(
                    position=position,
                    channel_id=channel_id,
                    message_id=message.id,
                    checksum=guide.checksum(section),
                )
            )

        await self.db.replace_guide_posts(posts)
        return GuideOutcome(posted=len(posts), removed=removed)

    @staticmethod
    async def _delete_tracked(
        channel: discord.abc.Messageable, stored: tuple[GuidePost, ...]
    ) -> int:
        """Delete the messages the bot posted last time, best effort.

        A message somebody already deleted by hand is the expected case rather
        than an error, so a failure here never stops the new copy going up.
        """
        removed = 0
        for post in stored:
            try:
                message = await channel.fetch_message(post.message_id)
                await message.delete()
            except discord.HTTPException:
                log.debug("Could not delete old guide message %d", post.message_id)
                continue
            removed += 1
        return removed


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Guide(bot))
