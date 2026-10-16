"""Tests for the history the dashboard graphs, and the dashboard the bot serves."""

from __future__ import annotations

import datetime
import random
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from aiohttp import test_utils, web

from flyconomy import economy, mines, tournament
from flyconomy.cogs.admin import steer_market
from flyconomy.cogs.api import SESSION_COOKIE, Api, is_local_host, top_share
from flyconomy.config import Settings
from flyconomy.database import Database
from tests.conftest import ALICE, BOB
from tests.test_cog_behavior import FakeUser
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
        await db.record_tournament_play(ALICE, 500, 800)

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
        live.message = SimpleNamespace(id=2)  # type: ignore[assignment]
        await live.apply_reveal(0)
        done = make_view(db, mines.Game(stake=1, mine_tiles=frozenset({0})), FakeUser(BOB))
        done.message = SimpleNamespace(id=1)  # type: ignore[assignment]
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
        live.message = SimpleNamespace(id=1)  # type: ignore[assignment]
        bot.cogs["Casino"] = SimpleNamespace(live_mines={live})

        body = await (await member.get("/api/overview")).json()

        assert "mines" not in body

    async def test_a_member_cannot_steer_the_market(self, member, db):
        response = await member.post("/api/market", json={"direction": "bull"})

        assert response.status == 403
        assert (await db.get_market()).regime == "calm"

    async def test_a_member_still_sees_the_charts(self, member, db):
        await db.ensure_account(ALICE)
        await db.record_history(int(time.time()), 5_000)

        assert (await member.get("/api/history?hours=1")).status == 200
