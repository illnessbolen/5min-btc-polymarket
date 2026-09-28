from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btc5m_bot.config import load_config  # noqa: E402
from btc5m_bot.exchange import Book, Level  # noqa: E402
from btc5m_bot.markets import Market, slot_slug  # noqa: E402
from btc5m_bot.pricefeed import BtcSnapshot, PriceFeedError  # noqa: E402

SLOT = 1_800_000_000  # a 5m slot boundary
UP_T = "111"
DOWN_T = "222"


class FakeClock:
    def __init__(self, now: float):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def make_book(token_id: str, bids, asks, now: float, tick: float = 0.01) -> Book:
    return Book(
        token_id=token_id,
        bids=tuple(Level(p, s) for p, s in sorted(bids, reverse=True)),
        asks=tuple(Level(p, s) for p, s in sorted(asks)),
        tick_size=tick,
        min_order_size=5,
        fetched_at=now,
    )


class ScriptedBooks:
    """Order books that change over time: token -> [(from_ts, bids, asks), ...]."""

    def __init__(self, clock: FakeClock, script: dict):
        self.clock = clock
        self.script = script
        self.requests = 0

    def set(self, token_id: str, from_ts: float, bids, asks) -> None:
        self.script.setdefault(token_id, []).append((from_ts, bids, asks))
        self.script[token_id].sort(key=lambda e: e[0])

    def get_book(self, token_id: str) -> Book:
        self.requests += 1
        now = self.clock()
        entries = [e for e in self.script[token_id] if e[0] <= now]
        _, bids, asks = entries[-1]
        return make_book(token_id, bids, asks, now)

    def get_books(self, token_ids):
        return {t: self.get_book(t) for t in token_ids}


class FakePrices:
    def __init__(self, clock: FakeClock, open_price: float = 60_000.0, move: Callable[[float], float] | float = 90.0):
        self.clock = clock
        self.open_price = open_price
        self.move = move
        self.fail = 0  # number of upcoming calls that raise

    def snapshot(self, slot_start: int) -> BtcSnapshot:
        if self.fail > 0:
            self.fail -= 1
            raise PriceFeedError("feed down")
        now = self.clock()
        move = self.move(now) if callable(self.move) else self.move
        return BtcSnapshot("fake", slot_start, self.open_price, self.open_price + move, now)


class FakeGamma:
    def __init__(self):
        self.resolutions: dict[str, Optional[str]] = {}
        self.market_calls = 0

    def get_market(self, start: int) -> Market:
        self.market_calls += 1
        return Market(
            slug=slot_slug(start),
            condition_id="0xcond",
            question="Bitcoin Up or Down",
            up_token_id=UP_T,
            down_token_id=DOWN_T,
            start_ts=start,
            end_ts=float(start + 300),
            active=True,
            closed=False,
            accepting_orders=True,
            tick_size=0.01,
            min_order_size=5,
        )

    def get_resolution(self, slug: str) -> Optional[str]:
        return self.resolutions.get(slug)


@pytest.fixture
def cfg():
    return load_config(profile="conservative")


@pytest.fixture
def cfg_factory():
    def build(profile: str = "conservative", **sections):
        c = load_config(profile=profile)
        for section, fields in sections.items():
            c = replace(c, **{section: replace(getattr(c, section), **fields)})
        return c

    return build
