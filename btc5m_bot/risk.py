"""Daily limits and the error kill switch. Only gates new entries; open positions are always managed."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any, Optional

from .config import BotConfig


def utc_day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


@dataclass
class RiskState:
    day: str = ""
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    day_start_equity: Optional[float] = None
    consecutive_errors: int = 0
    paused_until: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "RiskState":
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


class RiskManager:
    def __init__(self, cfg: BotConfig, state: Optional[RiskState] = None):
        self.cfg = cfg
        self.state = state or RiskState()

    def roll_day(self, now: float, equity: Optional[float]) -> bool:
        """Reset daily counters on a new UTC day. Returns True when a new day started."""
        day = utc_day(now)
        if day != self.state.day:
            self.state.day = day
            self.state.trades_today = 0
            self.state.realized_pnl_today = 0.0
            self.state.day_start_equity = equity
            return True
        if self.state.day_start_equity is None and equity is not None:
            self.state.day_start_equity = equity
        return False

    def block_reason(self, now: float, pending_exposure: float = 0.0) -> Optional[str]:
        """Why a new entry is not allowed right now, or None."""
        s, st = self.cfg.sizing, self.state
        if now < st.paused_until:
            return "error_cooldown"
        if s.max_trades_per_day and st.trades_today >= s.max_trades_per_day:
            return "max_trades_per_day"
        if s.daily_max_loss_pct > 0:
            if st.day_start_equity is None:
                return "equity_unknown"
            limit = st.day_start_equity * s.daily_max_loss_pct / 100.0
            # Unresolved positions count at their full remaining cost until they settle.
            if -st.realized_pnl_today + max(0.0, pending_exposure) >= limit - 1e-9:
                return "daily_loss_limit"
        return None

    def on_trade_opened(self) -> None:
        self.state.trades_today += 1

    def on_trade_closed(self, pnl: Optional[float]) -> None:
        if pnl is not None:
            self.state.realized_pnl_today += pnl

    def on_error(self, now: float) -> bool:
        """Count a failure; returns True when this failure trips the kill switch."""
        self.state.consecutive_errors += 1
        if self.state.consecutive_errors >= self.cfg.runtime.max_consecutive_errors:
            self.state.consecutive_errors = 0
            self.state.paused_until = now + self.cfg.runtime.error_cooldown_sec
            return True
        return False

    def on_success(self) -> None:
        self.state.consecutive_errors = 0
