"""Main trading loop: at most one entry per 5m slot, position management, exits and settlement."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from . import strategy as st
from .config import BotConfig, describe
from .exchange import DUST_SHARES, Fill, OrderUncertain
from .journal import Journal, Leg, Position, iso, summarize
from .markets import SLOT_SEC, Market, opposite, slot_start
from .notifier import Notifier
from .pricefeed import PriceFeedError
from .risk import RiskManager, RiskState, utc_day

log = logging.getLogger(__name__)

CLOSE_MARGIN_SEC = 1.0  # stop trading a market this close to its end
EXIT_RETRY_SEC = 1.0
RESOLUTION_GRACE_SEC = 20.0
RESOLUTION_POLL_SEC = 30.0
RESOLUTION_GIVE_UP_SEC = 6 * 3600.0
EQUITY_TTL_SEC = 30.0
MAX_ENTRY_ORDERS_PER_SLOT = 3
MAX_HEDGE_ORDERS = 2


def _money(x: Optional[float]) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def _is_transient(err: Exception) -> bool:
    """Network/API failures that are expected now and then and need no traceback."""
    return isinstance(err, (requests.RequestException, PriceFeedError, ConnectionError, TimeoutError)) or (
        type(err).__name__ == "PolyApiException"
    )


class Bot:
    def __init__(
        self,
        cfg: BotConfig,
        exchange: Any,
        gamma: Any,
        prices: Any,
        journal: Journal,
        notifier: Optional[Notifier] = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Optional[Callable[[float], Any]] = None,
        retry_sleep: Callable[[float], Any] = time.sleep,
        halt_file: Optional[Path] = None,
        flatten_on_stop: bool = False,
    ):
        self.cfg = cfg
        self.ex = exchange
        self.gamma = gamma
        self.prices = prices
        self.journal = journal
        self.notifier = notifier or Notifier()
        self.clock = clock
        self.stop_event = threading.Event()
        self.paused = threading.Event()
        self._sleep = sleep or self.stop_event.wait  # interruptible idle sleep
        self._retry_sleep = retry_sleep  # short waits inside order flows
        self.halt_file = halt_file
        self.flatten_on_stop = flatten_on_stop
        self.mode = exchange.mode

        state = journal.load_state()
        self.risk = RiskManager(cfg, RiskState.from_dict(state.get("risk")))
        self.position: Optional[Position] = Position.from_dict(state["position"]) if state.get("position") else None
        self.pending: list[Position] = [Position.from_dict(p) for p in state.get("pending") or []]
        self.last_entry_slot = int(state.get("last_entry_slot") or 0)
        if self.mode == "paper" and state.get("paper"):
            exchange.load_dict(state["paper"])

        self._markets: dict[int, Market] = {}
        self._equity: Optional[float] = None
        self._equity_at = float("-inf")  # last balance request (successful or not)
        self._slot: Optional[dict[str, Any]] = None
        self._deadline: Optional[float] = None
        self._notices: dict[str, float] = {}

    # ------------------------------------------------------------------ lifecycle

    def request_stop(self) -> None:
        self.stop_event.set()

    def run(self, duration_sec: Optional[float] = None) -> None:
        self._deadline = self.clock() + duration_sec if duration_sec else None
        self._startup()
        try:
            while not self.stop_event.is_set():
                if self._deadline and self.clock() >= self._deadline and self.position is None:
                    log.info("run duration reached")
                    break
                self._sleep(max(0.05, self.step()))
        finally:
            self._shutdown()

    def _startup(self) -> None:
        equity = self._refresh_equity(force=True)
        self.risk.roll_day(self.clock(), equity)
        log.info("starting %s bot, %s", self.mode, describe(self.cfg))
        extra = ""
        if self.position:
            extra = f"; resuming open position {self.position.slug} {self.position.main.side}"
            log.warning("resuming open position from state: %s", self.position.trade_id)
        if self.pending:
            extra += f"; {len(self.pending)} position(s) awaiting resolution"
        self._notify(f"started: {self.mode.upper()} {self.cfg.profile}, balance {_money(equity)}{extra}")
        self._save_state()

    def _shutdown(self) -> None:
        if self.position is not None and self.flatten_on_stop:
            if self.position.end_ts - self.clock() > CLOSE_MARGIN_SEC:
                try:
                    self._close(self.position, "shutdown")
                except Exception:
                    log.exception("flatten on stop failed")
        self._close_slot_log()
        self._save_state()
        note = f"; open position {self.position.slug} is kept for the next run" if self.position else ""
        self._notify(f"stopped ({self.mode}){note}")

    def step(self) -> float:
        """One loop iteration; returns how long to sleep before the next one."""
        now = self.clock()
        try:
            self._roll_day(now)
            if self.pending:
                try:
                    self._check_pending(now)
                except Exception as e:  # must never block managing the open position
                    log.warning("resolution check failed: %s", e)
            if self.position is not None:
                self._manage_position(now)
                delay = self.cfg.runtime.poll_sec
            else:
                delay = self._seek_entry(now)
            self.risk.on_success()
            return delay
        except Exception as e:
            if _is_transient(e):
                log.warning("step failed: %s", str(e)[:300])
            else:
                log.exception("step failed: %s", e)
            if self.risk.on_error(now):
                r = self.cfg.runtime
                self._notify(f"kill switch: {r.max_consecutive_errors} consecutive errors, "
                             f"new entries paused for {r.error_cooldown_sec:.0f}s (last: {str(e)[:200]})")
            return self.cfg.runtime.poll_sec
        finally:
            self._save_state()

    # ------------------------------------------------------------------ entries

    def _seek_entry(self, now: float) -> float:
        e = self.cfg.entry
        start = slot_start(now)
        left = start + SLOT_SEC - now
        if self.last_entry_slot == start or left < e.window_end_sec_left:
            self._close_slot_log()
            next_window = start + SLOT_SEC + (SLOT_SEC - e.window_start_sec_left)
            return self._idle(next_window - now)
        if left > e.window_start_sec_left:
            return self._idle(left - e.window_start_sec_left)

        slot = self._slot_log(start)
        block = self.entry_block_reason(now)
        if block:
            self._note(slot, block)
            return self._idle(left - e.window_end_sec_left)

        market = self._market(start)
        if market is None:
            self._note(slot, "market_not_found")
            return self.cfg.runtime.poll_sec
        if not market.tradable:
            self._note(slot, "market_not_tradable")
            return self.cfg.runtime.poll_sec

        btc = None
        try:
            btc = self.prices.snapshot(start)
        except PriceFeedError:
            if e.btc_move_usd_min > 0:
                raise
        books = self.ex.get_books([market.up_token_id, market.down_token_id])
        now = self.clock()
        decision = st.evaluate_entry(
            self.cfg, market.seconds_left(now), btc, books.get(market.up_token_id), books.get(market.down_token_id), now
        )
        self._note(slot, decision.reason, decision)
        if decision.enter:
            self._enter(market, decision, slot)
        return self.cfg.runtime.poll_sec

    def entry_block_reason(self, now: float) -> Optional[str]:
        if self.paused.is_set():
            return "paused"
        if self.halt_file is not None and self.halt_file.exists():
            return "halt_file"
        if self._deadline and now >= self._deadline:
            return "run_duration_reached"
        return self.risk.block_reason(now, pending_exposure=sum(p.exposure for p in self.pending))

    def _market(self, start: int) -> Optional[Market]:
        market = self._markets.get(start)
        if market is None:
            market = self.gamma.get_market(start)
            if market is not None and market.tradable:
                self._markets = {start: market}
                self.ex.prepare([market.up_token_id, market.down_token_id])
        return market

    def _enter(self, market: Market, decision: st.Decision, slot: dict[str, Any]) -> None:
        equity = self._refresh_equity()
        stake = st.compute_stake(self.cfg, equity)
        if stake < self.cfg.sizing.min_order_usd:
            self._note(slot, "stake_below_minimum")
            self.last_entry_slot = market.start_ts
            self._notify_throttled("stake_below_minimum", f"skip {market.slug}: stake {_money(stake)} is below the minimum order (balance {_money(equity)})")
            return
        if equity is not None and stake > equity + 1e-9:
            self._note(slot, "insufficient_balance")
            self.last_entry_slot = market.start_ts
            self._notify_throttled("insufficient_balance", f"skip {market.slug}: balance {_money(equity)} is below the stake {_money(stake)}")
            return

        token = market.token_for(decision.side)
        slot["orders"] = slot.get("orders", 0) + 1
        try:
            fill = self.ex.buy(token, stake, decision.limit_price)
        except OrderUncertain as err:
            log.error("entry order outcome unknown (%s); checking balance", err)
            self.last_entry_slot = market.start_ts  # never retry blindly
            fill = self._recover_uncertain_buy(token, decision.limit_price, err)
            if fill is None:
                self._notify(f"entry order on {market.slug} had no confirmation and no position was found: {err}")
                return
        if not fill.ok:
            log.info("entry order not filled: %s", fill.error or fill.status)
            self._note(slot, "order_not_filled")
            if fill.status == "rejected":
                self._notify_throttled("entry_rejected", f"entry order rejected on {market.slug}: {fill.error}")
            if slot["orders"] >= MAX_ENTRY_ORDERS_PER_SLOT:
                self.last_entry_slot = market.start_ts
            return

        now = self.clock()
        self.last_entry_slot = market.start_ts
        slot["entered"] = True
        self.risk.on_trade_opened()
        self.position = Position(
            trade_id=f"{market.start_ts}-{uuid.uuid4().hex[:6]}",
            mode=self.mode,
            profile=self.cfg.profile,
            slug=market.slug,
            slot_start=market.start_ts,
            end_ts=market.end_ts,
            up_token_id=market.up_token_id,
            down_token_id=market.down_token_id,
            tick_size=market.tick_size,
            main=Leg(
                role="main",
                side=decision.side,
                token_id=token,
                shares=fill.shares,
                cost=fill.usdc,
                opened_at=now,
                order_id=fill.order_id,
                tx=fill.tx,
                estimated=fill.status == "recovered",
            ),
            signal={**decision.details, "move_usd": decision.move_usd, "limit_price": decision.limit_price, "stake": stake},
        )
        self._invalidate_equity()
        self._save_state()
        move = f", BTC {decision.move_usd:+.0f}$" if decision.move_usd is not None else ""
        self._notify(
            f"ENTRY {decision.side} {market.slug}: {fill.shares:.2f} sh @ {fill.avg_price:.3f} = {_money(fill.usdc)}"
            f"{move}, {market.seconds_left(now):.0f}s left"
        )

    def _recover_uncertain_buy(self, token_id: str, limit_price: float, err: Exception) -> Optional[Fill]:
        for _ in range(5):
            self._retry_sleep(2.0)
            try:
                balance = self.ex.get_share_balance(token_id, refresh=True)
            except Exception as e:
                log.warning("balance check failed: %s", e)
                continue
            if balance >= DUST_SHARES:
                log.warning("found %.4f shares after an uncertain order; adopting them", balance)
                return Fill(True, balance, balance * limit_price, status="recovered", error=str(err))
        return None

    # ------------------------------------------------------------------ open position

    def _manage_position(self, now: float) -> None:
        pos = self.position
        left = pos.end_ts - now
        if left <= CLOSE_MARGIN_SEC:
            self._await_resolution(pos, pos.close_reason or ("exit_failed" if self.cfg.exit.exit_before_sec > 0 else "hold"))
            return
        if st.time_exit_due(self.cfg, left):
            self._close(pos, "time_exit")
            return
        book = self.ex.get_book(pos.main.token_id)
        if st.stop_loss_hit(self.cfg, pos.main.entry_price, book):
            log.info("stop-loss: mark %.3f <= %.3f", book.mark, st.stop_loss_price(self.cfg, pos.main.entry_price))
            self._close(pos, "stop_loss")
            return
        if pos.hedge is None and pos.hedge_attempts < MAX_HEDGE_ORDERS and st.hedge_due(self.cfg, left, book):
            self._open_hedge(pos, left)

    def _open_hedge(self, pos: Position, left: float) -> None:
        pos.hedge_attempts += 1
        side = opposite(pos.main.side)
        token = pos.token_for(side)
        amount = st.hedge_notional(self.cfg, pos.main.cost)
        book = self.ex.get_book(token)
        if book.best_ask is None:
            log.info("hedge skipped: no asks on %s", side)
            return
        limit = st.buy_limit(book.best_ask, self.cfg.entry.slippage, book.tick_size)
        try:
            fill = self.ex.buy(token, amount, limit)
        except OrderUncertain as err:
            pos.hedge_attempts = MAX_HEDGE_ORDERS
            fill = self._recover_uncertain_buy(token, limit, err)
            if fill is None:
                return
        if not fill.ok:
            log.info("hedge order not filled: %s", fill.error or fill.status)
            return
        pos.hedge = Leg(
            role="hedge",
            side=side,
            token_id=token,
            shares=fill.shares,
            cost=fill.usdc,
            opened_at=self.clock(),
            order_id=fill.order_id,
            tx=fill.tx,
            estimated=fill.status == "recovered",
        )
        self._save_state()
        self._notify(f"HEDGE {side} {pos.slug}: {fill.shares:.2f} sh @ {fill.avg_price:.3f} = {_money(fill.usdc)}, {left:.0f}s left")

    def _close(self, pos: Position, reason: str) -> None:
        pos.close_reason = pos.close_reason or reason
        log.info("closing %s (%s)", pos.trade_id, reason)
        for leg in pos.legs():
            if not leg.is_flat:
                self._exit_leg(pos, leg)
        if pos.is_flat:
            self.position = None
            self._finish(pos, winner=None)
        else:
            self._await_resolution(pos, reason)

    def _exit_leg(self, pos: Position, leg: Leg) -> None:
        """Sell a leg with FAK orders, widening the limit on each retry, until the market is about to close."""
        deadline = pos.end_ts - CLOSE_MARGIN_SEC
        reads = 0  # balance reads; refresh the exchange cache after the first one
        sells = 0  # sell orders sent; drives the widening price ladder
        zero_reads = 0
        uncertain: Optional[tuple[float, float]] = None  # (qty, min_price) of a sell with unknown outcome
        while not leg.is_flat and self.clock() < deadline:
            fill = None
            qty = leg.remaining
            min_price = 0.0
            try:
                balance = self.ex.get_share_balance(leg.token_id, refresh=reads > 0)
                reads += 1
                if balance < DUST_SHARES:
                    zero_reads += 1
                    if uncertain is not None and zero_reads >= 2:
                        qty, price = uncertain
                        log.warning("shares gone after an uncertain sell; assuming %.4f sold at >= %.3f", qty, price)
                        leg.closed_shares += qty
                        leg.proceeds += qty * price
                        leg.exits.append({"ts": iso(self.clock()), "status": "assumed_sold", "shares": qty, "min_price": price})
                        uncertain = None
                        continue
                    log.info("waiting for %s shares to become sellable (read %d)", leg.role, zero_reads)
                    self._retry_sleep(EXIT_RETRY_SEC)
                    continue
                zero_reads = 0
                uncertain = None  # shares are still there: an uncertain sell did not go through
                if balance < leg.remaining - DUST_SHARES:
                    log.warning("%s balance %.4f is below the recorded %.4f; reconciling", leg.role, balance, leg.remaining)
                    leg.shares = leg.closed_shares + balance
                qty = min(leg.remaining, balance)
                book = self.ex.get_book(leg.token_id)
                if book.best_bid is None:
                    log.info("no bids for %s leg, retrying", leg.role)
                else:
                    min_price = st.sell_limit(book.best_bid, sells, self.cfg.exit.slippage, book.tick_size)
                    sells += 1
                    fill = self.ex.sell(leg.token_id, qty, min_price)
            except OrderUncertain as err:
                log.error("exit order outcome unknown: %s", err)
                uncertain = (qty, min_price)
            except Exception as err:
                log.warning("exit attempt failed: %s", err)
            if fill is not None:
                leg.exits.append({"ts": iso(self.clock()), "reason": pos.close_reason, "min_price": min_price, **fill.summary()})
                if fill.ok:
                    leg.closed_shares += fill.shares
                    leg.proceeds += fill.usdc
                    self._save_state()
                    continue
            self._retry_sleep(EXIT_RETRY_SEC)
        if not leg.is_flat:
            log.error("%s leg not fully sold before the close: %.4f shares left", leg.role, leg.remaining)

    # ------------------------------------------------------------------ settlement

    def _await_resolution(self, pos: Position, reason: str) -> None:
        pos.status = "awaiting_resolution"
        pos.close_reason = pos.close_reason or reason
        self.pending.append(pos)
        if self.position is pos:
            self.position = None
        held = ", ".join(f"{leg.remaining:.2f} {leg.side}" for leg in pos.legs() if not leg.is_flat)
        self._notify(f"holding {held} on {pos.slug} to resolution ({pos.close_reason})")

    def _check_pending(self, now: float) -> None:
        for pos in list(self.pending):
            if now < pos.end_ts + RESOLUTION_GRACE_SEC or now - pos.last_resolution_check < RESOLUTION_POLL_SEC:
                continue
            pos.last_resolution_check = now
            winner = self.gamma.get_resolution(pos.slug)
            if winner is None:
                if now - pos.end_ts > RESOLUTION_GIVE_UP_SEC:
                    log.error("no resolution for %s after %.0fh, recording without PnL", pos.slug, RESOLUTION_GIVE_UP_SEC / 3600)
                    self.pending.remove(pos)
                    self._finish(pos, winner=None)
                continue
            self.pending.remove(pos)
            self._finish(pos, winner)

    def _finish(self, pos: Position, winner: Optional[str]) -> None:
        pnl = pos.pnl(winner)
        redeem = False
        if winner is not None:
            for leg in pos.legs():
                if not leg.is_flat:
                    payout = 1.0 if leg.side == winner else 0.0
                    redeem = redeem or payout > 0
                    self.ex.settle(leg.token_id, payout)
        self.journal.append_trade(pos.record(winner, pnl, self.clock()))
        self.risk.on_trade_closed(pnl)
        self._invalidate_equity()
        self._save_state()
        label = "n/a" if pnl is None else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT")
        pnl_text = "unknown" if pnl is None else f"{pnl:+.2f} USDC"
        tail = f", resolved {winner}" if winner else ""
        if redeem and self.mode == "live":
            tail += " (redeem the winning shares on polymarket.com)"
        day = self.risk.state
        self._notify(
            f"{label} {pos.main.side} {pos.slug}: PnL {pnl_text} ({pos.close_reason}{tail}); "
            f"today {day.trades_today} trades, {day.realized_pnl_today:+.2f} USDC"
        )

    # ------------------------------------------------------------------ helpers

    def _roll_day(self, now: float) -> None:
        state = self.risk.state
        if state.day == utc_day(now) and state.day_start_equity is not None:
            return
        prev_day, prev_trades, prev_pnl = state.day, state.trades_today, state.realized_pnl_today
        new_day = state.day != utc_day(now)
        if self.risk.roll_day(now, self._refresh_equity(force=new_day)) and prev_day:
            self._notify(f"day {prev_day} closed: {prev_trades} trades, PnL {prev_pnl:+.2f} USDC")

    def _refresh_equity(self, force: bool = False) -> Optional[float]:
        now = self.clock()
        if not force and now - self._equity_at < EQUITY_TTL_SEC:
            return self._equity
        self._equity_at = now  # also throttles retries while the balance endpoint fails
        try:
            self._equity = float(self.ex.get_usdc_balance())
        except Exception as e:
            log.warning("balance request failed: %s", e)
        return self._equity

    def _invalidate_equity(self) -> None:
        self._equity_at = float("-inf")

    def _idle(self, seconds: float) -> float:
        return max(0.2, min(self.cfg.runtime.idle_poll_sec, seconds))

    def _slot_log(self, start: int) -> dict[str, Any]:
        if self._slot is None or self._slot["slot"] != start:
            self._close_slot_log()
            self._slot = {"slot": start, "reasons": Counter(), "last": None, "max_move": None}
        return self._slot

    def _note(self, slot: dict[str, Any], reason: str, decision: Optional[st.Decision] = None) -> None:
        slot["reasons"][reason] += 1
        if decision is not None and decision.move_usd is not None:
            slot["max_move"] = max(slot["max_move"] or 0.0, abs(decision.move_usd))
        if reason != slot["last"]:
            slot["last"] = reason
            details = " ".join(f"{k}={v}" for k, v in (decision.details if decision else {}).items())
            log.info("slot %s: %s %s", slot["slot"], reason, details)

    def _close_slot_log(self) -> None:
        slot, self._slot = self._slot, None
        if slot is None:
            return
        outcome = "entered" if slot.get("entered") else "no entry"
        reasons = ", ".join(f"{k} x{v}" for k, v in slot["reasons"].most_common())
        move = "n/a" if slot["max_move"] is None else f"${slot['max_move']:.0f}"
        log.info("slot %s summary: %s; max |BTC move| %s; %s", slot["slot"], outcome, move, reasons)

    def _notify(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except Exception:
            log.exception("notification failed")

    def _notify_throttled(self, key: str, text: str, every_sec: float = 3600.0) -> None:
        now = self.clock()
        if now - self._notices.get(key, float("-inf")) >= every_sec:
            self._notices[key] = now
            self._notify(text)
        else:
            log.info("%s", text)

    def _save_state(self) -> None:
        state: dict[str, Any] = {
            "version": 1,
            "mode": self.mode,
            "profile": self.cfg.profile,
            "updated_at": iso(self.clock()),
            "risk": self.risk.state.to_dict(),
            "position": self.position.to_dict() if self.position else None,
            "pending": [p.to_dict() for p in self.pending],
            "last_entry_slot": self.last_entry_slot,
        }
        if self.mode == "paper":
            state["paper"] = self.ex.to_dict()
        try:
            self.journal.save_state(state)
        except OSError:
            log.exception("could not save state")

    # ------------------------------------------------------------------ status / commands

    def status_text(self) -> str:
        now = self.clock()
        s = self.risk.state
        lines = [
            f"{self.mode.upper()} {self.cfg.profile}, {'paused' if self.paused.is_set() else 'running'}",
            f"day {s.day}: {s.trades_today} trades, PnL {s.realized_pnl_today:+.2f} USDC, balance {_money(self._equity)}",
        ]
        pos = self.position
        if pos is not None:
            lines.append(
                f"open: {pos.main.side} {pos.slug} {pos.main.remaining:.2f} sh @ {pos.main.entry_price:.3f}, "
                f"{pos.end_ts - now:.0f}s left" + (", hedged" if pos.hedge else "")
            )
        if self.pending:
            lines.append(f"awaiting resolution: {', '.join(p.slug for p in self.pending)}")
        block = self.entry_block_reason(now)
        if block:
            lines.append(f"new entries blocked: {block}")
        return "\n".join(lines)

    def handle_command(self, command: str) -> str:
        if command in ("/status", "/start"):
            return self.status_text()
        if command == "/pause":
            self.paused.set()
            return "paused: no new entries (an open position is still managed)"
        if command == "/resume":
            self.paused.clear()
            return "resumed"
        if command == "/report":
            s = summarize(self.journal.load_trades(), since=utc_day(self.clock()))
            win_rate = "n/a" if s["win_rate"] is None else f"{s['win_rate']:.0%}"
            return f"today: {s['trades']} trades, {s['wins']}W/{s['losses']}L ({win_rate}), PnL {s['pnl_total']:+.2f} USDC"
        return "commands: /status /pause /resume /report"
