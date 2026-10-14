"""The owner's dashboard: one page and the JSON it reads, served from inside the bot.

It lives in the bot's process rather than beside it because the database does:
SQLite is not safe to share over a network, and a second process opening the
same file would be a second writer the migrations and transactions know nothing
about. The page and its API share one origin, so there is nothing to proxy.

Off unless ``dashboard_enabled`` is set. It is for the owner only: it names the
market's regime, which nothing member-facing may, and it can arm a run.
"""

from __future__ import annotations

import ipaddress
import itertools
import logging
import math
import time
from collections.abc import Sequence
from importlib import resources
from typing import Final

from aiohttp import web
from aiohttp.typedefs import Handler

from flyconomy import economy, mines
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.admin import steer_market
from flyconomy.cogs.base import BaseCog
from flyconomy.database import LeaderboardEntry

log = logging.getLogger(__name__)

#: Where the dashboard listens. Fixed rather than a setting because
#: ``compose.yaml`` maps it.
PORT: Final = 8765

#: The page, shipped as package data beside the member guide.
PAGE_RESOURCE: Final = "data/dashboard.html"

#: The slice of members whose share of the money the dashboard tracks.
TOP_FRACTION: Final = 0.10

#: The most members the overview lists by name. Enough for every member of a
#: community server; the cap only stops a huge one bloating a five-second poll.
MEMBERS_SHOWN: Final = 300

#: The longest history one request may ask for. A season of hourly snapshots
#: across a server's worth of members is far more than a chart can show.
_MAX_HISTORY_HOURS: Final = 24 * 31


def top_count(accounts: int, fraction: float = TOP_FRACTION) -> int:
    """How many accounts make up the top ``fraction``: rounded up, and never none."""
    return max(1, math.ceil(accounts * fraction))


def top_share(worths: Sequence[int], fraction: float = TOP_FRACTION) -> float:
    """Return the percentage of all money held by the richest ``fraction`` of accounts.

    The slice is rounded up and never empty, so a server of five still has a
    "richest 10%": its one richest member. Even shares give back ``fraction``
    itself as a percentage, which is the reference line the chart draws.

    Args:
        worths: Every account's net worth at one moment.
        fraction: How much of the population counts as the top.

    Returns:
        A percentage from 0 to 100, or 0 when there is no money at all.
    """
    total = sum(worths)
    if total <= 0:
        return 0.0
    return 100 * sum(sorted(worths, reverse=True)[: top_count(len(worths), fraction)]) / total


class Api(BaseCog, name="Api"):
    """Serves the dashboard while ``dashboard_enabled`` is set."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog. The server starts in :meth:`cog_load`."""
        super().__init__(bot)
        self._runner: web.AppRunner | None = None

    async def cog_load(self) -> None:
        """Start listening, unless the dashboard is switched off.

        A port that cannot be bound is logged rather than raised: the dashboard
        is a convenience, and it must never be the reason the bot is down.
        """
        if not self.settings.dashboard_enabled:
            return
        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.settings.api_host, PORT).start()
        except OSError:
            log.exception("Dashboard could not listen on port %d", PORT)
            await runner.cleanup()
            return
        self._runner = runner
        log.info("Dashboard listening on %s:%d", self.settings.api_host, PORT)

    async def cog_unload(self) -> None:
        """Stop listening."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def make_app(self) -> web.Application:
        """Build the application. Separate from :meth:`cog_load` so tests can serve it."""
        page = (resources.files("flyconomy") / PAGE_RESOURCE).read_text(encoding="utf-8")

        async def index(_request: web.Request) -> web.Response:
            return web.Response(text=page, content_type="text/html")

        # ponytail: no login, on the owner's word that only they use their
        # network. Anything that can reach the port can steer the market, so
        # add HTTP Basic auth here before the network is shared or the port is
        # forwarded.
        app = web.Application(middlewares=[_local_only])
        app.router.add_get("/", index)
        app.router.add_get("/api/overview", self.overview)
        app.router.add_get("/api/history", self.history)
        app.router.add_post("/api/market", self.market)
        return app

    def _next_run(self, cog_name: str, loop_name: str) -> float | None:
        """When one of the bot's scheduled jobs next runs, as Unix time.

        Read from the job's own loop rather than worked out again from the
        settings, so a countdown can only ever show the time the bot will
        actually act. ``None`` when the job is not running.
        """
        loop = getattr(self.bot.get_cog(cog_name), loop_name, None)
        when = getattr(loop, "next_iteration", None)
        return when.timestamp() if when is not None else None

    def _name(self, user_id: int) -> str:
        """A member's display name, or their id when the bot cannot see them."""
        user = self.bot.get_user(user_id)
        return user.display_name if user is not None else str(user_id)

    def _live_mines(self) -> list[dict[str, object]]:
        """Every mines board in play, mines and all, oldest first.

        This is the whole answer to the round, which is why it is only ever
        served here: anyone who sees it plays a solved board.
        """
        live = getattr(self.bot.get_cog("Casino"), "live_mines", ())
        # A board's message id is a snowflake, so it orders boards by start.
        boards = sorted(
            (v.message.id, v) for v in live if v.message is not None and not v.is_finished()
        )
        return [
            {
                "id": str(message_id),
                "player": self._name(v.player.id),
                "stake": v.game.stake,
                "mines": sorted(v.game.mine_tiles),
                "revealed": v.game.revealed,
                "multiplier": v.game.multiplier,
                "next_multiplier": v.game.next_multiplier,
            }
            for message_id, v in boards
        ]

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
        # The live end of the hourly charts, valued at the same price as the
        # rest of this response so the page never disagrees with itself.
        supply = await self.db.current_supply(int(time.time()), market.price)
        worths = await self.db.net_worths(market.price)
        members = await self.db.member_balances(market.price, MEMBERS_SHOWN)
        return web.json_response(
            {
                "market": {
                    "price": market.price,
                    "regime": market.regime,
                    "ticks_left": market.ticks_left,
                    # The band the walk lives in, so the dashboard draws the
                    # bot's own bounds rather than a copy of them.
                    "floor": economy.FLX_PRICE_FLOOR,
                    "anchor": economy.FLX_PRICE,
                    "ceiling": economy.FLX_PRICE_CEILING,
                    "calm_band_percent": economy.FLX_CALM_BAND_PERCENT,
                },
                "lottery_pot": lottery.pot,
                "economy": {
                    "at": supply.at,
                    "banks": supply.banks,
                    "wallets": supply.wallets,
                    "coins": supply.coins,
                    "top_share": round(top_share(worths), 2),
                    "top_count": top_count(len(worths)),
                    "accounts": len(worths),
                },
                # [id, name, cash, coins]: cash and coins apart, so the page
                # can tell members acting from the price moving under them.
                "members": [
                    [str(m.user_id), self._name(m.user_id), m.cash, m.coins] for m in members
                ],
                "schedule": {
                    "tick": {
                        "at": self._next_run("Market", "tick_loop"),
                        "every": economy.FLX_TICK_MINUTES * 60,
                    },
                    "interest": {"at": self._next_run("Economy", "interest_loop"), "every": 86_400},
                    "lottery": {"at": self._next_run("Lottery", "draw_loop"), "every": 86_400},
                },
                "mines": {
                    "columns": mines.COLUMNS,
                    "tiles": mines.TILES,
                    "games": self._live_mines(),
                },
                "circulating": await self.db.total_crypto(),
                "leaderboards": {
                    "net_worth": self._board(await self.db.top_net_worth()),
                    "wallet": self._board(await self.db.top_wallets()),
                    "crypto": self._board(await self.db.top_crypto(economy.LEADERBOARD_SIZE)),
                },
            }
        )

    async def history(self, request: web.Request) -> web.Response:
        """The price, every member's net worth, and the economy's totals over the last ``hours``."""
        try:
            hours = int(request.query.get("hours", "24"))
        except ValueError:
            raise web.HTTPBadRequest(text="hours must be a whole number") from None
        hours = max(1, min(hours, _MAX_HISTORY_HOURS))
        since = int(time.time()) - hours * 3600

        snapshots = await self.db.net_worth_history(since)
        series: dict[int, list[list[int]]] = {}
        for point in snapshots:
            series.setdefault(point.user_id, []).append([point.at, point.net_worth])
        # Snapshots arrive ordered by time, so each moment is one run of rows.
        concentration = []
        for at, group in itertools.groupby(snapshots, key=lambda point: point.at):
            worths = [point.net_worth for point in group]
            concentration.append(
                [at, round(top_share(worths), 2), top_count(len(worths)), len(worths)]
            )
        return web.json_response(
            {
                "prices": [list(p) for p in await self.db.price_history(since)],
                "members": [
                    {"id": str(user_id), "name": self._name(user_id), "points": points}
                    for user_id, points in series.items()
                ],
                "supply": [
                    [p.at, p.banks, p.wallets, p.coins] for p in await self.db.supply_history(since)
                ],
                # [at, percent held by the top, how many that is, how many accounts]
                "concentration": concentration,
                "top_fraction": TOP_FRACTION,
            }
        )

    async def market(self, request: web.Request) -> web.Response:
        """Arm a run or call one off, exactly as ``$market`` would."""
        # A JSON content type is what forces a CORS preflight, which this app
        # never answers, so any other site the owner has open cannot post here.
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="send the direction as application/json")
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


def is_local_host(host: str) -> bool:
    """Whether a ``Host`` header names this machine in a way no site can borrow.

    The no-login decision trusts the network, but the owner's browser is on
    that network too: a page that rebinds its own domain to this address is
    same-origin with the dashboard, reads the live mines boards, and arms runs.
    It can only ever do that under a name its owner registered, so an IP
    literal, ``localhost``, a bare machine name, or an mDNS ``.local`` name is
    always the owner, and anything else is refused.
    """
    name = host[1:].partition("]")[0] if host.startswith("[") else host.partition(":")[0]
    name = name.rstrip(".").lower()
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name == "localhost" or "." not in name or name.endswith((".local", ".localhost"))
    return True


@web.middleware
async def _local_only(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Refuse a rebound hostname, and forbid framing the page that arms runs."""
    if not is_local_host(request.host):
        raise web.HTTPMisdirectedRequest(text="open the dashboard by IP address or localhost")
    response = await handler(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Api(bot))
