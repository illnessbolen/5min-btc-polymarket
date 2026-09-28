"""Pure decision logic of the "momentum into close" strategy (no I/O, easy to test).

Entry (all must hold):
  1. time: seconds left in the slot is inside the entry window (default 90-150s, i.e. ~2 min left);
  2. impulse: BTC moved at least `btc_move_usd_min` since the slot open; the move picks the side
     (with the filter disabled the side is simply the one the market favours);
  3. skew: the chosen side's best ask is >= `threshold_price` (the crowd agrees with the move)
     and <= `max_entry_price` (reward/risk still acceptable);
  4. execution safety: fresh quotes, bid present, spread and top-of-book liquidity within limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .config import BotConfig
from .exchange import EPS, Book, clamp_price, floor_to, floor_to_tick
from .markets import DOWN, UP
from .pricefeed import BtcSnapshot

ENTER = "enter"
WAIT = "wait"
SKIP = "skip"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    side: Optional[str] = None
    ask: Optional[float] = None
    limit_price: Optional[float] = None
    move_usd: Optional[float] = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def enter(self) -> bool:
        return self.action == ENTER


def evaluate_entry(
    cfg: BotConfig,
    seconds_left: float,
    btc: Optional[BtcSnapshot],
    up: Optional[Book],
    down: Optional[Book],
    now: float,
) -> Decision:
    e = cfg.entry
    info: dict[str, Any] = {"seconds_left": round(seconds_left, 1)}

    if seconds_left > e.window_start_sec_left:
        return Decision(WAIT, "before_window", details=info)
    if seconds_left < e.window_end_sec_left:
        return Decision(SKIP, "window_passed", details=info)

    move: Optional[float] = None
    if btc is not None:
        move = btc.move
        info.update(btc_open=btc.open_price, btc_price=btc.price, btc_source=btc.source)

    if e.btc_move_usd_min > 0:
        if btc is None:
            return Decision(SKIP, "no_btc_price", details=info)
        if now - btc.fetched_at > e.max_quote_age_sec:
            return Decision(SKIP, "btc_price_stale", move_usd=move, details=info)
        if abs(move) < e.btc_move_usd_min:
            return Decision(SKIP, "move_too_small", move_usd=move, details=info)
        side = UP if move > 0 else DOWN
    else:
        # Impulse filter disabled: follow the side the market already favours. Compare marks, not asks:
        # a thin book can show a far-away best ask (seen live: bid 0.47 / ask 0.89).
        up_mark = up.mark if up else None
        down_mark = down.mark if down else None
        if up_mark is None and down_mark is None:
            return Decision(SKIP, "no_quotes", move_usd=move, details=info)
        side = UP if (up_mark or 0) >= (down_mark or 0) else DOWN

    book = up if side == UP else down
    info["side"] = side
    if book is None or book.best_ask is None:
        return Decision(SKIP, "no_ask", side=side, move_usd=move, details=info)
    if now - book.fetched_at > e.max_quote_age_sec:
        return Decision(SKIP, "book_stale", side=side, move_usd=move, details=info)

    ask = book.best_ask
    info.update(ask=ask, bid=book.best_bid, top_ask_notional=round(book.top_ask_notional, 2))
    if ask < e.threshold_price - EPS:
        return Decision(SKIP, "skew_not_confirmed", side=side, ask=ask, move_usd=move, details=info)
    if ask > e.max_entry_price + EPS:
        return Decision(SKIP, "price_above_max", side=side, ask=ask, move_usd=move, details=info)
    if book.best_bid is None:
        return Decision(SKIP, "no_bid", side=side, ask=ask, move_usd=move, details=info)
    if book.spread > e.max_spread + EPS:
        return Decision(SKIP, "spread_too_wide", side=side, ask=ask, move_usd=move, details=info)
    if book.top_ask_notional < e.min_top_ask_notional_usd - EPS:
        return Decision(SKIP, "thin_book", side=side, ask=ask, move_usd=move, details=info)

    limit = buy_limit(ask, e.slippage, book.tick_size, cap=e.max_entry_price)
    return Decision(ENTER, "signal", side=side, ask=ask, limit_price=limit, move_usd=move, details=info)


def compute_stake(cfg: BotConfig, equity: Optional[float]) -> float:
    s = cfg.sizing
    stake = s.stake_usd
    if s.max_notional_usd > 0:
        stake = min(stake, s.max_notional_usd)
    if equity is not None and s.risk_per_trade_pct_equity > 0:
        stake = min(stake, equity * s.risk_per_trade_pct_equity / 100.0)
    return max(0.0, floor_to(stake, 2))


def stop_loss_price(cfg: BotConfig, entry_price: float) -> float:
    return entry_price * (1.0 - cfg.exit.stop_loss_pct)


def stop_loss_hit(cfg: BotConfig, entry_price: float, book: Optional[Book]) -> bool:
    if not cfg.exit.stop_loss_enabled or book is None or book.mark is None:
        return False
    return book.mark <= stop_loss_price(cfg, entry_price) + EPS


def time_exit_due(cfg: BotConfig, seconds_left: float) -> bool:
    return cfg.exit.exit_before_sec > 0 and seconds_left <= cfg.exit.exit_before_sec + EPS


def hedge_due(cfg: BotConfig, seconds_left: float, main_book: Optional[Book]) -> bool:
    h = cfg.hedge
    if not h.enabled or main_book is None or main_book.mark is None:
        return False
    if seconds_left > h.trigger_seconds_left_lte + EPS or time_exit_due(cfg, seconds_left):
        return False
    return main_book.mark >= h.trigger_side_price_gte - EPS


def hedge_notional(cfg: BotConfig, main_cost: float) -> float:
    h = cfg.hedge
    amount = main_cost * h.share_of_main_pct / 100.0
    return floor_to(max(h.notional_usd_min, min(h.notional_usd_max, amount)), 2)


def buy_limit(ask: float, slippage: float, tick: float, cap: float = 1.0) -> float:
    return max(clamp_price(floor_to_tick(min(cap, ask + slippage), tick), tick), ask)


def sell_limit(best_bid: float, attempt: int, slippage: float, tick: float) -> float:
    """Minimum acceptable price for the n-th exit attempt: bid-slippage, bid-2*slippage, then any price."""
    if attempt >= 2:
        return tick
    return clamp_price(floor_to_tick(best_bid - slippage * (attempt + 1), tick), tick)
