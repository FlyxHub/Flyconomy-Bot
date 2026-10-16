"""The owner's dashboard: one page and the JSON it reads, served from inside the bot.

It lives in the bot's process rather than beside it because the database does:
SQLite is not safe to share over a network, and a second process opening the
same file would be a second writer the migrations and transactions know nothing
about. The page and its API share one origin, so there is nothing to proxy.

Off unless ``dashboard_enabled`` is set. Visitors sign in with Discord. The
bot's owner sees everything and can arm a run; a member of a server the bot is
in sees the rest, and never the regime or a live mines board, because nothing
member-facing may name the regime and a board shown face up is a solved one.
"""

from __future__ import annotations

import datetime
import ipaddress
import itertools
import logging
import math
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Final
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiohttp.typedefs import Handler

from flyconomy import economy, mines, tournament
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

DISCORD_API: Final = "https://discord.com/api/v10"
DISCORD_AUTHORIZE: Final = "https://discord.com/oauth2/authorize"

#: How long a sign-in lasts. Discord remembers the consent, so signing in
#: again is a redirect and a click at most.
SESSION_SECONDS: Final = 7 * 86_400

SESSION_COOKIE: Final = "flyconomy_session"
STATE_COOKIE: Final = "flyconomy_oauth_state"


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who is looking at the dashboard, as of this request."""

    name: str
    admin: bool


VIEWER: Final = web.RequestKey("viewer", Viewer)


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
        # ponytail: sessions live in memory, so a restart signs everyone out;
        # Discord remembers the consent, so signing back in is one redirect.
        self._sessions: dict[str, tuple[int, float]] = {}

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

        public_host = urlsplit(self.settings.dashboard_url).netloc.lower()

        @web.middleware
        async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
            """Refuse a rebound hostname, demand a sign-in, and forbid framing the page."""
            if request.host.lower() != public_host and not is_local_host(request.host):
                raise web.HTTPMisdirectedRequest(text="open the dashboard at its own address")
            if request.path not in _SIGN_IN_PATHS:
                session = self._sessions.get(request.cookies.get(SESSION_COOKIE, ""))
                if session is None or session[1] < time.time():
                    if request.path.startswith("/api/"):
                        raise web.HTTPUnauthorized(text="sign in first")
                    raise web.HTTPFound("/login")
                viewer = await self._viewer(session[0])
                if viewer is None:
                    raise web.HTTPForbidden(text=_NOT_A_MEMBER)
                request[VIEWER] = viewer
            response = await handler(request)
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            return response

        app = web.Application(middlewares=[guard])
        app.router.add_get("/", index)
        app.router.add_get("/login", self.login)
        app.router.add_get("/callback", self.callback)
        app.router.add_post("/logout", self.logout)
        app.router.add_get("/api/overview", self.overview)
        app.router.add_get("/api/history", self.history)
        app.router.add_post("/api/market", self.market)
        return app

    # ---------------------------------------------------------------- sign-in

    @property
    def _redirect_uri(self) -> str:
        return self.settings.dashboard_url.rstrip("/") + "/callback"

    def _set_cookie(self, response: web.StreamResponse, name: str, value: str, age: int) -> None:
        # Lax keeps the cookie off another site's POST, on top of the JSON
        # content type the market endpoint already demands.
        response.set_cookie(
            name,
            value,
            max_age=age,
            httponly=True,
            samesite="Lax",
            secure=self.settings.dashboard_url.startswith("https://"),
        )

    async def _viewer(self, user_id: int) -> Viewer | None:
        """Who a signed-in Discord user is to the dashboard, or ``None`` if nobody.

        Worked out on every request rather than stored at sign-in, so leaving
        the server or losing ownership takes effect at once. The owner check is
        the same one ``$market`` makes, so the web and Discord agree on who
        can steer.
        """
        user = self.bot.get_user(user_id)
        if user is None:
            return None
        if await self.bot.is_owner(user):
            return Viewer(user.display_name, admin=True)
        if user.mutual_guilds:
            return Viewer(user.display_name, admin=False)
        return None

    async def login(self, _request: web.Request) -> web.StreamResponse:
        """Send the visitor to Discord to say who they are."""
        state = secrets.token_urlsafe(16)
        query = urlencode(
            {
                "client_id": self.bot.application_id,
                "response_type": "code",
                "scope": "identify",
                "redirect_uri": self._redirect_uri,
                "state": state,
                # Skips Discord's consent screen once it has been accepted.
                "prompt": "none",
            }
        )
        response = web.HTTPFound(f"{DISCORD_AUTHORIZE}?{query}")
        self._set_cookie(response, STATE_COOKIE, state, 600)
        raise response

    async def callback(self, request: web.Request) -> web.StreamResponse:
        """Finish a sign-in: confirm the state, ask Discord who it was, and start a session."""
        state = request.cookies.get(STATE_COOKIE)
        code = request.query.get("code")
        # The state cookie is what ties this redirect to a sign-in this
        # browser started, so a link someone else crafted signs nobody in.
        returned = request.query.get("state", "").encode()
        if not (code and state and secrets.compare_digest(state.encode(), returned)):
            raise web.HTTPBadRequest(
                text="Sign-in did not finish. Open the dashboard to try again."
            )
        try:
            user_id = await self._identify(code)
        except (aiohttp.ClientError, KeyError, ValueError):
            log.exception("Dashboard sign-in could not be confirmed with Discord")
            raise web.HTTPBadGateway(
                text="Discord could not confirm who you are. Try again."
            ) from None
        viewer = await self._viewer(user_id)
        # Refused here rather than on the next page load, which would send
        # them back to Discord, which would send them straight back here.
        if viewer is None:
            raise web.HTTPForbidden(text=_NOT_A_MEMBER)
        now = time.time()
        self._sessions = {t: s for t, s in self._sessions.items() if s[1] > now}
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (user_id, now + SESSION_SECONDS)
        log.info("Dashboard sign-in: %s (%s)", viewer.name, "owner" if viewer.admin else "member")
        response = web.HTTPFound("/")
        self._set_cookie(response, SESSION_COOKIE, token, SESSION_SECONDS)
        response.del_cookie(STATE_COOKIE)
        raise response

    async def _identify(self, code: str) -> int:
        """Trade a sign-in's one-time code for the id of the Discord user it belongs to."""
        secret = self.settings.dashboard_client_secret
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": str(self.bot.application_id),
            "client_secret": secret.get_secret_value() if secret is not None else "",
        }
        async with aiohttp.ClientSession(raise_for_status=True) as http:
            async with http.post(f"{DISCORD_API}/oauth2/token", data=form) as response:
                token = (await response.json())["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            async with http.get(f"{DISCORD_API}/users/@me", headers=headers) as response:
                return int((await response.json())["id"])

    async def logout(self, request: web.Request) -> web.Response:
        """End the session. Lands on a page of its own, since ``/`` would sign straight back in."""
        self._sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        response = web.Response(text=_SIGNED_OUT, content_type="text/html")
        response.del_cookie(SESSION_COOKIE)
        return response

    # ------------------------------------------------------------------- data

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

    async def _tournament(self) -> dict[str, object]:
        """This week's tournament as it stands, and how last week ended."""
        now = datetime.datetime.now(ZoneInfo(self.timezone))
        week = tournament.week_of(now)
        pot, entrants, leaders = await self.db.tournament_standings(week)
        last = await self.db.last_tournament_result()
        return {
            "week": week,
            "pot": pot,
            "entrants": entrants,
            "winner_takes": tournament.payout(pot),
            "min_wagered": tournament.MIN_WAGERED,
            "standings": [
                {
                    "id": str(e.user_id),
                    "name": self._name(e.user_id),
                    "net": e.net,
                    "wagered": e.wagered,
                    "qualified": e.qualified,
                }
                for e in leaders
            ],
            "last": None
            if last is None
            else {
                "week": last.week,
                "winner": None if last.winner_id is None else self._name(last.winner_id),
                "paid": last.paid,
                "entrants": last.entrants,
            },
        }

    def _tournament_ends(self) -> float | None:
        """When this week's tournament settles, or ``None`` if nothing will settle it.

        The settling job wakes every midnight but only acts on a Monday, so
        its own next run is not the moment that matters. The week's end is,
        shown only while that job is actually running to act on it.
        """
        if self._next_run("Tournament", "settle_loop") is None:
            return None
        return tournament.week_ends(datetime.datetime.now(ZoneInfo(self.timezone))).timestamp()

    def _board(self, entries: list[LeaderboardEntry]) -> list[dict[str, object]]:
        # Ids as strings: snowflakes are past JavaScript's safe integers.
        return [
            {"id": str(e.user_id), "name": self._name(e.user_id), "amount": e.amount}
            for e in entries
        ]

    async def overview(self, request: web.Request) -> web.Response:
        """The market, the lottery, and the leaderboards as they stand.

        A member gets the same response less the regime and the live mines
        boards. They are left out here rather than hidden by the page, since
        anything sent is readable whatever the page does with it.
        """
        viewer = request[VIEWER]
        market = await self.db.get_market()
        lottery = await self.db.lottery_state()
        # The live end of the hourly charts, valued at the same price as the
        # rest of this response so the page never disagrees with itself.
        supply = await self.db.current_supply(int(time.time()), market.price)
        worths = await self.db.net_worths(market.price)
        members = await self.db.member_balances(market.price, MEMBERS_SHOWN)
        market_body: dict[str, object] = {
            "price": market.price,
            # The band the walk lives in, so the dashboard draws the bot's
            # own bounds rather than a copy of them.
            "floor": economy.FLX_PRICE_FLOOR,
            "anchor": economy.FLX_PRICE,
            "ceiling": economy.FLX_PRICE_CEILING,
            "calm_band_percent": economy.FLX_CALM_BAND_PERCENT,
        }
        if viewer.admin:
            market_body |= {"regime": market.regime, "ticks_left": market.ticks_left}
        body: dict[str, object] = {
            "viewer": {"name": viewer.name, "admin": viewer.admin},
            "market": market_body,
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
            "members": [[str(m.user_id), self._name(m.user_id), m.cash, m.coins] for m in members],
            "schedule": {
                "tick": {
                    "at": self._next_run("Market", "tick_loop"),
                    "every": economy.FLX_TICK_MINUTES * 60,
                },
                "interest": {"at": self._next_run("Economy", "interest_loop"), "every": 86_400},
                "lottery": {"at": self._next_run("Lottery", "draw_loop"), "every": 86_400},
                "tournament": {"at": self._tournament_ends(), "every": 7 * 86_400},
            },
            "bounties": {
                "total": await self.db.bounty_total(),
                "open": self._board(await self.db.top_bounties()),
            },
            "tournament": await self._tournament(),
            "circulating": await self.db.total_crypto(),
            "leaderboards": {
                "net_worth": self._board(await self.db.top_net_worth()),
                "wallet": self._board(await self.db.top_wallets()),
                "crypto": self._board(await self.db.top_crypto(economy.LEADERBOARD_SIZE)),
            },
        }
        if viewer.admin:
            body["mines"] = {
                "columns": mines.COLUMNS,
                "tiles": mines.TILES,
                "games": self._live_mines(),
            }
        return web.json_response(body)

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
        if not request[VIEWER].admin:
            raise web.HTTPForbidden(text="only the bot's owner can steer the market")
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


#: Paths a visitor reaches before they have a session.
_SIGN_IN_PATHS: Final = frozenset({"/login", "/callback", "/logout"})

_NOT_A_MEMBER: Final = "The dashboard is only open to members of a server the bot is in."

_SIGNED_OUT: Final = (
    '<!doctype html><meta charset="utf-8"><title>Signed out</title>'
    '<p>Signed out of the Flyconomy dashboard. <a href="/login">Sign in again</a></p>'
)


def is_local_host(host: str) -> bool:
    """Whether a ``Host`` header names this machine in a way no site can borrow.

    Besides ``dashboard_url``'s own host, these are the only names the page
    answers to. A page that rebinds its own domain to this address is
    same-origin with the dashboard, and could only do it under a name its
    owner registered, so an IP literal, ``localhost``, a bare machine name, or
    an mDNS ``.local`` name is always this machine, and anything else is
    refused. The session cookie already keeps such a page signed out; this is
    the second lock.
    """
    name = host[1:].partition("]")[0] if host.startswith("[") else host.partition(":")[0]
    name = name.rstrip(".").lower()
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name == "localhost" or "." not in name or name.endswith((".local", ".localhost"))
    return True


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Api(bot))
