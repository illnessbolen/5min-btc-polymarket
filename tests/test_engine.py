from __future__ import annotations

import pytest

from btc5m_bot.engine import Bot
from btc5m_bot.exchange import Fill, OrderUncertain, PaperExchange
from btc5m_bot.journal import Journal
from btc5m_bot.markets import UP, slot_slug

from conftest import DOWN_T, SLOT, UP_T, FakeClock, FakeGamma, FakePrices, ScriptedBooks

S = SLOT  # slot start; the slot ends at S + 300


class RecordingNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)

    def close(self) -> None:
        pass


def base_books(clock: FakeClock) -> ScriptedBooks:
    return ScriptedBooks(
        clock,
        {
            UP_T: [(0, [(0.79, 500)], [(0.80, 500), (0.81, 500)])],
            DOWN_T: [(0, [(0.19, 500)], [(0.21, 500)])],
        },
    )


def make_bot(cfg, tmp_path, clock, books, prices=None, gamma=None, exchange=None, **kw):
    notifier = RecordingNotifier()
    bot = Bot(
        cfg,
        exchange or PaperExchange(books, cash=100.0),
        gamma or FakeGamma(),
        prices or FakePrices(clock),
        Journal(tmp_path / "paper"),
        notifier,
        clock=clock,
        sleep=clock.sleep,
        retry_sleep=clock.sleep,
        **kw,
    )
    return bot, notifier


def drive(bot: Bot, clock: FakeClock, until: float) -> None:
    while clock() < until:
        clock.sleep(max(0.05, bot.step()))


def trades(tmp_path):
    return Journal(tmp_path / "paper").load_trades()


def test_full_cycle_entry_hedge_and_pre_close_exit(cfg, tmp_path):
    clock = FakeClock(S + 100)  # 200s left: before the entry window
    books = base_books(clock)
    books.set(UP_T, S + 200, [(0.90, 500)], [(0.91, 500)])
    books.set(UP_T, S + 258, [(0.96, 500)], [(0.97, 500)])  # mark 0.965 -> hedge after 45s left
    books.set(DOWN_T, S + 258, [(0.03, 500)], [(0.04, 500)])
    bot, notes = make_bot(cfg, tmp_path, clock, books)
    bot._startup()

    drive(bot, clock, S + 151)
    pos = bot.position
    assert pos is not None and pos.main.side == UP
    assert 149 <= pos.end_ts - pos.main.opened_at <= 150  # entered as soon as the window opened
    assert pos.main.cost == pytest.approx(5.0)
    assert pos.main.shares == pytest.approx(5.0 / 0.80)

    drive(bot, clock, S + 300)
    assert bot.position is None and not bot.pending
    [t] = trades(tmp_path)
    assert t["close_reason"] == "time_exit" and t["hedged"] is True
    main, hedge = t["legs"]
    assert hedge["side"] == "DOWN" and hedge["cost"] == pytest.approx(1.0)
    assert main["proceeds"] == pytest.approx(6.25 * 0.96)
    assert hedge["proceeds"] == pytest.approx(25 * 0.03)
    assert t["pnl"] == pytest.approx(6.25 * 0.96 + 0.75 - 6.0)
    assert bot.ex.positions == {}
    assert bot.ex.cash == pytest.approx(100 + t["pnl"])
    assert bot.risk.state.trades_today == 1
    assert any(m.startswith("ENTRY UP") for m in notes.messages)
    assert any(m.startswith("HEDGE DOWN") for m in notes.messages)
    assert any(m.startswith("WIN UP") for m in notes.messages)


def test_one_entry_per_slot_and_next_slot_trades_again(cfg, tmp_path):
    clock = FakeClock(S + 140)
    books = base_books(clock)
    books.set(UP_T, S + 170, [(0.50, 500)], [(0.52, 500)])  # stop-loss right after entry
    books.set(UP_T, S + 300, [(0.79, 500)], [(0.80, 500)])  # next slot back to normal
    bot, _ = make_bot(cfg, tmp_path, clock, books)
    bot._startup()

    drive(bot, clock, S + 300)
    [t] = trades(tmp_path)
    assert t["close_reason"] == "stop_loss" and t["pnl"] < 0
    assert bot.last_entry_slot == S  # no re-entry after the stop in the same slot

    drive(bot, clock, S + 300 + 160)
    assert bot.position is not None and bot.position.slot_start == S + 300


def test_no_entry_without_impulse(cfg, tmp_path):
    clock = FakeClock(S + 100)
    bot, _ = make_bot(cfg, tmp_path, clock, base_books(clock), prices=FakePrices(clock, move=40.0))
    bot._startup()
    drive(bot, clock, S + 300)
    assert bot.position is None and trades(tmp_path) == []


def test_hold_to_resolution_settles_paper_balance(cfg_factory, tmp_path):
    cfg = cfg_factory(exit={"exit_before_sec": 0.0}, hedge={"enabled": False})
    clock = FakeClock(S + 140)
    gamma = FakeGamma()
    bot, notes = make_bot(cfg, tmp_path, clock, base_books(clock), gamma=gamma)
    bot._startup()

    drive(bot, clock, S + 310)
    assert bot.position is None and len(bot.pending) == 1
    assert trades(tmp_path) == []
    assert bot.entry_block_reason(clock()) is None  # $5 exposure is below the 10% daily limit

    gamma.resolutions[slot_slug(S)] = UP
    drive(bot, clock, S + 360)
    [t] = trades(tmp_path)
    assert t["winner"] == UP and t["pnl"] == pytest.approx(6.25 - 5.0)
    assert bot.ex.cash == pytest.approx(101.25)
    assert not bot.pending


def test_daily_trade_limit_blocks_new_entries(cfg_factory, tmp_path):
    cfg = cfg_factory(sizing={"max_trades_per_day": 1})
    clock = FakeClock(S + 140)
    bot, _ = make_bot(cfg, tmp_path, clock, base_books(clock))
    bot._startup()
    drive(bot, clock, S + 300 + 200)
    assert len(trades(tmp_path)) == 1
    assert bot.position is None
    assert bot.entry_block_reason(clock()) == "max_trades_per_day"


def test_kill_switch_pauses_entries_after_consecutive_errors(cfg, tmp_path):
    clock = FakeClock(S + 150)
    prices = FakePrices(clock)
    prices.fail = 3
    bot, notes = make_bot(cfg, tmp_path, clock, base_books(clock), prices=prices)
    bot._startup()
    drive(bot, clock, S + 300)
    assert bot.position is None and trades(tmp_path) == []
    assert any("kill switch" in m for m in notes.messages)
    assert bot.risk.state.paused_until == pytest.approx(S + 152 + 300, abs=1)


def test_pause_command_and_status(cfg, tmp_path):
    clock = FakeClock(S + 140)
    bot, _ = make_bot(cfg, tmp_path, clock, base_books(clock))
    bot._startup()
    assert "paused" in bot.handle_command("/pause")
    drive(bot, clock, S + 300)
    assert trades(tmp_path) == [] and bot.position is None
    assert "paused" in bot.handle_command("/status")
    bot.handle_command("/resume")
    drive(bot, clock, S + 300 + 160)
    assert bot.position is not None
    assert "open: UP" in bot.handle_command("/status")
    assert bot.handle_command("/help").startswith("commands")


def test_halt_file_blocks_entries(cfg, tmp_path):
    clock = FakeClock(S + 140)
    halt = tmp_path / "HALT"
    halt.write_text("")
    bot, _ = make_bot(cfg, tmp_path, clock, base_books(clock), halt_file=halt)
    bot._startup()
    drive(bot, clock, S + 300)
    assert trades(tmp_path) == [] and bot.position is None


def test_restart_resumes_open_position(cfg, tmp_path):
    clock = FakeClock(S + 140)
    books = base_books(clock)
    books.set(UP_T, S + 250, [(0.93, 500)], [(0.94, 500)])
    exchange = PaperExchange(books, cash=100.0)
    bot, _ = make_bot(cfg, tmp_path, clock, books, exchange=exchange)
    bot._startup()
    drive(bot, clock, S + 160)
    assert bot.position is not None
    bot._shutdown()

    # A fresh process: state (position and paper balances) comes from state.json.
    bot2, notes2 = make_bot(cfg, tmp_path, clock, books, exchange=PaperExchange(books, cash=100.0))
    assert bot2.position is not None and bot2.ex.positions
    bot2._startup()
    assert any("resuming" in m for m in notes2.messages)
    drive(bot2, clock, S + 300)
    [t] = trades(tmp_path)
    assert t["close_reason"] == "time_exit" and t["pnl"] == pytest.approx(6.25 * 0.93 - 5.0)


def test_uncertain_entry_is_adopted_from_balance(cfg, tmp_path):
    clock = FakeClock(S + 140)
    books = base_books(clock)

    class Flaky(PaperExchange):
        def buy(self, token_id, usdc, limit_price):
            super().buy(token_id, usdc, limit_price)  # the order did reach the book...
            raise OrderUncertain("timeout")  # ...but the response was lost

    bot, _ = make_bot(cfg, tmp_path, clock, books, exchange=Flaky(books, cash=100.0))
    bot._startup()
    drive(bot, clock, S + 165)
    pos = bot.position
    assert pos is not None and pos.main.estimated
    assert pos.main.shares == pytest.approx(6.25)
    assert pos.main.cost == pytest.approx(6.25 * 0.82)  # estimated at the limit price
    assert bot.last_entry_slot == S


def test_exit_waits_for_settled_balance_and_reconciles(cfg_factory, tmp_path):
    cfg = cfg_factory(hedge={"enabled": False})
    clock = FakeClock(S + 140)
    books = base_books(clock)
    books.set(UP_T, S + 250, [(0.95, 500)], [(0.99, 500)])

    class Lagging(PaperExchange):
        hidden_reads = 2

        def get_share_balance(self, token_id, refresh=False):
            if token_id == UP_T and self.hidden_reads > 0:
                self.hidden_reads -= 1
                return 0.0
            bal = super().get_share_balance(token_id)
            return bal * 0.99 if token_id == UP_T else bal  # a fee trimmed the received shares

    bot, _ = make_bot(cfg, tmp_path, clock, books, exchange=Lagging(books, cash=100.0))
    bot._startup()
    drive(bot, clock, S + 300)
    [t] = trades(tmp_path)
    main = t["legs"][0]
    assert main["shares"] == pytest.approx(6.25 * 0.99, abs=0.011)
    assert t["close_reason"] == "time_exit" and t["pnl"] == pytest.approx(main["proceeds"] - 5.0)


def test_failed_exit_falls_back_to_resolution(cfg, tmp_path):
    clock = FakeClock(S + 140)
    books = base_books(clock)
    books.set(UP_T, S + 250, [], [(0.99, 500)])  # nobody bids into the close
    gamma = FakeGamma()
    bot, notes = make_bot(cfg, tmp_path, clock, books, gamma=gamma)
    bot._startup()
    drive(bot, clock, S + 300)
    assert bot.position is None and len(bot.pending) == 1
    assert bot.pending[0].close_reason == "time_exit"
    gamma.resolutions[slot_slug(S)] = "DOWN"
    drive(bot, clock, S + 360)
    [t] = trades(tmp_path)
    assert t["winner"] == "DOWN" and t["pnl"] == pytest.approx(-5.0)


def test_rejected_entry_is_retried_at_most_three_times(cfg, tmp_path):
    clock = FakeClock(S + 140)
    books = base_books(clock)

    class Rejecting(PaperExchange):
        calls = 0

        def buy(self, token_id, usdc, limit_price):
            self.calls += 1
            return Fill(False, 0.0, 0.0, status="rejected", error="not enough balance / allowance")

    ex = Rejecting(books, cash=100.0)
    bot, notes = make_bot(cfg, tmp_path, clock, books, exchange=ex)
    bot._startup()
    drive(bot, clock, S + 300)
    assert ex.calls == 3 and bot.position is None
    assert sum("rejected" in m for m in notes.messages) == 1  # throttled


def test_duration_limit_stops_the_loop(cfg, tmp_path):
    clock = FakeClock(S + 100)
    bot, notes = make_bot(cfg, tmp_path, clock, base_books(clock), prices=FakePrices(clock, move=10.0))
    bot.run(duration_sec=600)
    assert clock() >= S + 700
    assert notes.messages[0].startswith("started") and notes.messages[-1].startswith("stopped")


def test_resolution_errors_do_not_block_the_open_position(cfg_factory, tmp_path):
    cfg = cfg_factory(hedge={"enabled": False})
    clock = FakeClock(S + 140)
    books = base_books(clock)
    books.set(UP_T, S + 250, [], [(0.99, 500)])  # no bids into the close: slot S is held to resolution
    books.set(UP_T, S + 300, [(0.79, 500)], [(0.80, 500)])
    books.set(UP_T, S + 550, [(0.93, 500)], [(0.94, 500)])

    class BrokenGamma(FakeGamma):
        def get_resolution(self, slug):
            raise ConnectionError("gamma down")

    bot, _ = make_bot(cfg, tmp_path, clock, books, gamma=BrokenGamma())
    bot._startup()
    drive(bot, clock, S + 600)
    [t] = trades(tmp_path)
    assert t["slug"] == slot_slug(S + 300) and t["close_reason"] == "time_exit"
    assert [p.slug for p in bot.pending] == [slot_slug(S)]


def test_balance_failures_are_throttled_and_block_entries(cfg, tmp_path):
    clock = FakeClock(S + 100)
    books = base_books(clock)

    class NoBalance(PaperExchange):
        calls = 0

        def get_usdc_balance(self):
            self.calls += 1
            raise RuntimeError("balance endpoint down")

    ex = NoBalance(books, cash=100.0)
    bot, _ = make_bot(cfg, tmp_path, clock, books, exchange=ex)
    bot._startup()
    drive(bot, clock, S + 300)
    assert ex.calls <= 9  # about one request per 30s, not one per loop iteration
    assert bot.position is None and bot.entry_block_reason(clock()) == "equity_unknown"


def test_slot_summary_reports_entries_only_after_a_fill(cfg, tmp_path, caplog):
    clock = FakeClock(S + 140)
    books = base_books(clock)

    class Broke(PaperExchange):
        def get_usdc_balance(self):
            return 3.0  # 8% of $3 = $0.24, below the $1 minimum order

    bot, notes = make_bot(cfg, tmp_path, clock, books, exchange=Broke(books, cash=3.0))
    bot._startup()
    with caplog.at_level("INFO"):
        drive(bot, clock, S + 300)
    assert bot.position is None and bot.last_entry_slot == S
    assert any("stake $0.24 is below the minimum order" in m for m in notes.messages)
    assert "summary: no entry" in caplog.text
