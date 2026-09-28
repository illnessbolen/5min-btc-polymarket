from __future__ import annotations

import pytest

from btc5m_bot import strategy as st
from btc5m_bot.markets import DOWN, UP
from btc5m_bot.pricefeed import BtcSnapshot

from conftest import DOWN_T, UP_T, make_book

NOW = 1_800_000_180.0  # 120s left in the slot


def snap(move: float, age: float = 0.0) -> BtcSnapshot:
    return BtcSnapshot("test", 1_800_000_000, 60_000.0, 60_000.0 + move, NOW - age)


def books(up_bid=0.79, up_ask=0.80, down_bid=0.19, down_ask=0.21, size=200.0, age=0.0):
    up = make_book(UP_T, [(up_bid, size)] if up_bid else [], [(up_ask, size)] if up_ask else [], NOW - age)
    down = make_book(DOWN_T, [(down_bid, size)] if down_bid else [], [(down_ask, size)] if down_ask else [], NOW - age)
    return up, down


def decide(cfg, seconds_left=120.0, move=85.0, btc_age=0.0, **book_kw):
    up, down = books(**book_kw)
    return st.evaluate_entry(cfg, seconds_left, snap(move, btc_age) if move is not None else None, up, down, NOW)


def test_enters_with_momentum_and_caps_limit(cfg):
    d = decide(cfg)
    assert d.enter and d.side == UP and d.ask == 0.80
    assert d.limit_price == pytest.approx(0.82)  # ask + 0.02 slippage
    assert d.move_usd == pytest.approx(85.0)


def test_limit_never_exceeds_max_entry_price(cfg):
    d = decide(cfg, up_bid=0.88, up_ask=0.89)
    assert d.enter and d.limit_price == pytest.approx(0.90)


def test_down_move_picks_down(cfg):
    d = decide(cfg, move=-95.0, up_bid=0.19, up_ask=0.21, down_bid=0.79, down_ask=0.80)
    assert d.enter and d.side == DOWN


@pytest.mark.parametrize(
    "kwargs, action, reason",
    [
        ({"seconds_left": 160.0}, st.WAIT, "before_window"),
        ({"seconds_left": 85.0}, st.SKIP, "window_passed"),
        ({"move": None}, st.SKIP, "no_btc_price"),
        ({"btc_age": 9.0}, st.SKIP, "btc_price_stale"),
        ({"move": 69.9}, st.SKIP, "move_too_small"),
        ({"move": -50.0}, st.SKIP, "move_too_small"),
        ({"up_ask": None}, st.SKIP, "no_ask"),
        ({"age": 9.0}, st.SKIP, "book_stale"),
        ({"up_bid": 0.64, "up_ask": 0.65}, st.SKIP, "skew_not_confirmed"),
        ({"up_bid": 0.92, "up_ask": 0.93}, st.SKIP, "price_above_max"),
        ({"up_bid": None}, st.SKIP, "no_bid"),
        ({"up_bid": 0.76}, st.SKIP, "spread_too_wide"),
        ({"size": 10.0}, st.SKIP, "thin_book"),
    ],
)
def test_skip_reasons(cfg, kwargs, action, reason):
    d = decide(cfg, **kwargs)
    assert (d.action, d.reason) == (action, reason)


def test_move_against_market_skew_is_skipped(cfg):
    # BTC is up but the crowd prices DOWN as the favourite: no confirmation.
    d = decide(cfg, move=80.0, up_bid=0.25, up_ask=0.27, down_bid=0.73, down_ask=0.75)
    assert d.reason == "skew_not_confirmed" and d.side == UP


def test_spread_equal_to_limit_passes_despite_float_noise(cfg):
    assert decide(cfg, up_bid=0.77, up_ask=0.80).enter  # 0.80 - 0.77 = 0.030000000000000027


def test_window_bounds_are_inclusive(cfg):
    assert decide(cfg, seconds_left=150.0).enter
    assert decide(cfg, seconds_left=90.0).enter


def test_impulse_filter_disabled_follows_favoured_side(cfg_factory):
    cfg = cfg_factory(entry={"btc_move_usd_min": 0.0})
    d = decide(cfg, move=None, up_bid=0.25, up_ask=0.27, down_bid=0.73, down_ask=0.75)
    assert d.enter and d.side == DOWN
    # A lone far-away ask must not make a side look favoured.
    d = decide(cfg, move=None, up_bid=0.74, up_ask=0.75, down_bid=0.24, down_ask=0.89)
    assert d.enter and d.side == UP
    assert decide(cfg, move=None, up_bid=None, up_ask=None, down_bid=None, down_ask=None).reason == "no_quotes"


def test_compute_stake_caps(cfg_factory):
    cfg = cfg_factory(sizing={"stake_usd": 5.0, "max_notional_usd": 8.0, "risk_per_trade_pct_equity": 8.0})
    assert st.compute_stake(cfg, None) == 5.0
    assert st.compute_stake(cfg, 1000.0) == 5.0
    assert st.compute_stake(cfg, 50.0) == 4.0
    assert st.compute_stake(cfg, 33.33) == 2.66
    big = cfg_factory(sizing={"stake_usd": 20.0, "max_notional_usd": 8.0, "risk_per_trade_pct_equity": 0.0})
    assert st.compute_stake(big, 1000.0) == 8.0


def test_stop_loss(cfg):
    entry = 0.80  # 25% stop -> 0.60
    assert st.stop_loss_price(cfg, entry) == pytest.approx(0.60)
    hit = make_book(UP_T, [(0.59, 10)], [(0.61, 10)], NOW)  # mid 0.60
    miss = make_book(UP_T, [(0.60, 10)], [(0.62, 10)], NOW)  # mid 0.61
    no_ask = make_book(UP_T, [(0.58, 10)], [], NOW)  # mark falls back to the bid
    assert st.stop_loss_hit(cfg, entry, hit)
    assert not st.stop_loss_hit(cfg, entry, miss)
    assert st.stop_loss_hit(cfg, entry, no_ask)
    assert not st.stop_loss_hit(cfg, entry, make_book(UP_T, [], [(0.5, 10)], NOW))


def test_time_exit(cfg, cfg_factory):
    assert st.time_exit_due(cfg, 20.0) and not st.time_exit_due(cfg, 20.5)
    hold = cfg_factory(exit={"exit_before_sec": 0.0})
    assert not st.time_exit_due(hold, 1.0)


def test_hedge_due_and_size(cfg):
    strong = make_book(UP_T, [(0.95, 10)], [(0.96, 10)], NOW)
    weak = make_book(UP_T, [(0.90, 10)], [(0.92, 10)], NOW)
    assert st.hedge_due(cfg, 40.0, strong)
    assert not st.hedge_due(cfg, 50.0, strong)  # too early
    assert not st.hedge_due(cfg, 20.0, strong)  # already exiting
    assert not st.hedge_due(cfg, 40.0, weak)
    assert st.hedge_notional(cfg, 5.0) == 1.0  # 3% of 5 -> clamped to min
    assert st.hedge_notional(cfg, 200.0) == 2.0  # clamped to max


def test_price_ladders():
    assert st.sell_limit(0.95, 0, 0.05, 0.01) == pytest.approx(0.90)
    assert st.sell_limit(0.95, 1, 0.05, 0.01) == pytest.approx(0.85)
    assert st.sell_limit(0.95, 2, 0.05, 0.01) == pytest.approx(0.01)
    assert st.sell_limit(0.03, 0, 0.05, 0.01) == pytest.approx(0.01)
    assert st.buy_limit(0.98, 0.02, 0.01) == pytest.approx(0.99)
    assert st.buy_limit(0.05, 0.02, 0.01) == pytest.approx(0.07)
