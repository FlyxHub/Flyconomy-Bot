"""Tests that a full season stays inside sane bounds.

The economy runs from one January 1 to the next, so the property that matters
is not "is inflation low" but "does the money supply stay readable across 365
days". These drive the real rules from :mod:`flyconomy.economy` over an
in-memory ledger, which keeps a whole season fast enough to run on every commit.
Database behaviour is covered separately in `test_lottery.py`.
"""

from __future__ import annotations

import random

from flyconomy import economy
from flyconomy.config import Settings

SEASON_DAYS = 365

#: A season must not leave the richest member above this. It is far above real
#: play and far below the point where numbers stop being readable, so it catches
#: runaway growth without being brittle about balance changes.
RICHEST_CEILING = 10_000_000_000

#: Nor the whole economy above this.
SUPPLY_CEILING = 100_000_000_000


#: Ticks the market takes in a day, from :data:`economy.FLX_TICK_MINUTES`.
TICKS_PER_DAY = 24 * 60 // economy.FLX_TICK_MINUTES

#: Where a trader in the simulation buys and sells. The market mean-reverts, so
#: a band either side of the anchor is a round trip that completes on its own --
#: the strategy any member would find within a week of watching the ticker.
TRADE_BUY_BELOW = int(economy.FLX_PRICE * 0.9)
TRADE_SELL_ABOVE = int(economy.FLX_PRICE * 1.1)


def run_season(
    days: int = SEASON_DAYS,
    *,
    players: int = 12,
    seed: int = 7,
    daily_cap: int | None = None,
    rake: float = 0.25,
    traders: int = 0,
    flx_cap: int | None = None,
) -> dict[str, int]:
    """Play out a season and report the money supply.

    Args:
        days: Days to simulate.
        players: How many members are active.
        seed: Random seed, so the result is reproducible.
        daily_cap: Ceiling on a daily claim, or ``None`` for the default.
        rake: Share of the house take diverted to the lottery pot.
        traders: How many of the members trade the Flyxcoin band, checking the
            price on every tick. They are the first ``traders`` players.
        flx_cap: Ceiling on one member's daily Flyxcoin buying, or ``None`` for
            the default. A huge number stands in for having no cap at all.

    Returns:
        The total supply, the richest member, and the pot.
    """
    rng = random.Random(seed)
    settings = Settings(discord_token="placeholder")
    cap = settings.max_daily_payout if daily_cap is None else daily_cap
    coin_cap = settings.max_flx_buy if flx_cap is None else flx_cap

    wallet = dict.fromkeys(range(players), 0)
    bank = dict.fromkeys(range(players), economy.STARTING_BANK)
    coins = dict.fromkeys(range(players), 0)
    pot = 0
    market = economy.MarketState(price=economy.FLX_PRICE)

    for _ in range(days):
        bought_today = dict.fromkeys(range(players), 0)
        for _tick in range(TICKS_PER_DAY if traders else 0):
            market = economy.next_flx_market(market, rng)
            for user in range(traders):
                if market.price <= TRADE_BUY_BELOW:
                    affordable = economy.affordable_flx(bank[user], market.price)
                    buy = min(affordable, economy.flx_buy_allowance(bought_today[user], coin_cap))
                    if buy:
                        bank[user] -= economy.flx_cost(buy, market.price)
                        coins[user] += buy
                        bought_today[user] += buy
                elif market.price >= TRADE_SELL_ABOVE and coins[user]:
                    bank[user] += economy.flx_cost(coins[user], market.price)
                    coins[user] = 0

        entrants = []
        for user in range(players):
            grinder = user % 3 == 0

            bank[user] += economy.daily_payout(bank[user], cap)

            for _ in range(60 if grinder else 10):
                if rng.random() < 1 / economy.BEG_SUCCESS_ODDS:
                    wallet[user] += rng.randint(economy.BEG_MIN, economy.BEG_MAX)

            for _ in range(60 if grinder else 10):
                bet = min(settings.max_bet, int(wallet[user] * 0.05))
                if bet < 1:
                    break
                wallet[user] -= bet
                multiplier = economy.slots_payout_multiplier(economy.spin_slots(rng))
                wallet[user] += bet * multiplier
                contribution = int((bet - bet * multiplier) * rake)
                if contribution > 0:
                    pot += contribution

            if bank[user] >= settings.lottery_ticket_price:
                bank[user] -= settings.lottery_ticket_price
                pot += settings.lottery_ticket_price
                entrants.append(user)

        if entrants:
            bank[rng.choice(entrants)] += pot
            pot = 0

    def worth(user: int) -> int:
        return wallet[user] + bank[user] + economy.flx_cost(coins[user], market.price)

    return {
        "supply": sum(worth(u) for u in range(players)) + pot,
        "richest": max(worth(u) for u in range(players)),
        "pot": pot,
    }


class TestDailyIsBounded:
    def test_a_claim_never_exceeds_the_cap(self):
        for bank in (0, 1_000, 100_000, 10**9, 10**15):
            assert economy.daily_payout(bank) <= economy.DAILY_PAYOUT_CAP

    def test_small_balances_still_get_the_full_rate(self):
        # The cap must not change how the early game feels.
        assert economy.daily_payout(10_000) == 1_000
        assert economy.daily_payout(50_000) == 5_000

    def test_the_cap_binds_once_the_bank_is_large(self):
        assert economy.daily_payout(10_000_000) == economy.DAILY_PAYOUT_CAP

    def test_a_season_of_daily_alone_stays_readable(self):
        # This is now the *typical* account rather than a hypothetical one: the
        # payout is automatic, so an account that is never touched again still
        # collects every day until January. What it reaches is the ceiling on
        # doing nothing at all, which is the bound that matters most.
        bank = economy.STARTING_BANK
        for _ in range(SEASON_DAYS):
            bank += economy.daily_payout(bank)
        assert bank < 10_000_000, f"a year of daily alone reached {bank:,}"

    def test_growth_is_linear_once_the_cap_binds(self):
        def after(days: int) -> int:
            bank = economy.STARTING_BANK
            for _ in range(days):
                bank += economy.daily_payout(bank)
            return bank

        # Doubling the days roughly doubles the total. It lands slightly above
        # 2 because the first weeks are still exponential, before the bank grows
        # past ten times the cap. Exponential growth would square instead.
        assert 1.9 < after(730) / after(365) < 2.3

    def test_the_uncapped_rate_would_hyperinflate(self):
        # Guards the reason the cap exists.
        bank = float(economy.STARTING_BANK)
        for _ in range(SEASON_DAYS):
            bank *= 1 + economy.DAILY_PAYOUT_RATE
        assert bank > 1e15


class TestAutomaticInterestScalesWithAccounts:
    """The payout is no longer gated by who shows up.

    Before it was automatic, the faucet was sized by the members who claimed
    it. Now every row in `bank` is paid, and rows are cheap: anyone who ever
    ran one command has one, forever. So the question the old design never had
    to ask is what happens as accounts accumulate, and the answer has to be
    "issuance grows in proportion, not faster".
    """

    @staticmethod
    def _dormant_season(accounts: int) -> int:
        """Total issued over a season to accounts that only ever collect."""
        banks = [economy.STARTING_BANK] * accounts
        issued = 0
        for _ in range(SEASON_DAYS):
            for i, bank in enumerate(banks):
                payout = economy.daily_payout(bank)
                banks[i] = bank + payout
                issued += payout
        return issued

    def test_issuance_is_linear_in_the_number_of_accounts(self):
        ten = self._dormant_season(10)
        hundred = self._dormant_season(100)
        # Ten times the accounts, ten times the money. Anything else means the
        # accounts interact, which they must not: each one is capped alone.
        assert hundred == ten * 10

    def test_a_dead_server_of_dormant_accounts_stays_inside_the_ceiling(self):
        # 500 accounts nobody has touched since February is a realistic end
        # state for a server in its second season, and the worst case for a
        # faucet that no longer asks anyone to show up.
        assert self._dormant_season(500) < SUPPLY_CEILING

    def test_the_cap_still_binds_per_account_not_per_server(self):
        # A shared cap would make the payout shrink as the server grew, which
        # would quietly punish members for other people joining.
        assert economy.daily_payout(10**9) == economy.DAILY_PAYOUT_CAP
        assert self._dormant_season(2) == self._dormant_season(1) * 2


class TestSeasonStaysBounded:
    def test_a_full_season_does_not_hyperinflate(self):
        result = run_season()
        assert result["supply"] < SUPPLY_CEILING, f"supply reached {result['supply']:,}"
        assert result["richest"] < RICHEST_CEILING, f"richest reached {result['richest']:,}"

    def test_the_result_is_stable_across_seeds(self):
        for seed in (1, 2, 3):
            result = run_season(seed=seed)
            assert result["supply"] < SUPPLY_CEILING
            assert result["richest"] < RICHEST_CEILING

    def test_a_second_season_would_not_run_away_either(self):
        # The economy is reset each January, but it must not depend on that.
        result = run_season(days=SEASON_DAYS * 2)
        assert result["supply"] < SUPPLY_CEILING

    def test_growth_over_the_season_is_linear_not_exponential(self):
        half = run_season(days=SEASON_DAYS // 2)["supply"]
        full = run_season(days=SEASON_DAYS)["supply"]
        # Linear growth doubles when the days double. Exponential growth would
        # square. Allow a wide band; the point is to catch the wrong shape.
        assert 1.2 < full / half < 4.0, f"supply grew {full / half:.1f}x in twice the days"

    def test_more_players_do_not_change_the_shape(self):
        small = run_season(players=6)["richest"]
        large = run_season(players=30)["richest"]
        assert small < RICHEST_CEILING
        assert large < RICHEST_CEILING


class TestTheCapIsWhatHoldsItTogether:
    def test_removing_the_cap_breaks_the_season(self):
        # If this ever stops failing, something else is bounding growth and the
        # cap may no longer be doing the work this suite claims it does.
        uncapped = run_season(days=200, daily_cap=10**18)["supply"]
        capped = run_season(days=200)["supply"]
        assert uncapped > capped * 1_000


class TestTheLotteryDoesNotMintMoney:
    def test_a_bigger_rake_never_multiplies_the_supply(self):
        supplies = {rake: run_season(rake=rake)["supply"] for rake in (0.0, 0.25, 1.0)}
        # A larger rake recycles more of the house edge instead of destroying
        # it, so the supply rises, but it stays the same order of magnitude
        # because the pot is fed by the house's net take rather than by gross
        # losses.
        assert supplies[1.0] < supplies[0.0] * 20
        for supply in supplies.values():
            assert supply < SUPPLY_CEILING

    def test_no_rake_makes_the_casino_a_pure_sink(self):
        assert run_season(rake=0.0)["supply"] < run_season(rake=1.0)["supply"]


class TestTheMarketIsBoundedToo:
    """The market is the only place in this economy where money multiplies.

    The walk mean-reverts and :func:`economy.flx_cost` quotes one price to buyer
    and seller alike, so buying below the anchor and selling above it is a round
    trip that pays every time -- and it pays a percentage of whatever bank the
    member brought, which is the compounding shape the rest of this suite exists
    to catch. :data:`economy.FLX_DAILY_BUY_CAP` is what bounds it.
    """

    def test_a_season_of_band_trading_stays_readable(self):
        result = run_season(traders=3)
        assert result["supply"] < SUPPLY_CEILING, f"supply reached {result['supply']:,}"
        assert result["richest"] < RICHEST_CEILING, f"richest reached {result['richest']:,}"

    def test_trading_growth_is_linear_not_exponential(self):
        half = run_season(days=SEASON_DAYS // 2, traders=3)["richest"]
        full = run_season(days=SEASON_DAYS, traders=3)["richest"]
        assert 1.2 < full / half < 4.0, f"the richest grew {full / half:.1f}x in twice the days"

    def test_the_result_is_stable_across_seeds(self):
        for seed in (1, 2, 3):
            result = run_season(traders=3, seed=seed)
            assert result["supply"] < SUPPLY_CEILING
            assert result["richest"] < RICHEST_CEILING

    def test_more_traders_do_not_change_the_shape(self):
        # Issuance scales with how many members trade, not with how rich they
        # are. That is linear in accounts and capped per account, the same shape
        # the daily interest already has.
        assert run_season(traders=6)["richest"] < RICHEST_CEILING

    def test_the_cap_is_what_holds_it_together(self):
        # If this ever stops failing, something else is bounding the market and
        # the cap may no longer be doing the work this class claims it does.
        uncapped = run_season(days=200, traders=3, flx_cap=10**9)["richest"]
        capped = run_season(days=200, traders=3)["richest"]
        assert uncapped > RICHEST_CEILING, f"an uncapped market only reached {uncapped:,}"
        assert capped < RICHEST_CEILING

    def test_raising_the_cap_raises_the_ceiling_proportionally(self):
        # The cap is a dial rather than a cliff: doubling it roughly doubles
        # what a season of trading returns. That is what makes it safe to retune
        # without re-deriving the whole bound.
        low = run_season(days=120, traders=1, flx_cap=50)["richest"]
        high = run_season(days=120, traders=1, flx_cap=100)["richest"]
        assert 1.5 < high / low < 3.0, f"doubling the cap moved the result {high / low:.1f}x"
