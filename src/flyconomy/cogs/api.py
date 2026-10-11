"""A small JSON API for the owner's dashboard, served from inside the bot.

It lives in the bot's process rather than beside it because the database does:
SQLite is not safe to share over a network, and a second process opening the
same file would be a second writer the migrations and transactions know nothing
about. The dashboard itself runs elsewhere -- ``python -m flyconomy.dashboard``
on the owner's own machine -- and talks to this over HTTP.

Everything here is owner-only, enforced by one bearer token rather than by
Discord: the API reports the market's regime, which nothing member-facing may
name, and can arm a run. Without ``api_token`` set the server never starts.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Final

from aiohttp import web
from aiohttp.typedefs import Handler

from flyconomy import economy
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.admin import steer_market
from flyconomy.cogs.base import BaseCog
from flyconomy.database import LeaderboardEntry

log = logging.getLogger(__name__)

#: Where the API listens. Fixed rather than a setting because ``compose.yaml``
#: maps it, and the dashboard's default URL names it.
API_PORT: Final = 8765

#: The longest history one request may ask for. A season of hourly snapshots
#: across a server's worth of members is far more than a chart can show.
_MAX_HISTORY_HOURS: Final = 24 * 31


class Api(BaseCog, name="Api"):
    """Serves the dashboard API while ``api_token`` is set."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog. The server starts in :meth:`cog_load`."""
        super().__init__(bot)
        self._runner: web.AppRunner | None = None

    async def cog_load(self) -> None:
        """Start listening, unless the API is switched off.

        A port that cannot be bound is logged rather than raised: the dashboard
        is a convenience, and it must never be the reason the bot is down.
        """
        if self.settings.api_token is None:
            return
        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.settings.api_host, API_PORT).start()
        except OSError:
            log.exception("Dashboard API could not listen on port %d", API_PORT)
            await runner.cleanup()
            return
        self._runner = runner
        log.info("Dashboard API listening on %s:%d", self.settings.api_host, API_PORT)

    async def cog_unload(self) -> None:
        """Stop listening."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def make_app(self) -> web.Application:
        """Build the application. Separate from :meth:`cog_load` so tests can serve it."""
        token = self.settings.api_token
        assert token is not None  # noqa: S101 - cog_load returns early otherwise
        expected = f"Bearer {token.get_secret_value()}".encode()

        @web.middleware
        async def require_token(request: web.Request, handler: Handler) -> web.StreamResponse:
            given = request.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                raise web.HTTPUnauthorized
            return await handler(request)

        app = web.Application(middlewares=[require_token])
        app.router.add_get("/api/overview", self.overview)
        app.router.add_get("/api/history", self.history)
        app.router.add_post("/api/market", self.market)
        return app

    def _name(self, user_id: int) -> str:
        """A member's display name, or their id when the bot cannot see them."""
        user = self.bot.get_user(user_id)
        return user.display_name if user is not None else str(user_id)

    def _board(self, entries: list[LeaderboardEntry]) -> list[dict[str, object]]:
        # Ids as strings: snowflakes are past JavaScript's safe integers.
        return [
            {"id": str(e.user_id), "name": self._name(e.user_id), "amount": e.amount}
            for e in entries
        ]

    async def overview(self, _request: web.Request) -> web.Response:
        """The market, the lottery, and the leaderboards as they stand."""
        market = await self.db.get_market()
        lottery = await self.db.lottery_state()
        return web.json_response(
            {
                "market": {
                    "price": market.price,
                    "regime": market.regime,
                    "minutes_left": market.ticks_left * economy.FLX_TICK_MINUTES,
                    # The band the walk lives in, so the dashboard draws the
                    # bot's own bounds rather than a copy of them.
                    "floor": economy.FLX_PRICE_FLOOR,
                    "anchor": economy.FLX_PRICE,
                    "ceiling": economy.FLX_PRICE_CEILING,
                    "calm_band_percent": economy.FLX_CALM_BAND_PERCENT,
                },
                "lottery_pot": lottery.pot,
                "circulating": await self.db.total_crypto(),
                "leaderboards": {
                    "net_worth": self._board(await self.db.top_net_worth()),
                    "wallet": self._board(await self.db.top_wallets()),
                    "crypto": self._board(await self.db.top_crypto(economy.LEADERBOARD_SIZE)),
                },
            }
        )

    async def history(self, request: web.Request) -> web.Response:
        """The price and every member's net worth over the last ``hours``."""
        try:
            hours = int(request.query.get("hours", "24"))
        except ValueError:
            raise web.HTTPBadRequest(text="hours must be a whole number") from None
        hours = max(1, min(hours, _MAX_HISTORY_HOURS))
        since = int(time.time()) - hours * 3600

        series: dict[int, list[list[int]]] = {}
        for point in await self.db.net_worth_history(since):
            series.setdefault(point.user_id, []).append([point.at, point.net_worth])
        return web.json_response(
            {
                "prices": [list(p) for p in await self.db.price_history(since)],
                "members": [
                    {"id": str(user_id), "name": self._name(user_id), "points": points}
                    for user_id, points in series.items()
                ],
            }
        )

    async def market(self, request: web.Request) -> web.Response:
        """Arm a run or call one off, exactly as ``$market`` would."""
        try:
            body = await request.json()
            wanted = economy.parse_market_target(str(body["direction"]))
        except (ValueError, KeyError, TypeError):
            wanted = None
        if wanted is None:
            raise web.HTTPBadRequest(text="direction must be bull, bear, or neutral")
        message = await steer_market(self.db, wanted, self.rng)
        log.info("Dashboard steered the market: %s", message)
        return web.json_response({"message": message})


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Api(bot))
