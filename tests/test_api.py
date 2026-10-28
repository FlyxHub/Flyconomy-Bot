"""Tests for the history the dashboard graphs, and the dashboard the bot serves."""

from __future__ import annotations

import asyncio
import datetime
import random
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from aiohttp import test_utils, web

from flyconomy import blackjack, crash, economy, events, mines, tournament
from flyconomy.cogs.admin import steer_market
from flyconomy.cogs.api import SESSION_COOKIE, Api, is_local_host, top_share
from flyconomy.cogs.events import Events
from flyconomy.cogs.gambling import Gambling
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.ratelimit import SlidingWindowLimiter
from tests.conftest import ALICE, BOB
from tests.test_cog_behavior import FakeBot, FakeChannel, FakeUser
from tests.test_events import make_cog as make_events_cog
from tests.test_lottery import make_cog as make_lottery_cog
from tests.test_mines import make_view

HOUR = 3600

OWNER = 9001
MEMBER = 9002
STRANGER = 9003

type Client = test_utils.TestClient[web.Request, web.Application]


class FakeApiBot:
    """The bot surface the API cog reads."""

    application_id = 4242

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.cogs: dict[str, object] = {}
        self.limiter = SlidingWindowLimiter(rate=100, per=60)
        # Alice and Bob stay unseen, so their names read back as their ids.
        self.users = {
            OWNER: SimpleNamespace(id=OWNER, display_name="Owner", mutual_guilds=[object()]),
            MEMBER: SimpleNamespace(id=MEMBER, display_name="Member", mutual_guilds=[object()]),
            STRANGER: SimpleNamespace(id=STRANGER, display_name="Stranger", mutual_guilds=[]),
        }

    def get_user(self, user_id: int) -> object | None:
        return self.users.get(user_id)

    async def is_owner(self, user: SimpleNamespace) -> bool:
        return bool(user.id == OWNER)

    def get_cog(self, name: str) -> object | None:
        return self.cogs.get(name)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        discord_token="placeholder", dashboard_enabled=True, dashboard_client_secret="hush"
    )


@pytest.fixture
def bot(db: Database, settings: Settings) -> FakeApiBot:
    return FakeApiBot(db, settings)


@pytest.fixture
def cog(bot: FakeApiBot) -> Api:
    cog = Api(bot)  # type: ignore[arg-type]
    cog.rng = random.Random(1)
    return cog


@pytest.fixture
async def anonymous(cog: Api) -> AsyncIterator[Client]:
    async with test_utils.TestClient(test_utils.TestServer(cog.make_app())) as client:
        yield client


def sign_in(cog: Api, client: Client, user_id: int) -> None:
    """Skip Discord: hand the client a session as if it had signed in."""
    cog._sessions[f"token-{user_id}"] = (user_id, time.time() + HOUR)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: f"token-{user_id}"})


@pytest.fixture
def client(cog: Api, anonymous: Client) -> Client:
    sign_in(cog, anonymous, OWNER)
    return anonymous


@pytest.fixture
def member(cog: Api, anonymous: Client) -> Client:
    sign_in(cog, anonymous, MEMBER)
    return anonymous


class TestHistory:
    async def test_the_price_is_kept_every_tick_and_balances_hourly(self, db):
        await db.ensure_account(ALICE)
        start = 1_000_000
        for tick in range(13):
            await db.record_history(start + tick * economy.FLX_TICK_MINUTES * 60, 5_000 + tick)

        assert len(await db.price_history(0)) == 13
        snapshots = await db.net_worth_history(0)
        assert [point.at for point in snapshots] == [start, start + HOUR]

    async def test_net_worth_is_valued_at_the_price_of_its_own_snapshot(self, db):
        await db.ensure_account(ALICE)
        await db.add_crypto(ALICE, 2)
        await db.record_history(1_000_000, 7_000)

        account = await db.get_account(ALICE)
        [point] = await db.net_worth_history(0)
        assert point.net_worth == account.wallet + account.bank + 2 * 7_000

    async def test_a_purge_erases_a_members_history(self, db):
        await db.ensure_account(ALICE)
        await db.ensure_account(BOB)
        await db.record_history(1_000_000, 5_000)

        await db.purge_user(ALICE)

        assert [point.user_id for point in await db.net_worth_history(0)] == [BOB]

    async def test_a_self_reset_keeps_it(self, db):
        await db.ensure_account(ALICE)
        await db.record_history(1_000_000, 5_000)

        await db.reset_account(ALICE, now=time.time())

        assert [point.user_id for point in await db.net_worth_history(0)] == [ALICE]


class TestTopShare:
    def test_even_shares_give_back_the_fraction(self):
        assert top_share([100] * 10) == pytest.approx(10)

    def test_one_member_holding_everything_is_all_of_it(self):
        assert top_share([0, 0, 0, 500]) == 100

    def test_the_top_is_rounded_up_and_never_empty(self):
        # Eleven accounts: the richest 10% is two of them, not one.
        assert top_share([10] + [1] * 10) == pytest.approx(100 * 11 / 20)
        assert top_share([7]) == 100

    def test_no_money_is_no_concentration(self):
        assert top_share([]) == 0
        assert top_share([0, 0]) == 0


class TestSupply:
    async def test_the_supply_splits_banks_wallets_and_coins_at_the_snapshot_price(self, db):
        await db.ensure_account(ALICE)
        await db.ensure_account(BOB)
        await db.add_crypto(BOB, 3)
        await db.record_history(1_000_000, 7_000)

        alice, bob = await db.get_account(ALICE), await db.get_account(BOB)
        [point] = await db.supply_history(0)
        assert point.banks == alice.bank + bob.bank
        assert point.wallets == alice.wallet + bob.wallet
        assert point.coins == 3 * 7_000


class TestApi:
    async def test_the_page_is_served_from_the_same_origin_as_its_data(self, client):
        page = await client.get("/")

        assert page.status == 200
        assert page.content_type == "text/html"
        assert "/api/overview" in await page.text()

    async def test_it_is_off_unless_asked_for(self, db):
        cog = Api(FakeApiBot(db, Settings(discord_token="placeholder")))  # type: ignore[arg-type]

        await cog.cog_load()

        assert cog._runner is None

    async def test_the_overview_reports_the_market_and_the_boards(self, client, db):
        await db.ensure_account(ALICE)

        body = await (await client.get("/api/overview")).json()

        assert body["viewer"] == {"name": "Owner", "admin": True}
        assert body["market"] == {
            "price": economy.FLX_PRICE,
            "regime": "calm",
            "ticks_left": 0,
            "floor": economy.FLX_PRICE_FLOOR,
            "anchor": economy.FLX_PRICE,
            "ceiling": economy.FLX_PRICE_CEILING,
            "calm_band_percent": economy.FLX_CALM_BAND_PERCENT,
        }
        assert body["leaderboards"]["net_worth"][0]["name"] == str(ALICE)
        assert body["leaderboards"]["net_worth"][0]["id"] == str(ALICE)

    async def test_the_overview_carries_the_economy_as_it_stands_now(self, client, db):
        # No snapshot has been taken at all: these come from the live balances.
        await db.ensure_account(ALICE)
        await db.ensure_account(BOB)
        await db.add_bank(ALICE, 9_000)
        await db.add_crypto(BOB, 2)

        body = await (await client.get("/api/overview")).json()

        alice, bob = await db.get_account(ALICE), await db.get_account(BOB)
        now = body["economy"]
        assert now["banks"] == alice.bank + bob.bank
        assert now["wallets"] == alice.wallet + bob.wallet
        assert now["coins"] == 2 * body["market"]["price"]
        assert (now["top_count"], now["accounts"]) == (1, 2)
        richest = max(alice.net_worth, bob.net_worth)
        assert now["top_share"] == pytest.approx(
            100 * richest / (alice.net_worth + bob.net_worth), abs=0.01
        )

    async def test_the_overview_lists_every_member_with_cash_and_coins_apart(self, client, db):
        await db.ensure_account(ALICE)
        await db.ensure_account(BOB)
        await db.add_crypto(BOB, 2)

        body = await (await client.get("/api/overview")).json()

        alice, bob = await db.get_account(ALICE), await db.get_account(BOB)
        # Richest first, at the live price: Bob's coins put him on top.
        assert body["members"] == [
            [str(BOB), str(BOB), bob.wallet + bob.bank, 2],
            [str(ALICE), str(ALICE), alice.wallet + alice.bank, 0],
        ]

    async def test_the_schedule_is_read_from_the_bots_own_loops(self, client, bot):
        when = datetime.datetime(2026, 10, 12, 8, 0, tzinfo=datetime.UTC)
        bot.cogs["Economy"] = SimpleNamespace(interest_loop=SimpleNamespace(next_iteration=when))

        schedule = (await (await client.get("/api/overview")).json())["schedule"]

        assert schedule["interest"] == {"at": when.timestamp(), "every": 86_400}
        # A job that is not running has no time to count down to.
        assert schedule["lottery"]["at"] is None
        assert schedule["tick"]["every"] == economy.FLX_TICK_MINUTES * 60

    async def test_the_tournament_counts_down_to_the_weeks_end_only_while_it_settles(
        self, client, bot
    ):
        schedule = (await (await client.get("/api/overview")).json())["schedule"]
        assert schedule["tournament"]["at"] is None

        soon = datetime.datetime(2026, 10, 15, tzinfo=datetime.UTC)
        bot.cogs["Tournament"] = SimpleNamespace(settle_loop=SimpleNamespace(next_iteration=soon))
        schedule = (await (await client.get("/api/overview")).json())["schedule"]

        # The job wakes nightly, but the week only settles on a Monday.
        at = datetime.datetime.fromtimestamp(
            schedule["tournament"]["at"], ZoneInfo("America/Chicago")
        )
        assert (at.weekday(), at.hour, at.minute) == (0, 0, 0)
        assert schedule["tournament"]["every"] == 7 * 86_400

    async def test_the_overview_carries_open_bounties(self, client, db):
        await db.add_bank(ALICE, 1_000)
        await db.post_bounty(ALICE, BOB, 1_000)

        body = (await (await client.get("/api/overview")).json())["bounties"]

        assert body["total"] == 900
        assert body["open"] == [{"id": str(BOB), "name": str(BOB), "amount": 900}]

    async def test_the_overview_carries_this_weeks_tournament(self, client, db):
        week = tournament.week_of(datetime.datetime.now(ZoneInfo("America/Chicago")))
        await db.add_bank(ALICE, tournament.ENTRY_FEE)
        await db.join_tournament(ALICE, week, now=0.0)
        await db.record_house_play(ALICE, 500, 800)

        body = (await (await client.get("/api/overview")).json())["tournament"]

        assert (body["week"], body["pot"], body["entrants"]) == (week, tournament.ENTRY_FEE, 1)
        assert body["winner_takes"] == tournament.payout(tournament.ENTRY_FEE)
        assert body["standings"] == [
            {"id": str(ALICE), "name": str(ALICE), "net": 300, "wagered": 500, "qualified": False}
        ]
        assert body["last"] is None

    async def test_live_mines_boards_are_served_face_up_until_settled(self, client, bot, db):
        game = mines.Game(stake=500, mine_tiles=frozenset({3, 9}))
        live = make_view(db, game, FakeUser(ALICE))
        live.wager_id = 2
        await live.apply_reveal(0)
        done = make_view(db, mines.Game(stake=1, mine_tiles=frozenset({0})), FakeUser(BOB))
        done.wager_id = 1
        await done.apply_reveal(0)
        bot.cogs["Casino"] = SimpleNamespace(live_mines={live, done})

        body = (await (await client.get("/api/overview")).json())["mines"]

        assert (body["columns"], body["tiles"]) == (mines.COLUMNS, mines.TILES)
        # The busted round is over, so only the live one is shown.
        assert body["games"] == [
            {
                "id": "2",
                "player": str(ALICE),
                "stake": 500,
                "mines": [3, 9],
                "revealed": [0],
                "multiplier": game.multiplier,
                "next_multiplier": game.next_multiplier,
            }
        ]

    async def test_an_empty_economy_has_no_concentration(self, client):
        body = await (await client.get("/api/overview")).json()

        assert body["economy"]["accounts"] == 0
        assert body["economy"]["top_share"] == 0

    async def test_history_carries_ids_as_strings(self, client, db):
        # Snowflakes are past JavaScript's safe integers; a number would be
        # silently rounded into someone else's id in the browser.
        await db.ensure_account(ALICE)
        await db.record_history(int(time.time()), 5_000)

        body = await (await client.get("/api/history?hours=1")).json()

        assert body["prices"][0][1] == 5_000
        assert body["members"][0]["id"] == str(ALICE)

    async def test_history_carries_the_supply_and_its_concentration(self, client, db):
        await db.ensure_account(ALICE)
        await db.ensure_account(BOB)
        await db.add_bank(ALICE, 9_000)
        at = int(time.time())
        await db.record_history(at, 5_000)

        body = await (await client.get("/api/history?hours=1")).json()

        alice, bob = await db.get_account(ALICE), await db.get_account(BOB)
        assert body["supply"] == [[at, alice.bank + bob.bank, alice.wallet + bob.wallet, 0]]
        [[when, share, top, accounts]] = body["concentration"]
        assert (when, top, accounts) == (at, 1, 2)
        assert share == pytest.approx(
            100 * alice.net_worth / (alice.net_worth + bob.net_worth), abs=0.01
        )

    async def test_a_bad_range_is_refused(self, client):
        response = await client.get("/api/history?hours=lots")
        assert response.status == 400

    async def test_a_run_can_be_armed_and_called_off(self, client, db):
        response = await client.post("/api/market", json={"direction": "bull"})
        assert response.status == 200
        assert (await db.get_market()).regime == "bull"

        response = await client.post("/api/market", json={"direction": "neutral"})
        assert "Called off" in (await response.json())["message"]
        assert (await db.get_market()).regime == "calm"

    @pytest.mark.parametrize("body", [{"direction": "sideways"}, {}, ["bull"]])
    async def test_a_bad_direction_is_refused(self, client, db, body):
        response = await client.post("/api/market", json=body)

        assert response.status == 400
        assert (await db.get_market()).regime == "calm"

    async def test_a_rebound_hostname_is_refused(self, client, db):
        # DNS rebinding: an attacker's domain resolved to this address.
        response = await client.post(
            "/api/market", json={"direction": "bull"}, headers={"Host": "evil.example:8765"}
        )

        assert response.status == 421
        assert (await db.get_market()).regime == "calm"

    @pytest.mark.parametrize(
        ("host", "allowed"),
        [
            ("127.0.0.1:8765", True),
            ("10.0.0.64:8765", True),
            ("[::1]:8765", True),
            ("localhost:8765", True),
            ("my-pc:8765", True),
            ("my-pc.local:8765", True),
            ("evil.example", False),
            ("127.0.0.1.nip.io:8765", False),
        ],
    )
    def test_only_names_no_site_can_borrow_are_local(self, host, allowed):
        assert is_local_host(host) is allowed

    async def test_the_page_cannot_be_framed(self, client):
        page = await client.get("/")
        assert page.headers["X-Frame-Options"] == "DENY"

    async def test_a_cross_site_form_post_is_refused(self, client, db):
        # text/plain is a "simple" request another site can send without a preflight.
        response = await client.post(
            "/api/market", data='{"direction": "bull"}', headers={"Content-Type": "text/plain"}
        )

        assert response.status == 415
        assert (await db.get_market()).regime == "calm"


async def start_sign_in(client: Client) -> dict[str, str]:
    """Open /login and return the query it sends the browser to Discord with."""
    login = await client.get("/login", allow_redirects=False)
    sent = urlsplit(login.headers["Location"])
    assert sent.netloc == "discord.com"
    return {k: v[0] for k, v in parse_qs(sent.query).items()}


def discord_says(cog: Api, monkeypatch: pytest.MonkeyPatch, user_id: int) -> None:
    async def identify(_code: str) -> int:
        return user_id

    monkeypatch.setattr(cog, "_identify", identify)


class TestSignIn:
    async def test_a_visitor_with_no_session_is_sent_to_sign_in(self, anonymous):
        page = await anonymous.get("/", allow_redirects=False)
        assert (page.status, page.headers["Location"]) == (302, "/login")

        assert (await anonymous.get("/api/overview")).status == 401
        response = await anonymous.post("/api/market", json={"direction": "bull"})
        assert response.status == 401

    async def test_signing_in_goes_through_discord_and_back(self, anonymous, cog, monkeypatch):
        discord_says(cog, monkeypatch, OWNER)

        query = await start_sign_in(anonymous)
        assert query["client_id"] == "4242"
        assert query["redirect_uri"] == "http://localhost:8765/callback"
        assert query["scope"] == "identify"

        back = await anonymous.get(
            f"/callback?code=one-time&state={query['state']}", allow_redirects=False
        )
        assert (back.status, back.headers["Location"]) == (302, "/")

        body = await (await anonymous.get("/api/overview")).json()
        assert body["viewer"] == {"name": "Owner", "admin": True}

    async def test_a_callback_this_browser_did_not_start_signs_nobody_in(
        self, anonymous, cog, monkeypatch
    ):
        discord_says(cog, monkeypatch, OWNER)
        await start_sign_in(anonymous)

        back = await anonymous.get("/callback?code=stolen&state=forged", allow_redirects=False)

        assert back.status == 400
        assert cog._sessions == {}

    async def test_someone_outside_the_bots_servers_is_refused_at_the_door(
        self, anonymous, cog, monkeypatch
    ):
        discord_says(cog, monkeypatch, STRANGER)
        state = (await start_sign_in(anonymous))["state"]

        back = await anonymous.get(f"/callback?code=x&state={state}", allow_redirects=False)

        # Refused here, not on the next page load, which would loop via Discord.
        assert back.status == 403
        assert cog._sessions == {}

    async def test_signing_out_ends_the_session(self, client):
        assert (await client.post("/logout")).status == 200
        assert (await client.get("/api/overview")).status == 401

    async def test_an_expired_session_is_no_session(self, cog, anonymous):
        cog._sessions["old"] = (OWNER, time.time() - 1)
        anonymous.session.cookie_jar.update_cookies({SESSION_COOKIE: "old"})

        assert (await anonymous.get("/api/overview")).status == 401

    async def test_the_dashboards_own_address_is_accepted(self, db):
        settings = Settings(
            discord_token="placeholder",
            dashboard_enabled=True,
            dashboard_client_secret="hush",
            dashboard_url="https://dash.example.com",
        )
        cog = Api(FakeApiBot(db, settings))  # type: ignore[arg-type]
        async with test_utils.TestClient(test_utils.TestServer(cog.make_app())) as client:
            sign_in(cog, client, OWNER)
            ok = await client.get("/api/overview", headers={"Host": "dash.example.com"})
            elsewhere = await client.get("/api/overview", headers={"Host": "evil.example"})

        assert (ok.status, elsewhere.status) == (200, 421)

    def test_the_dashboard_will_not_start_without_a_way_to_sign_in(self):
        with pytest.raises(ValueError, match="dashboard_client_secret"):
            Settings(discord_token="placeholder", dashboard_enabled=True)


class TestMemberView:
    async def test_a_member_is_never_sent_the_regime(self, member, db, cog):
        await steer_market(db, "bull", cog.rng)

        response = await member.get("/api/overview")
        body = await response.json()

        assert body["viewer"] == {"name": "Member", "admin": False}
        assert "regime" not in body["market"]
        assert "ticks_left" not in body["market"]
        assert "bull" not in (await response.text()).lower()

    async def test_a_member_is_never_sent_a_live_mines_board(self, member, bot, db):
        live = make_view(db, mines.Game(stake=500, mine_tiles=frozenset({3})), FakeUser(ALICE))
        live.message = SimpleNamespace(id=1, channel=FakeChannel())  # type: ignore[assignment]
        bot.cogs["Casino"] = SimpleNamespace(live_mines={live})

        body = await (await member.get("/api/overview")).json()

        assert "mines" not in body

    async def test_a_member_sees_their_own_profile(self, member, db):
        await db.add_bank(MEMBER, 1_000_000)
        await db.record_house_play(MEMBER, 100, 1_500)
        await db.claim_achievements(MEMBER, time.time())

        me = await (await member.get("/api/profile")).json()

        assert me["own"] is True
        assert me["account"]["bank"] == economy.STARTING_BANK + 1_000_000
        assert me["account"]["rank"] == 1
        assert me["career"]["best_multiple"] == 15
        badges = {a["name"]: a for a in me["achievements"]}
        assert badges["Big Win"]["earned_at"] is not None
        assert badges["Regular"] == badges["Regular"] | {"earned_at": None, "progress": 1}

    async def test_the_overview_no_longer_carries_a_profile(self, member):
        assert "me" not in await (await member.get("/api/overview")).json()

    async def test_someone_elses_profile_is_condensed(self, member, db):
        await db.add_bank(ALICE, 1_000_000)
        await db.record_house_play(ALICE, 100, 1_500)
        await db.claim_achievements(ALICE, time.time())

        alice = await (await member.get(f"/api/profile?id={ALICE}")).json()

        assert alice["own"] is False
        assert alice["account"]["net_worth"] == economy.STARTING_BANK + 1_000_000
        # Their career and their progress toward locked badges stay theirs.
        assert "career" not in alice
        assert {a["name"] for a in alice["achievements"]} == {
            "First Bet",
            "Big Win",
            "Millionaire",
        }
        assert all("progress" not in a for a in alice["achievements"])

    async def test_a_member_without_an_account_has_no_profile(self, member):
        assert (await member.get(f"/api/profile?id={BOB}")).status == 404
        assert (await member.get("/api/profile?id=nobody")).status == 400

    async def test_a_member_cannot_steer_the_market(self, member, db):
        response = await member.post("/api/market", json={"direction": "bull"})

        assert response.status == 403
        assert (await db.get_market()).regime == "calm"

    async def test_a_member_still_sees_the_charts(self, member, db):
        await db.ensure_account(ALICE)
        await db.record_history(int(time.time()), 5_000)

        assert (await member.get("/api/history?hours=1")).status == 200


ANNOUNCE = 7070


def events_cog(bot: FakeApiBot, db: Database, channel: FakeChannel | None) -> Events:
    """Register a real Events cog on the API's bot, announcing into ``channel``."""
    settings = Settings(
        discord_token="placeholder",
        lottery_announce_channel_id=ANNOUNCE if channel is not None else None,
    )
    channels: dict[int, object] = {ANNOUNCE: channel} if channel is not None else {}
    cog = make_events_cog(FakeBot(db, settings, channels=channels))
    bot.cogs["Events"] = cog
    return cog


class TestEventButtons:
    async def test_the_owner_starts_an_event_where_the_scheduler_would(self, client, bot, db):
        channel = FakeChannel()
        cog = events_cog(bot, db, channel)

        response = await client.post("/api/event", json={"event": "goldrush"})

        assert response.status == 200
        assert "Gold Rush" in (await response.json())["message"]
        assert cog.current() is events.GOLD_RUSH
        assert len(channel.embeds) == 1
        overview = await (await client.get("/api/overview")).json()
        assert overview["event"]["key"] == "goldrush"

    async def test_a_member_cannot_start_one(self, member, bot, db):
        cog = events_cog(bot, db, FakeChannel())

        response = await member.post("/api/event", json={"event": "drop"})

        assert response.status == 403
        assert cog.current() is None

    async def test_an_unknown_event_is_refused(self, client, bot, db):
        events_cog(bot, db, FakeChannel())
        assert (await client.post("/api/event", json={"event": "jackpot"})).status == 400

    async def test_without_an_announcement_channel_nothing_starts(self, client, bot, db):
        cog = events_cog(bot, db, None)

        response = await client.post("/api/event", json={"event": "charity"})

        assert response.status == 409
        assert cog.current() is None

    async def test_it_demands_json(self, client, bot, db):
        events_cog(bot, db, FakeChannel())
        assert (await client.post("/api/event", data={"event": "drop"})).status == 415


@pytest.fixture
async def casino(bot: FakeApiBot, db: Database, settings: Settings) -> AsyncIterator[Gambling]:
    """The real casino and lottery cogs, and a member with a wallet to stake from."""
    cog = Gambling(bot)  # type: ignore[arg-type]
    cog.rng = random.Random(7)
    bot.cogs["Casino"] = cog
    bot.cogs["Lottery"] = make_lottery_cog(db, settings)
    await db.add_wallet(MEMBER, 10_000)
    yield cog
    if cog.live_jackpot is not None:
        cog.live_jackpot.cancel()


def hand_with_a_turn() -> random.Random:
    """A seed whose opening hand is not a natural, so there is a choice to make."""
    seed = next(i for i in range(100) if not blackjack.Game.deal(1, random.Random(i)).finished)
    return random.Random(seed)


def rocket_crashing(low: float, high: float) -> random.Random:
    """A seed whose crash round busts between ``low`` and ``high``."""
    seed = next(
        i for i in range(10_000) if low < crash.Game.deal(1, random.Random(i)).crash_point < high
    )
    return random.Random(seed)


async def wallet(db: Database) -> int:
    return (await db.get_account(MEMBER)).wallet


class TestCasinoPage:
    async def test_the_floor_is_served_to_a_member(self, member):
        response = await member.get("/play")

        assert response.status == 200
        assert "text/html" in response.headers["Content-Type"]

    async def test_the_brand_images_ship_with_the_package(self, member):
        for name in ("logo.jpg", "banner.jpg"):
            response = await member.get(f"/brand/{name}")

            assert response.status == 200
            assert response.content_type == "image/jpeg"
        assert (await member.get("/brand/missing.jpg")).status == 404

    async def test_walking_in_signed_out_goes_to_sign_in(self, anonymous):
        response = await anonymous.get("/play", allow_redirects=False)

        assert (response.status, response.headers["Location"]) == (302, "/login")

    async def test_walking_in_opens_no_account(self, member, db, casino):
        await db.purge_user(MEMBER)

        body = await (await member.get("/api/casino")).json()

        assert (body["wallet"], body["bank"]) == (0, 0)
        assert await db.find_account(MEMBER) is None
        assert body["rules"]["slot_reel"][0]["emoji"] == economy.SLOT_REEL[0].emoji


class TestCasinoTables:
    async def test_a_board_is_played_from_the_page_without_its_mines(self, member, cog, casino, db):
        before = await wallet(db)

        dealt = await (
            await member.post("/api/casino/deal", json={"game": "mines", "bet": 100})
        ).json()

        assert dealt["wallet"] == before - 100
        assert dealt["table"]["tiles"] == [mines.HIDDEN] * mines.TILES
        board = cog._tables[MEMBER].game
        safe = next(t for t in range(mines.TILES) if not board.is_mine(t))
        mine = next(iter(board.mine_tiles))

        moved = await member.post("/api/casino/move", json={"move": "reveal", "tile": safe})
        state = (await moved.json())["table"]
        # Nothing about where the mines are, in any field, until it is over.
        assert state["tiles"][safe] == mines.SAFE
        assert mines.MINE not in state["tiles"]
        assert str(sorted(board.mine_tiles)) not in await moved.text()

        done = await (await member.post("/api/casino/move", json={"move": "cashout"})).json()

        assert done["table"]["finished"] is True
        assert done["table"]["tiles"][mine] == mines.MINE
        assert done["wallet"] == before - 100 + mines.payout(100, board.multiplier)
        assert "First Bet" in {a["name"] for a in done["unlocked"]}

    async def test_the_dealers_hole_card_stays_down_until_the_hand_is_over(self, member, casino):
        casino.rng = hand_with_a_turn()

        dealt = await (
            await member.post("/api/casino/deal", json={"game": "blackjack", "bet": 50})
        ).json()
        assert len(dealt["table"]["dealer"]) == 1
        assert dealt["table"]["dealer_value"] is None

        stood = await (await member.post("/api/casino/move", json={"move": "stand"})).json()

        assert stood["table"]["finished"] is True
        assert len(stood["table"]["dealer"]) >= 2
        assert stood["table"]["result"]

    async def test_the_crash_point_stays_secret_until_the_round_is_over(
        self, member, cog, casino, db
    ):
        casino.rng = rocket_crashing(5, 20)
        before = await wallet(db)

        dealt = await member.post("/api/casino/deal", json={"game": "crash", "bet": 100})
        point = cog._tables[MEMBER].game.crash_point

        assert (await dealt.json())["table"]["crash_point"] is None
        assert f"{point}" not in await dealt.text()

        done = await (await member.post("/api/casino/move", json={"move": "cashout"})).json()

        cashed = done["table"]["cashed_out"]
        assert done["table"]["finished"] is True
        assert done["table"]["crash_point"] == point
        assert done["wallet"] == before - 100 + crash.payout(100, cashed)

    async def test_a_polling_page_sees_the_bust_when_it_happens(self, member, cog, casino, db):
        # Busts in well under a second, long before the tick would look.
        casino.rng = rocket_crashing(1.0, 1.03)
        before = await wallet(db)
        await member.post("/api/casino/deal", json={"game": "crash", "bet": 100})

        await asyncio.sleep(0.6)
        polled = await (await member.get("/api/casino/table")).json()

        assert polled["table"]["finished"] is True
        assert polled["table"]["cashed_out"] is None
        assert polled["table"]["crash_point"] == cog._tables[MEMBER].game.crash_point
        assert await wallet(db) == before - 100

    async def test_a_bet_over_the_table_limit_costs_nothing(self, member, casino, db, settings):
        before = await wallet(db)

        response = await member.post(
            "/api/casino/deal", json={"game": "mines", "bet": settings.max_bet + 1}
        )

        assert response.status == 400
        assert await wallet(db) == before

    async def test_one_table_at_a_time(self, member, casino):
        await member.post("/api/casino/deal", json={"game": "mines", "bet": 10})

        again = await member.post("/api/casino/deal", json={"game": "crash", "bet": 10})

        assert again.status == 409

    async def test_dealing_spends_the_shared_rate_limit(self, member, bot, casino):
        bot.limiter = SlidingWindowLimiter(rate=1, per=60)
        bot.limiter.acquire(MEMBER)

        response = await member.post("/api/casino/deal", json={"game": "mines", "bet": 10})

        assert response.status == 429

    async def test_a_play_must_be_posted_as_json(self, member, casino):
        response = await member.post("/api/casino/deal", data={"game": "mines", "bet": "10"})

        assert response.status == 415

    async def test_a_walked_away_board_settles_on_its_own(
        self, member, cog, casino, db, monkeypatch
    ):
        # A posted view's clock is Discord's; an unposted one has only ours.
        monkeypatch.setattr(mines, "DECISION_TIMEOUT_SECONDS", 0.05)
        before = await wallet(db)
        await member.post("/api/casino/deal", json={"game": "mines", "bet": 100})

        await asyncio.sleep(0.2)

        assert cog._tables[MEMBER].is_finished()
        # Untouched, so refunded, exactly as an abandoned Discord board is.
        assert await wallet(db) == before
        assert (await (await member.get("/api/casino")).json())["table"]["finished"] is True


class TestCasinoBets:
    @pytest.mark.parametrize(
        ("game", "pick"),
        [
            ("coinflip", "heads"),
            ("rps", "rock"),
            ("dice", 4),
            ("slots", None),
            ("war", None),
            ("roulette", "red"),
            ("roulette", "00"),
        ],
    )
    async def test_every_instant_game_plays_and_settles(self, member, casino, db, game, pick):
        before = await wallet(db)

        response = await member.post(
            "/api/casino/bet", json={"game": game, "bet": 100, "pick": pick}
        )
        body = await response.json()

        assert response.status == 200
        result = body["result"]
        assert result["stake"] == 100
        assert body["wallet"] == before - 100 + result["paid"]
        assert await wallet(db) == body["wallet"]

    async def test_a_bad_pick_costs_nothing(self, member, casino, db, bot):
        before = await wallet(db)

        response = await member.post(
            "/api/casino/bet", json={"game": "dice", "bet": 100, "pick": 7}
        )

        assert response.status == 400
        assert await wallet(db) == before
        assert bot.limiter.retry_after(MEMBER) == 0

    async def test_a_spin_on_the_page_waits_out_the_same_timer_as_discord(self, member, casino, db):
        await member.post("/api/casino/bet", json={"game": "slots", "bet": 100})

        again = await member.post("/api/casino/bet", json={"game": "slots", "bet": 100})

        assert again.status == 400
        assert "cooldown" in (await again.text()).lower()
        # The one timer the command reads, too.
        assert casino.slots_wait(MEMBER) > economy.SLOTS_COOLDOWN_SECONDS - 5


class TestCasinoCage:
    async def test_deposit_and_withdraw_move_money_between_wallet_and_bank(
        self, member, casino, db
    ):
        before = await db.get_account(MEMBER)

        deposited = await (
            await member.post("/api/casino/bank", json={"move": "deposit", "amount": 4_000})
        ).json()
        withdrawn = await (
            await member.post("/api/casino/bank", json={"move": "withdraw", "amount": 1_000})
        ).json()

        assert (deposited["wallet"], deposited["bank"]) == (
            before.wallet - 4_000,
            before.bank + 4_000,
        )
        assert (withdrawn["wallet"], withdrawn["bank"]) == (
            before.wallet - 3_000,
            before.bank + 3_000,
        )

    async def test_withdrawing_more_than_the_bank_holds_is_refused(self, member, casino, db):
        bank = (await db.get_account(MEMBER)).bank

        response = await member.post(
            "/api/casino/bank", json={"move": "withdraw", "amount": bank + 1}
        )

        assert response.status == 400
        assert (await db.get_account(MEMBER)).bank == bank


class TestCageAnnouncements:
    """A web withdrawal has to be as visible as ``$withdraw``, or the page is a safe."""

    @pytest.fixture
    def channel(self, bot, cog, monkeypatch) -> FakeChannel:
        bot.settings = bot.settings.model_copy(update={"lottery_announce_channel_id": 555})
        channel = FakeChannel()

        async def resolve(channel_id: int) -> FakeChannel | None:
            return channel if channel_id == 555 else None

        monkeypatch.setattr(cog, "resolve_channel", resolve)
        return channel

    async def test_deposits_and_withdrawals_are_announced_in_discord(self, member, casino, channel):
        await member.post("/api/casino/bank", json={"move": "deposit", "amount": 4_000})
        await member.post("/api/casino/bank", json={"move": "withdraw", "amount": 1_500})

        assert channel.sent == [
            f"<@{MEMBER}> deposited $4,000 into the bank at the casino.",
            f"<@{MEMBER}> withdrew $1,500 from the bank at the casino.",
        ]

    async def test_a_refused_withdrawal_announces_nothing(self, member, casino, channel, db):
        bank = (await db.get_account(MEMBER)).bank

        await member.post("/api/casino/bank", json={"move": "withdraw", "amount": bank + 1})

        assert channel.sent == []

    @pytest.mark.parametrize(
        ("bet", "pick", "announced"),
        [
            (
                10_000,
                "heads",
                f"🎰 <@{MEMBER}> won $20,000 on a $10,000 coinflip bet at the casino.",
            ),
            (
                10_000,
                "tails",
                f"💸 <@{MEMBER}> lost $10,000 on a $10,000 coinflip bet at the casino.",
            ),
            (100, "heads", None),
            (100, "tails", None),
        ],
        ids=["a big win", "a big loss", "a small win", "a small loss"],
    )
    async def test_only_big_results_on_the_page_are_announced(
        self, member, casino, channel, db, bet, pick, announced
    ):
        await db.add_wallet(MEMBER, 10_000)
        # The coin always lands heads.
        casino.rng = SimpleNamespace(choice=lambda options: options[0])

        await member.post("/api/casino/bet", json={"game": "coinflip", "bet": bet, "pick": pick})

        assert channel.sent == ([announced] if announced else [])

    async def test_a_table_win_is_announced_once(self, member, bot, cog, casino, channel):
        bot.settings = bot.settings.model_copy(update={"casino_win_announce_min": 1})
        await member.post("/api/casino/deal", json={"game": "mines", "bet": 1_000})
        table = cog._tables[MEMBER]
        safe = next(t for t in range(mines.TILES) if not table.game.is_mine(t))
        await member.post("/api/casino/move", json={"move": "reveal", "tile": safe})

        await member.post("/api/casino/move", json={"move": "cashout"})
        await cog._announce_table(MEMBER, table)

        assert len(channel.sent) == 1
        assert "on a $1,000 mines bet" in channel.sent[0]

    async def test_without_a_channel_the_money_still_moves(self, member, casino, db):
        before = (await db.get_account(MEMBER)).wallet

        response = await member.post("/api/casino/bank", json={"move": "deposit", "amount": 100})

        assert response.status == 200
        assert (await db.get_account(MEMBER)).wallet == before - 100


class TestCasinoPots:
    async def test_a_ticket_is_bought_once_a_draw(self, member, casino, db, settings):
        await db.add_bank(MEMBER, settings.lottery_ticket_price)
        bank = (await db.get_account(MEMBER)).bank

        bought = await (await member.post("/api/casino/lottery", json={})).json()
        again = await member.post("/api/casino/lottery", json={})

        assert bought["lottery"]["entered"] is True
        assert bought["bank"] == bank - settings.lottery_ticket_price
        assert again.status == 409

    async def test_without_an_announcement_channel_the_page_cannot_open_a_round(
        self, member, casino, db
    ):
        before = await wallet(db)

        response = await member.post("/api/casino/jackpot", json={"ante": 500})

        assert response.status == 400
        assert "/jackpot" in await response.text()
        assert await wallet(db) == before

    async def test_the_page_joins_a_round_opened_in_discord(self, member, casino, db):
        await db.add_wallet(ALICE, 1_000)
        await casino.ante_jackpot(ALICE, 1_000, FakeChannel())

        body = await (await member.post("/api/casino/jackpot", json={"ante": 500})).json()

        round_ = body["jackpot"]
        assert round_["open"] is True
        assert round_["pot"] == 1_500
        assert [e["me"] for e in round_["entrants"]] == [False, True]
        assert round_["entrants"][1]["chance"] == pytest.approx(500 / 1_500)
