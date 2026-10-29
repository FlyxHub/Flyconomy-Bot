"""The owner's dashboard: one page and the JSON it reads, served from inside the bot.

It lives in the bot's process rather than beside it because the database does:
SQLite is not safe to share over a network, and a second process opening the
same file would be a second writer the migrations and transactions know nothing
about. The page and its API share one origin, so there is nothing to proxy.

Off unless ``dashboard_enabled`` is set. Visitors sign in with Discord. The
bot's owner sees everything and can arm a run; a member of a server the bot is
in sees the rest, and never the regime or a live mines board, because nothing
member-facing may name the regime and a board shown face up is a solved one.

Members also play every house game, the jackpot, and the lottery on the
casino floor at ``/play``. The page only ever draws what the server sends, and
the server sends a table the way the Discord embed shows it -- unturned tiles
hidden, the dealer's hole card face down, the crash point unknown -- because a
board whose answer reached the browser is solved however the page hides it.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import itertools
import logging
import math
import secrets
import time
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Final
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import discord
from aiohttp import web
from aiohttp.typedefs import Handler
from discord.ext import commands

from flyconomy import (
    achievements,
    blackjack,
    crash,
    economy,
    embeds,
    events,
    jackpot,
    mines,
    tournament,
)
from flyconomy.bot import FlyconomyBot, describe_command_error
from flyconomy.cogs.admin import steer_market
from flyconomy.cogs.base import BaseCog
from flyconomy.cogs.events import Events
from flyconomy.cogs.gambling import Bet, Gambling
from flyconomy.cogs.lottery import Lottery
from flyconomy.database import LeaderboardEntry
from flyconomy.errors import FlyconomyError, RateLimitedError
from flyconomy.views import BlackjackView, CrashView, MinesView

log = logging.getLogger(__name__)

#: Where the dashboard listens. Fixed rather than a setting because
#: ``compose.yaml`` maps it.
PORT: Final = 8765

#: The page, shipped as package data beside the member guide.
PAGE_RESOURCE: Final = "data/dashboard.html"

#: The casino floor members play on, served at ``/play``.
CASINO_RESOURCE: Final = "data/casino.html"

#: The banner over the casino door, served at ``/brand/<name>``.
BRAND_DIR: Final = "data/brand"

#: The slice of members whose share of the money the dashboard tracks.
TOP_FRACTION: Final = 0.10

#: The most members the overview lists by name. Enough for every member of a
#: community server; the cap only stops a huge one bloating a five-second poll.
MEMBERS_SHOWN: Final = 300

#: The most rows each leaderboard sends. The page shows as many as fit the
#: panel, never fewer than ``LEADERBOARD_SIZE``, so this only needs to cover
#: the tallest the owner's column makes it.
LEDGER_ROWS: Final = 25

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

    user_id: int
    name: str
    admin: bool


VIEWER: Final = web.RequestKey("viewer", Viewer)

#: A game the dashboard deals: one of the casino's own views, never posted.
type Table = BlackjackView | CrashView | MinesView


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
        # One table per member, kept once settled so the page can show the
        # result, and replaced by their next deal.
        self._tables: dict[int, Table] = {}
        self._touched: dict[int, float] = {}
        # ponytail: one lock for every deal, so a double-clicked Deal cannot
        # stake twice; per-member locks if dealing is ever a bottleneck.
        self._dealing = asyncio.Lock()
        self._expiring: set[asyncio.Task[None]] = set()
        # Tables whose win has been announced, so a move and the idle timeout
        # finishing one together cannot announce it twice.
        self._announced: weakref.WeakSet[Table] = weakref.WeakSet()

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
        log.info(
            "Dashboard listening on %s:%d; sign-in redirects to %s, which the "
            "Developer Portal must list exactly",
            self.settings.api_host,
            PORT,
            self._redirect_uri,
        )

    async def cog_unload(self) -> None:
        """Stop listening."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def make_app(self) -> web.Application:
        """Build the application. Separate from :meth:`cog_load` so tests can serve it."""
        page = (resources.files("flyconomy") / PAGE_RESOURCE).read_text(encoding="utf-8")

        floor = (resources.files("flyconomy") / CASINO_RESOURCE).read_text(encoding="utf-8")

        async def index(_request: web.Request) -> web.Response:
            return web.Response(text=page, content_type="text/html")

        async def casino_page(_request: web.Request) -> web.Response:
            return web.Response(text=floor, content_type="text/html")

        brand = {
            name: (resources.files("flyconomy") / BRAND_DIR / name).read_bytes()
            for name in ("banner.webp",)
        }

        async def brand_image(request: web.Request) -> web.Response:
            image = brand.get(request.match_info["name"])
            if image is None:
                raise web.HTTPNotFound
            return web.Response(
                body=image,
                content_type="image/webp",
                headers={"Cache-Control": "private, max-age=86400"},
            )

        public_host = urlsplit(self.settings.dashboard_url).netloc.lower()
        encrypted = self.settings.dashboard_url.startswith("https://")

        @web.middleware
        async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
            """Refuse a rebound hostname, demand a sign-in, and forbid framing the page."""
            if request.host.lower() != public_host and not is_local_host(request.host):
                raise web.HTTPMisdirectedRequest(text="open the dashboard at its own address")
            # The tunnel to the bot is plain HTTP whatever the visitor used, so
            # the proxy in front says which it was. Sent back to dashboard_url
            # rather than the Host header, so a forged one goes nowhere.
            if encrypted and request.headers.get("X-Forwarded-Proto") == "http":
                raise web.HTTPPermanentRedirect(
                    self.settings.dashboard_url.rstrip("/") + request.path_qs
                )
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
            if encrypted:
                # A browser that has seen this never tries plain HTTP again.
                response.headers["Strict-Transport-Security"] = "max-age=31536000"
            return response

        app = web.Application(middlewares=[guard])
        app.router.add_get("/", index)
        app.router.add_get("/login", self.login)
        app.router.add_get("/callback", self.callback)
        app.router.add_post("/logout", self.logout)
        app.router.add_get("/api/overview", self.overview)
        app.router.add_get("/api/history", self.history)
        app.router.add_get("/api/profile", self.profile)
        app.router.add_post("/api/market", self.market)
        app.router.add_post("/api/event", self.event_start)
        app.router.add_get("/play", casino_page)
        app.router.add_get("/brand/{name}", brand_image)
        app.router.add_get("/api/casino", self.casino)
        app.router.add_get("/api/casino/table", self.casino_table)
        app.router.add_post("/api/casino/bank", self.bank)
        app.router.add_post("/api/casino/bet", self.bet)
        app.router.add_post("/api/casino/deal", self.deal)
        app.router.add_post("/api/casino/move", self.move)
        app.router.add_post("/api/casino/jackpot", self.jackpot_ante)
        app.router.add_post("/api/casino/lottery", self.lottery_ticket)
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
            # Settings refuse plain HTTP anywhere but loopback.
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
            return Viewer(user_id, user.display_name, admin=True)
        if user.mutual_guilds:
            return Viewer(user_id, user.display_name, admin=False)
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
        # Wager ids count up, so they order boards by start, from Discord and
        # this page alike.
        boards = sorted(
            ((v.wager_id or 0, v) for v in live if not v.is_finished()), key=lambda b: b[0]
        )
        return [
            {
                "id": str(wager_id),
                "player": self._name(v.player.id),
                "stake": v.game.stake,
                "mines": sorted(v.game.mine_tiles),
                "revealed": v.game.revealed,
                "multiplier": v.game.multiplier,
                "next_multiplier": v.game.next_multiplier,
            }
            for wager_id, v in boards
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

    async def profile(self, request: web.Request) -> web.Response:
        """A member's profile: the viewer's own in full, anyone else's condensed.

        The viewer's own carries their career and every achievement with how
        far along they are, because a goal you can see is the point of having
        them. Someone else's is what ``/balance @them`` already shows in
        Discord, plus the badges they have earned; their career and progress
        stay theirs, and are left out here rather than hidden by the page.
        """
        viewer = request[VIEWER]
        try:
            user_id = int(request.query.get("id", viewer.user_id))
        except ValueError:
            raise web.HTTPBadRequest(text="id must be a member id") from None
        account = await self.db.find_account(user_id)
        own = user_id == viewer.user_id
        if account is None and not own:
            raise web.HTTPNotFound(text="that member has no account")
        values = await self.db.achievement_values(user_id) or {}
        held = await self.db.earned_achievements(user_id)
        badges: list[dict[str, object]] = []
        for a in achievements.ALL:
            if not own and a.key not in held:
                continue
            badge: dict[str, object] = {
                "emoji": a.emoji,
                "name": a.name,
                "description": a.description,
                "earned_at": held.get(a.key),
            }
            if own:
                badge |= {"goal": a.goal, "progress": min(values.get(a.stat, 0), a.goal)}
            badges.append(badge)
        body: dict[str, object] = {
            "name": viewer.name if own else self._name(user_id),
            "own": own,
            "account": None,
            "achievements": badges,
        }
        if own:
            body["career"] = {
                stat: values.get(stat, 0)
                for stat in ("plays", "wagered", "best_multiple", "best_profit", "robberies")
            }
        if account is not None:
            worths = await self.db.net_worths(account.flx_price)
            body["account"] = {
                "wallet": account.wallet,
                "bank": account.bank,
                "crypto": account.crypto,
                "coin_value": account.crypto * account.flx_price,
                "net_worth": account.net_worth,
                "miner": account.miner,
                "security": account.security,
                "rob_chance": economy.rob_success_percent(account.security),
                "rank": 1 + sum(w > account.net_worth for w in worths),
                "accounts": len(worths),
            }
        return web.json_response(body)

    def _event(self) -> dict[str, object] | None:
        """The server event running now, which every member may see."""
        cog = self.bot.get_cog("Events")
        if not isinstance(cog, Events) or cog.current() is None or cog.active is None:
            return None
        event, ends = cog.active
        return {
            "key": event.key,
            "emoji": event.emoji,
            "name": event.name,
            "blurb": event.blurb,
            "ends": ends,
        }

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
        # Read beside the balances, so a fee lands in both or in neither.
        creator_fees = self.db.creator_fees
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
            "event": self._event(),
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
            # [creator id, fees paid since startup], so the live feed can take
            # the fees out of the creator's moves rather than announce them.
            "creator_fees": [str(self.settings.creator_tax_user_id), creator_fees]
            if self.settings.creator_tax_user_id is not None
            else None,
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
                "net_worth": self._board(await self.db.top_net_worth(LEDGER_ROWS)),
                "wallet": self._board(await self.db.top_wallets(LEDGER_ROWS)),
                "crypto": self._board(await self.db.top_crypto(LEDGER_ROWS)),
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

    async def event_start(self, request: web.Request) -> web.Response:
        """Start a server event now, exactly as ``$startevent`` would."""
        if not request[VIEWER].admin:
            raise web.HTTPForbidden(text="only the bot's owner can start an event")
        # The same CORS-preflight lock the market endpoint relies on.
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="send the event as application/json")
        try:
            body = await request.json()
            event = events.BY_KEY.get(str(body["event"]).lower())
        except (ValueError, KeyError, TypeError):
            event = None
        cog = self.bot.get_cog("Events")
        if event is None or not isinstance(cog, Events):
            raise web.HTTPBadRequest(text=f"event must be one of {', '.join(events.BY_KEY)}")
        if not await cog.start_announced(event):
            # $startevent can fall back to the channel it was run in; a web
            # page has no channel, and an event nobody sees is pointless.
            raise web.HTTPConflict(
                text="No announcement channel is set or reachable. Use $startevent in a channel."
            )
        log.info("Dashboard started a server event: %s", event.key)
        return web.json_response({"message": f"Started {event.emoji} {event.name}."})

    # ---------------------------------------------------------------- casino
    #
    # Every game here is the casino cog's own: the instant games through its
    # play_* functions, the tables through its deal_* functions and views, the
    # jackpot and the lottery through the cogs that run them. The web only
    # reads the request, spends the shared rate limit, and says what happened.

    def _casino(self) -> Gambling:
        """The casino cog, or a 503 while it is not loaded."""
        casino = self.bot.get_cog("Casino")
        if not isinstance(casino, Gambling):
            raise web.HTTPServiceUnavailable(text="The casino is not open right now.")
        return casino

    async def _json(self, request: web.Request) -> dict[str, object]:
        """A POST's JSON body: the same CORS-preflight lock the market relies on."""
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="send the move as application/json")
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="send a JSON object")
        return body

    def _spend(self, user_id: int) -> None:
        """Charge one action to the budget every command shares, as ``cog_check`` does."""
        wait = self.limiter.acquire(user_id)
        if wait:
            raise web.HTTPTooManyRequests(text=describe_command_error(RateLimitedError(wait)))

    async def _table(self, user_id: int) -> Table | None:
        """The member's table, with a crash round that has busted settled first.

        A crash round's tick only looks every couple of seconds, so a page
        polling it would otherwise keep climbing past the crash point.
        """
        table = self._tables.get(user_id)
        if isinstance(table, CrashView):
            await table.check_bust()
        return table

    def _jackpot(self, user_id: int) -> dict[str, object] | None:
        """The jackpot round in play, or the last one, as anyone in it sees it."""
        live = getattr(self.bot.get_cog("Casino"), "live_jackpot", None)
        if live is None:
            return None
        state = live.state
        return {
            "round": state.round_number,
            "open": live.is_open(),
            "seconds_left": live.seconds_left(),
            "pot": state.pot,
            "entrants": [
                {
                    "name": self._name(entry.user_id),
                    "amount": entry.amount,
                    "chance": jackpot.win_chance(entry.amount, state.pot),
                    "me": entry.user_id == user_id,
                }
                for entry in state.entries
            ],
            "winner": self._name(live.winner_id) if live.winner_id is not None else None,
            "won": live.winner_id == user_id,
            "paid": live.paid,
            "refunded": live.refunded,
        }

    async def _lottery(self, user_id: int) -> dict[str, object]:
        """The open draw, and whether this member holds a ticket for it."""
        state = await self.db.lottery_state()
        return {
            "draw": state.draw,
            "pot": state.pot,
            "entrants": state.entrants,
            "price": self.settings.lottery_ticket_price,
            "entered": user_id in await self.db.lottery_entrants(),
            "draws_at": self._next_run("Lottery", "draw_loop"),
        }

    async def _casino_state(
        self, user_id: int, *, claim: bool = False, result: object = None, rules: bool = False
    ) -> web.Response:
        """Everything the casino page draws for one member, in one response."""
        # find, not get: walking in is not a command, so it opens no account.
        account = await self.db.find_account(user_id)
        table = await self._table(user_id)
        casino = self.bot.get_cog("Casino")
        body: dict[str, object] = {
            "wallet": account.wallet if account is not None else 0,
            "bank": account.bank if account is not None else 0,
            "max_bet": self.settings.max_bet,
            "table": table_state(table) if table is not None else None,
            "slots_wait": casino.slots_wait(user_id) if isinstance(casino, Gambling) else 0.0,
            "jackpot": self._jackpot(user_id),
            "lottery": await self._lottery(user_id),
            "result": result,
        }
        if rules:
            body["rules"] = CASINO_RULES
        if claim:
            # The web has no channel to announce in, so the page says it.
            body["unlocked"] = [
                {"emoji": a.emoji, "name": a.name, "description": a.description}
                for a in await self.db.claim_achievements(user_id, time.time())
            ]
        return web.json_response(body)

    async def casino(self, request: web.Request) -> web.Response:
        """The whole floor for the signed-in member, with the rules the page draws from."""
        return await self._casino_state(request[VIEWER].user_id, rules=True)

    async def casino_table(self, request: web.Request) -> web.Response:
        """Just the member's table: what a page polls while a crash round climbs."""
        user_id = request[VIEWER].user_id
        table = await self._table(user_id)
        body: dict[str, object] = {"table": table_state(table) if table is not None else None}
        if table is not None and table.is_finished():
            body["unlocked"] = [
                {"emoji": a.emoji, "name": a.name, "description": a.description}
                for a in await self.db.claim_achievements(user_id, time.time())
            ]
        return web.json_response(body)

    async def bank(self, request: web.Request) -> web.Response:
        """Deposit or withdraw, as ``$deposit`` and ``$withdraw`` do."""
        user_id = request[VIEWER].user_id
        body = await self._json(request)
        source, destination = _BANK_MOVES.get(str(body.get("move")), (None, None))
        amount = body.get("amount")
        if source is None or destination is None:
            raise web.HTTPBadRequest(text="move must be deposit or withdraw")
        if type(amount) is not int or amount < 1:
            raise web.HTTPBadRequest(text="amount must be a whole number of dollars")
        self._spend(user_id)
        try:
            await self.db.transfer(user_id, source=source, destination=destination, amount=amount)
        except FlyconomyError as exc:
            raise _refused(exc) from exc
        await self._announce_bank(user_id, str(body["move"]), amount)
        return await self._casino_state(user_id, result={"move": body["move"], "amount": amount})

    async def _announce_bank(self, user_id: int, move: str, amount: int) -> None:
        """Say in Discord that someone moved cash, as ``$deposit``/``$withdraw`` do.

        A command's reply lands in a channel where anyone can see a wallet
        just filled up and go and rob it. The page has no channel of its own,
        so without this a withdrawal made here would be invisible, and the web
        would be the safe way to carry cash. Best effort, like every post the
        bot makes on its own: the money has moved by now, so an unreachable
        channel is logged rather than undoing it.
        """
        verb = "deposited" if move == "deposit" else "withdrew"
        where = "into" if move == "deposit" else "from"
        await self._announce(
            f"<@{user_id}> {verb} {embeds.money(amount)} {where} the bank at the casino."
        )

    async def _announce_result(self, user_id: int, game: str, stake: int, paid: int) -> None:
        """Say in Discord that someone won or lost big on the page, as a command's reply would.

        A game in Discord is played where everyone can see it, so the whole
        server watches a wallet fill up or empty out. Only results that move
        ``casino_win_announce_min`` or more either way are posted, so a busy
        page cannot flood the channel; a push or a tie moves nothing and is
        never posted.
        """
        net = paid - stake
        if abs(net) < max(1, self.settings.casino_win_announce_min):
            return
        name = _GAME_NAMES.get(game, game)
        bet = f"on a {embeds.money(stake)} {name} bet at the casino."
        await self._announce(
            f"\N{SLOT MACHINE} <@{user_id}> won {embeds.money(paid)} {bet}"
            if net > 0
            else f"\N{MONEY WITH WINGS} <@{user_id}> lost {embeds.money(-net)} {bet}"
        )

    async def _announce_table(self, user_id: int, table: Table) -> None:
        """Announce a settled table's result, once, however it came to settle."""
        if not table.is_finished() or table in self._announced:
            return
        self._announced.add(table)
        await self._announce_result(
            user_id, str(table_state(table)["game"]), table.game.stake, _paid(table)
        )

    async def _announce(self, text: str) -> None:
        """Post a line to the announcement channel, best effort.

        The money has always moved by the time this runs, so an unreachable
        channel is logged rather than undoing it -- the same contract as every
        post the bot makes on its own.
        """
        channel_id = self.settings.lottery_announce_channel_id
        channel = await self.resolve_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            # A mention so a robber can click through, without pinging anyone.
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Could not post to the announcement channel: %s", text)

    async def bet(self, request: web.Request) -> web.Response:
        """Play one instant game through the casino's own ``play_*``."""
        user_id = request[VIEWER].user_id
        body = await self._json(request)
        game, stake, pick = body.get("game"), _stake_of(body), body.get("pick")
        casino = self._casino()
        # Every pick is checked before anything is spent, as each command does.
        wager: int | str | None = None
        match game:
            case "coinflip" if pick in economy.COIN_SIDES:
                pass
            case "rps" if pick in economy.RPS_MOVES:
                pass
            case "dice" if type(pick) is int and 1 <= pick <= economy.DICE_SIDES:
                pass
            case "roulette" if isinstance(pick, str):
                wager = economy.parse_roulette_bet(pick)
                if wager is None:
                    raise web.HTTPBadRequest(text="bet on red, black, 0, 00, or 1 to 36")
            case "slots" | "war":
                pass
            case _:
                raise web.HTTPBadRequest(text="that is not a game, or not a pick for it")
        self._spend(user_id)
        # Bet is covariant in its outcome, so every game's result fits here;
        # each branch turns its outcome into something JSON can carry.
        played: Bet[object]
        outcome: object
        try:
            match game:
                case "coinflip":
                    played = await casino.play_coinflip(user_id, str(pick), stake)
                    outcome = played.outcome
                case "rps":
                    played = await casino.play_rps(user_id, str(pick), stake)
                    outcome = played.outcome
                case "dice":
                    played = await casino.play_dice(user_id, int(str(pick)), stake)
                    outcome = played.outcome
                case "slots":
                    spun = await casino.play_slots(user_id, stake)
                    played = spun
                    outcome = [economy.SLOT_REEL.index(symbol) for symbol in spun.outcome]
                case "war":
                    drawn = await casino.play_war(user_id, stake)
                    played, outcome = drawn, [str(card) for card in drawn.outcome]
                case _:
                    assert wager is not None  # noqa: S101 - parsed above
                    landed = await casino.play_roulette(user_id, wager, stake)
                    played, outcome = landed, str(landed.outcome)
        except (FlyconomyError, commands.CommandError) as exc:
            raise _refused(exc) from exc
        await self._announce_result(user_id, str(game), played.stake, played.paid)
        return await self._casino_state(
            user_id,
            claim=True,
            result={
                "game": game,
                "stake": played.stake,
                "multiplier": played.multiplier,
                "paid": played.paid,
                "outcome": outcome,
            },
        )

    async def deal(self, request: web.Request) -> web.Response:
        """Stake and deal a table through the casino's own deal, as the command does."""
        user_id = request[VIEWER].user_id
        body = await self._json(request)
        game, stake = body.get("game"), _stake_of(body)
        if game not in ("mines", "blackjack", "crash"):
            raise web.HTTPBadRequest(text="game must be mines, blackjack, or crash")
        mine_count = body.get("mines", mines.DEFAULT_MINES)
        if type(mine_count) is not int or not mines.MIN_MINES <= mine_count <= mines.MAX_MINES:
            raise web.HTTPBadRequest(text=f"mines must be {mines.MIN_MINES} to {mines.MAX_MINES}")
        casino = self._casino()
        player = self.bot.get_user(user_id)
        if player is None:
            raise web.HTTPServiceUnavailable(text="The casino cannot see you right now.")

        async with self._dealing:
            live = self._tables.get(user_id)
            if live is not None and not live.is_finished():
                raise web.HTTPConflict(text="Finish the game you have on the table first.")
            # Charged per deal, as a command is; a move is a button press,
            # which Discord does not charge either.
            self._spend(user_id)
            table: Table
            try:
                if game == "mines":
                    table = await casino.deal_mines(player, stake, mine_count)
                elif game == "crash":
                    table = await casino.deal_crash(player, stake)
                    if not table.is_finished():
                        table.start_ticking()
                else:
                    table = await casino.deal_blackjack(player, stake)
            except FlyconomyError as exc:
                raise _refused(exc) from exc
            self._tables[user_id] = table
            self._touched[user_id] = time.monotonic()
        if not table.is_finished():
            task = asyncio.create_task(self._expire(user_id, table))
            self._expiring.add(task)
            task.add_done_callback(self._expiring.discard)
        await self._announce_table(user_id, table)
        return await self._casino_state(user_id, claim=True)

    async def move(self, request: web.Request) -> web.Response:
        """Press one of the table's buttons, through the view's own ``apply_*``."""
        user_id = request[VIEWER].user_id
        body = await self._json(request)
        table = await self._table(user_id)
        if table is None or table.is_finished():
            raise web.HTTPConflict(text="That game is already over. Deal a new one.")
        self._touched[user_id] = time.monotonic()
        problem: str | None = None
        match body.get("move"), table:
            case "reveal", MinesView():
                tile = body.get("tile")
                if type(tile) is not int or not 0 <= tile < mines.TILES:
                    raise web.HTTPBadRequest(text=f"tile must be 0 to {mines.TILES - 1}")
                problem = await table.apply_reveal(tile)
            case "cashout", MinesView() | CrashView():
                problem = await table.apply_cashout()
            case "hit", BlackjackView():
                await table.apply_hit()
            case "stand", BlackjackView():
                await table.apply_stand()
            case "double", BlackjackView():
                problem = await table.apply_double()
            case _:
                raise web.HTTPBadRequest(text="that move is not part of this game")
        if problem is not None:
            raise web.HTTPConflict(text=problem)
        await self._announce_table(user_id, table)
        return await self._casino_state(user_id, claim=table.is_finished())

    async def jackpot_ante(self, request: web.Request) -> web.Response:
        """Ante into the jackpot, opening a round in the announcement channel if none is."""
        user_id = request[VIEWER].user_id
        body = await self._json(request)
        ante = _stake_of(body, "ante")
        casino = self._casino()
        self._spend(user_id)
        # A round goes up in the channel the bot's own announcements use, so
        # everyone in Discord can still press Join on it.
        channel_id = self.settings.lottery_announce_channel_id
        channel = await self.resolve_channel(channel_id) if channel_id else None
        try:
            await casino.ante_jackpot(user_id, ante, channel)
        except (FlyconomyError, discord.HTTPException) as exc:
            raise _refused(exc) from exc
        return await self._casino_state(user_id, result={"ante": ante})

    async def lottery_ticket(self, request: web.Request) -> web.Response:
        """Buy this draw's ticket, through the lottery cog's own entry."""
        user_id = request[VIEWER].user_id
        await self._json(request)
        lottery = self.bot.get_cog("Lottery")
        if not isinstance(lottery, Lottery):
            raise web.HTTPServiceUnavailable(text="The lottery is not open right now.")
        self._spend(user_id)
        try:
            entered = await lottery.enter(user_id)
        except FlyconomyError as exc:
            raise _refused(exc) from exc
        if not entered:
            raise web.HTTPConflict(text="You already hold a ticket for this draw.")
        return await self._casino_state(user_id, claim=True, result={"ticket": True})

    async def _expire(self, user_id: int, table: Table) -> None:
        """Run the view's own timeout once its player stops pressing.

        A view only starts its clock when Discord posts it, and these never
        are, so the page keeps the clock instead: idle for the view's timeout,
        and the round ends the way a walked-away Discord one does.
        """
        timeout = table.timeout or 0.0
        while not table.is_finished():
            idle = time.monotonic() - self._touched[user_id]
            if idle >= timeout:
                await table.on_timeout()
                await self._announce_table(user_id, table)
                return
            await asyncio.sleep(timeout - idle)


#: How each game is named in an announcement, where its key would not read.
_GAME_NAMES: Final = {"rps": "rock paper scissors"}

#: The two banking moves, as the columns each one moves money between.
_BANK_MOVES: Final = {"deposit": ("wallet", "bank"), "withdraw": ("bank", "wallet")}

#: What the casino page draws its tables from. Sent once, on walking in, and
#: read from the rules modules so a retuned payout reaches the page unasked.
CASINO_RULES: Final = {
    "mine_range": [mines.MIN_MINES, mines.MAX_MINES, mines.DEFAULT_MINES],
    "mine_tiles": mines.TILES,
    "mine_columns": mines.COLUMNS,
    "dice_sides": economy.DICE_SIDES,
    "roulette_red": sorted(economy.ROULETTE_RED),
    "slot_reel": [
        {"emoji": s.emoji, "name": s.name, "triple": s.triple_return, "pair": s.pays_on_pair}
        for s in economy.SLOT_REEL
    ],
    "slot_pair": economy.SLOT_PAIR_RETURN,
    "slots_cooldown": economy.SLOTS_COOLDOWN_SECONDS,
    "returns": {
        "coinflip": economy.COINFLIP_RETURN,
        "rps": economy.RPS_RETURN,
        "dice": economy.DICE_RETURN,
        "war": economy.WAR_WIN_RETURN,
        "roulette_color": economy.ROULETTE_COLOR_RETURN,
        "roulette_pocket": economy.ROULETTE_STRAIGHT_RETURN,
    },
    "crash_growth": crash.GROWTH_PER_SECOND,
    "crash_max": crash.MAX_MULTIPLIER,
    "jackpot_cut": jackpot.HOUSE_CUT,
    "jackpot_seconds": jackpot.ROUND_SECONDS,
}


def _stake_of(body: dict[str, object], key: str = "bet") -> int:
    """A whole, positive number of dollars from a request, or a 400."""
    stake = body.get(key)
    # bool is an int to Python; a page posting `true` has not named a bet.
    if type(stake) is not int or stake < 1:
        raise web.HTTPBadRequest(text=f"{key} must be a whole number of dollars")
    return stake


def _refused(exc: Exception) -> web.HTTPBadRequest:
    """The member-facing message for a refused move, the same one Discord gives."""
    return web.HTTPBadRequest(text=describe_command_error(exc) or str(exc))


def _paid(table: Table) -> int:
    """What a settled table credited back, by the same payout the view settled with."""
    if table.voided:
        return 0
    if isinstance(table, BlackjackView):
        outcome = table.game.outcome
        return blackjack.payout(table.game.stake, outcome) if outcome is not None else 0
    cashed = table.cashed_out_multiplier
    if cashed is None:
        return 0
    if isinstance(table, MinesView):
        return mines.payout(table.game.stake, cashed)
    return crash.payout(table.game.stake, cashed)


def table_state(table: Table) -> dict[str, object]:
    """A table as its player may see it: what the Discord embed shows, no more.

    Unturned tiles stay hidden, the dealer's second card face down, and a
    crash round's crash point unknown until the round is settled. This is the
    only thing standing between the browser and a solved board, so it decides
    what to leave out here rather than trusting the page to hide it.
    """
    done = table.is_finished()
    result = table.result_line()
    common: dict[str, object] = {
        "stake": table.game.stake,
        "finished": done,
        "result": result.replace("**", "") if result is not None else None,
        "paid": _paid(table) if done else None,
    }
    if isinstance(table, MinesView):
        board = table.game
        return common | {
            "game": "mines",
            "mines": board.mine_count,
            "columns": mines.COLUMNS,
            "tiles": [mines.tile_state(board, t, finished=done) for t in range(mines.TILES)],
            "multiplier": board.multiplier,
            "next_multiplier": board.next_multiplier,
            "can_cash_out": not done and bool(board.revealed),
        }
    if isinstance(table, CrashView):
        rocket = table.game
        return common | {
            "game": "crash",
            "elapsed": table.elapsed(),
            "cashed_out": table.cashed_out_multiplier,
            "crash_point": rocket.crash_point if done else None,
        }
    hand = table.game
    return common | {
        "game": "blackjack",
        "player": [str(card) for card in hand.player],
        "player_value": hand.player_value,
        "dealer": [str(card) for card in hand.dealer] if done else [str(hand.dealer_upcard)],
        "dealer_value": hand.dealer_value if done else None,
        "can_double": not done and hand.can_double,
    }


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
