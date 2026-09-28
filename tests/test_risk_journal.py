from __future__ import annotations

import pytest

from btc5m_bot.journal import Journal, Leg, Position, summarize
from btc5m_bot.risk import RiskManager, RiskState

DAY1 = 1_800_000_000  # 2027-01-15
DAY2 = DAY1 + 86_400


def test_daily_limits(cfg):
    risk = RiskManager(cfg)
    assert risk.roll_day(DAY1, 100.0)
    assert risk.block_reason(DAY1) is None
    for _ in range(cfg.sizing.max_trades_per_day):
        risk.on_trade_opened()
    assert risk.block_reason(DAY1) == "max_trades_per_day"
    assert risk.roll_day(DAY2, 90.0) and risk.block_reason(DAY2) is None

    risk.on_trade_closed(-6.0)
    assert risk.block_reason(DAY2) is None
    assert risk.block_reason(DAY2, pending_exposure=3.0) == "daily_loss_limit"  # 6 + 3 >= 10% of 90
    risk.on_trade_closed(-3.0)
    assert risk.block_reason(DAY2) == "daily_loss_limit"
    risk.on_trade_closed(None)
    assert risk.state.realized_pnl_today == pytest.approx(-9.0)


def test_unknown_equity_blocks_when_loss_limit_enabled(cfg):
    risk = RiskManager(cfg)
    risk.roll_day(DAY1, None)
    assert risk.block_reason(DAY1) == "equity_unknown"
    risk.roll_day(DAY1 + 10, 50.0)
    assert risk.block_reason(DAY1 + 10) is None


def test_kill_switch(cfg):
    risk = RiskManager(cfg, RiskState(day="x"))
    assert not risk.on_error(DAY1)
    risk.on_success()
    assert not risk.on_error(DAY1) and not risk.on_error(DAY1)
    assert risk.on_error(DAY1)  # third consecutive
    assert risk.state.paused_until == DAY1 + cfg.runtime.error_cooldown_sec
    assert risk.block_reason(DAY1 + 1) == "error_cooldown"
    assert RiskState.from_dict(risk.state.to_dict() | {"junk": 1}) == risk.state


def position(**main_kw):
    main = Leg(role="main", side="UP", token_id="u", shares=10.0, cost=8.0, opened_at=DAY1, **main_kw)
    return Position(
        trade_id="t1", mode="paper", profile="conservative", slug="btc-updown-5m-1", slot_start=DAY1,
        end_ts=DAY1 + 300, up_token_id="u", down_token_id="d", tick_size=0.01, main=main,
    )


def test_position_pnl():
    pos = position()
    assert pos.pnl(None) is None and pos.exposure == 8.0
    assert pos.pnl("UP") == pytest.approx(2.0) and pos.pnl("DOWN") == pytest.approx(-8.0)
    pos.main.closed_shares, pos.main.proceeds = 10.0, 9.5
    assert pos.is_flat and pos.pnl(None) == pytest.approx(1.5) and pos.exposure == 0
    pos.hedge = Leg(role="hedge", side="DOWN", token_id="d", shares=20.0, cost=1.0, opened_at=DAY1)
    assert pos.pnl("DOWN") == pytest.approx(1.5 - 1.0 + 20.0)
    assert Position.from_dict(pos.to_dict()) == pos


def test_journal_state_and_trades(tmp_path):
    j = Journal(tmp_path)
    assert j.load_state() == {}
    j.save_state({"a": 1})
    assert j.load_state() == {"a": 1}
    j.state_path.write_text("{broken", encoding="utf-8")
    assert j.load_state() == {}
    assert list(tmp_path.glob("state.corrupt-*.json"))

    pos = position()
    pos.close_reason = "time_exit"
    j.append_trade(pos.record("UP", 2.0, DAY1 + 280))
    with j.trades_path.open("a") as f:
        f.write("not json\n")
    j.append_trade(pos.record("DOWN", -8.0, DAY2))
    trades = j.load_trades()
    assert len(trades) == 2 and trades[0]["finished_at"] == "2027-01-15T08:04:40Z"

    s = summarize(trades)
    assert (s["trades"], s["wins"], s["losses"], s["pnl_total"], s["win_rate"]) == (2, 1, 1, -6.0, 0.5)
    assert summarize(trades, since="2027-01-16")["trades"] == 1
