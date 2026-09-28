"""Position bookkeeping, persisted bot state (state.json) and the trade journal (trades.jsonl)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .exchange import DUST_SHARES

log = logging.getLogger(__name__)


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Leg:
    role: str  # "main" | "hedge"
    side: str
    token_id: str
    shares: float
    cost: float
    opened_at: float
    order_id: Optional[str] = None
    tx: Optional[str] = None
    estimated: bool = False  # recovered after an uncertain order: cost is an estimate
    closed_shares: float = 0.0
    proceeds: float = 0.0
    exits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.shares - self.closed_shares)

    @property
    def is_flat(self) -> bool:
        return self.remaining < DUST_SHARES

    @property
    def entry_price(self) -> float:
        return self.cost / self.shares if self.shares > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Leg":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Position:
    trade_id: str
    mode: str
    profile: str
    slug: str
    slot_start: int
    end_ts: float
    up_token_id: str
    down_token_id: str
    tick_size: float
    main: Leg
    hedge: Optional[Leg] = None
    hedge_attempts: int = 0
    signal: dict[str, Any] = field(default_factory=dict)
    close_reason: Optional[str] = None
    status: str = "open"  # "open" | "awaiting_resolution"
    last_resolution_check: float = 0.0

    def legs(self) -> list[Leg]:
        return [self.main] + ([self.hedge] if self.hedge else [])

    def token_for(self, side: str) -> str:
        return self.up_token_id if side == "UP" else self.down_token_id

    @property
    def is_flat(self) -> bool:
        return all(leg.is_flat for leg in self.legs())

    @property
    def exposure(self) -> float:
        """Worst-case remaining loss: what is still at risk if unsold shares expire worthless."""
        return sum(max(0.0, leg.cost - leg.proceeds) for leg in self.legs() if not leg.is_flat)

    def pnl(self, winner: Optional[str]) -> Optional[float]:
        total = 0.0
        for leg in self.legs():
            value = leg.proceeds
            if not leg.is_flat:
                if winner is None:
                    return None
                value += leg.remaining * (1.0 if leg.side == winner else 0.0)
            total += value - leg.cost
        return round(total, 6)

    def record(self, winner: Optional[str], pnl: Optional[float], finished_at: float) -> dict[str, Any]:
        m = self.main
        return {
            "trade_id": self.trade_id,
            "mode": self.mode,
            "profile": self.profile,
            "slug": self.slug,
            "side": m.side,
            "opened_at": iso(m.opened_at),
            "finished_at": iso(finished_at),
            "close_reason": self.close_reason,
            "winner": winner,
            "entry_price": round(m.entry_price, 6),
            "cost": round(sum(leg.cost for leg in self.legs()), 6),
            "proceeds": round(sum(leg.proceeds for leg in self.legs()), 6),
            "pnl": pnl,
            "hedged": self.hedge is not None,
            "signal": self.signal,
            "legs": [leg.to_dict() for leg in self.legs()],
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        fields["main"] = Leg.from_dict(data["main"])
        fields["hedge"] = Leg.from_dict(data["hedge"]) if data.get("hedge") else None
        return cls(**fields)


class Journal:
    def __init__(self, root: Path | str, create: bool = True):
        self.root = Path(root)
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.trades_path = self.root / "trades.jsonl"

    def load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except ValueError:
            backup = self.state_path.with_name(f"state.corrupt-{int(time.time())}.json")
            os.replace(self.state_path, backup)
            log.error("state file was corrupt, moved to %s; starting with empty state", backup)
            return {}

    def save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def append_trade(self, record: dict[str, Any]) -> None:
        with self.trades_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_trades(self) -> list[dict[str, Any]]:
        trades = []
        try:
            with self.trades_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trades.append(json.loads(line))
                    except ValueError:
                        log.warning("skipping malformed journal line: %.80s", line)
        except FileNotFoundError:
            pass
        return trades


def summarize(trades: Iterable[dict[str, Any]], since: Optional[str] = None) -> dict[str, Any]:
    """Aggregate journal records; `since` is an ISO date/time prefix filter on finished_at."""
    rows = [t for t in trades if not since or str(t.get("finished_at") or "") >= since]
    pnls = [t["pnl"] for t in rows if isinstance(t.get("pnl"), (int, float))]
    wins = sum(1 for p in pnls if p > 0)
    by_day: dict[str, dict[str, Any]] = {}
    for t in rows:
        day = str(t.get("finished_at") or "")[:10] or "unknown"
        d = by_day.setdefault(day, {"trades": 0, "pnl": 0.0})
        d["trades"] += 1
        if isinstance(t.get("pnl"), (int, float)):
            d["pnl"] = round(d["pnl"] + t["pnl"], 6)
    return {
        "trades": len(rows),
        "with_pnl": len(pnls),
        "wins": wins,
        "losses": sum(1 for p in pnls if p < 0),
        "win_rate": round(wins / len(pnls), 4) if pnls else None,
        "pnl_total": round(sum(pnls), 6) if pnls else 0.0,
        "pnl_avg": round(sum(pnls) / len(pnls), 6) if pnls else None,
        "close_reasons": dict(Counter(str(t.get("close_reason")) for t in rows)),
        "sides": dict(Counter(str(t.get("side")) for t in rows)),
        "by_day": dict(sorted(by_day.items())),
    }
