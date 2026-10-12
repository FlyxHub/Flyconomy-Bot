"""Tests for the history the dashboard graphs, and the dashboard the bot serves."""

from __future__ import annotations

import datetime
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from aiohttp import test_utils, web

from flyconomy import economy, mines
from flyconomy.cogs.api import Api, top_share
from flyconomy.config import Settings
from flyconomy.database import Database
from tests.conftest import ALICE, BOB
from tests.test_cog_behavior import FakeUser
from tests.test_mines import make_view

HOUR = 3600


class FakeApiBot:
    """The bot surface the API cog reads."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.cogs: dict[str, object] = {}

    def get_user(self, _user_id: int) -> None:
        return None

    def get_cog(self, name: str) -> object | None:
        return self.cogs.get(name)


@pytest.fixture
def settings() -> Settings:
    return Settings(discord_token="placeholder", dashboard_enabled=True)


@pytest.fixture
def bot(db: Database, settings: Settings) -> FakeApiBot:
    return FakeApiBot(db, settings)


@pytest.fixture
async def client(
    bot: FakeApiBot,
) -> AsyncIterator[test_utils.TestClient[web.Request, web.Application]]:
    cog = Api(bot)  # type: ignore[arg-type]
    cog.rng.seed(1)
    async with test_utils.TestClient(test_utils.TestServer(cog.make_app())) as client:
        yield client


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

        assert body["market"] == {
            "price": economy.FLX_PRICE,
            "regime": "calm",
            "minutes_left": 0,
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
