"""Tests for the history the dashboard graphs, its API, and the local dashboard."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
from aiohttp import test_utils, web

from flyconomy import dashboard, economy
from flyconomy.cogs.api import Api
from flyconomy.config import Settings
from flyconomy.database import Database
from tests.conftest import ALICE, BOB

TOKEN = "a-test-token-of-some-length"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HOUR = 3600


class FakeApiBot:
    """The bot surface the API cog reads."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def get_user(self, _user_id: int) -> None:
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(discord_token="placeholder", api_token=TOKEN)


@pytest.fixture
async def client(
    db: Database, settings: Settings
) -> AsyncIterator[test_utils.TestClient[web.Request, web.Application]]:
    cog = Api(FakeApiBot(db, settings))  # type: ignore[arg-type]
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


class TestApi:
    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"X": TOKEN}])
    async def test_every_route_needs_the_token(self, client, headers):
        for method, path in [
            ("GET", "/api/overview"),
            ("GET", "/api/history"),
            ("POST", "/api/market"),
        ]:
            response = await client.request(
                method, path, headers=headers, json={"direction": "bull"}
            )
            assert response.status == 401, path

    async def test_without_the_token_the_market_is_untouched(self, client, db):
        await client.post("/api/market", json={"direction": "bull"})

        assert (await db.get_market()).regime == "calm"

    async def test_the_overview_reports_the_market_and_the_boards(self, client, db):
        await db.ensure_account(ALICE)

        body = await (await client.get("/api/overview", headers=AUTH)).json()

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

    async def test_history_carries_ids_as_strings(self, client, db):
        # Snowflakes are past JavaScript's safe integers; a number would be
        # silently rounded into someone else's id in the browser.
        await db.ensure_account(ALICE)
        await db.record_history(int(time.time()), 5_000)

        body = await (await client.get("/api/history?hours=1", headers=AUTH)).json()

        assert body["prices"][0][1] == 5_000
        assert body["members"][0]["id"] == str(ALICE)

    async def test_a_bad_range_is_refused(self, client):
        response = await client.get("/api/history?hours=lots", headers=AUTH)
        assert response.status == 400

    async def test_a_run_can_be_armed_and_called_off(self, client, db):
        response = await client.post("/api/market", headers=AUTH, json={"direction": "bull"})
        assert response.status == 200
        assert (await db.get_market()).regime == "bull"

        response = await client.post("/api/market", headers=AUTH, json={"direction": "neutral"})
        assert "Called off" in (await response.json())["message"]
        assert (await db.get_market()).regime == "calm"

    @pytest.mark.parametrize("body", [{"direction": "sideways"}, {}, ["bull"]])
    async def test_a_bad_direction_is_refused(self, client, db, body):
        response = await client.post("/api/market", headers=AUTH, json=body)

        assert response.status == 400
        assert (await db.get_market()).regime == "calm"


class TestDashboard:
    async def test_it_forwards_to_the_bot_with_the_token(self, client):
        settings = dashboard.DashboardSettings(
            api_token=TOKEN, dashboard_api_url=str(client.make_url("/"))
        )
        app = dashboard.make_app(settings)
        async with test_utils.TestClient(test_utils.TestServer(app)) as local:
            page = await local.get("/")
            assert "Flyconomy" in await page.text()

            response = await local.post("/api/market", json={"direction": "bear"})
            assert response.status == 200
            assert "Armed a bear run" in (await response.json())["message"]

    async def test_an_unreachable_bot_is_a_502_not_a_crash(self):
        settings = dashboard.DashboardSettings(
            api_token=TOKEN, dashboard_api_url="http://127.0.0.1:1"
        )
        async with test_utils.TestClient(
            test_utils.TestServer(dashboard.make_app(settings))
        ) as local:
            response = await local.get("/api/overview")
            assert response.status == 502
