"""BTC spot price feed: slot open price and latest price, used to measure the impulse.

Polymarket resolves BTC 5m markets on the Chainlink BTC/USD stream; exchange spot
prices are used here as a proxy for the *move* inside the slot, which is what the
strategy filters on (tens of dollars), so the small basis between venues is irrelevant.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import requests

SLOT_SEC = 300


class PriceFeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class BtcSnapshot:
    source: str
    slot_start: int
    open_price: float
    price: float
    fetched_at: float

    @property
    def move(self) -> float:
        return self.price - self.open_price


class BinanceSource:
    """One klines request returns both the slot open and the live close of the 5m candle."""

    name = "binance"

    def __init__(
        self,
        session: requests.Session,
        bases: Iterable[str] = ("https://api.binance.com", "https://data-api.binance.vision"),
        symbol: str = "BTCUSDT",
        timeout: float = 4.0,
        clock: Callable[[], float] = time.time,
    ):
        self.session = session
        self.bases = tuple(bases)
        self.symbol = symbol
        self.timeout = timeout
        self.clock = clock
        self._preferred = self.bases[0]  # last base that answered (api.binance.com is geo-blocked in some regions)

    def snapshot(self, slot_start: int) -> BtcSnapshot:
        errors = []
        for base in sorted(self.bases, key=lambda b: b != self._preferred):
            try:
                r = self.session.get(
                    f"{base}/api/v3/klines",
                    params={"symbol": self.symbol, "interval": "5m", "startTime": slot_start * 1000, "limit": 1},
                    timeout=self.timeout,
                )
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    raise PriceFeedError("empty klines")
                row = rows[0]
                if int(row[0]) != slot_start * 1000:
                    raise PriceFeedError(f"candle {row[0]} does not start at slot {slot_start * 1000}")
                snap = BtcSnapshot(self.name, slot_start, float(row[1]), float(row[4]), self.clock())
                self._preferred = base
                return snap
            except (requests.RequestException, ValueError, TypeError, IndexError, PriceFeedError) as e:
                errors.append(f"{base}: {e}")
        raise PriceFeedError("binance: " + "; ".join(errors))


class CoinbaseSource:
    """Slot open from the 1m candle starting at the slot boundary, live price from the ticker."""

    name = "coinbase"

    def __init__(
        self,
        session: requests.Session,
        base: str = "https://api.exchange.coinbase.com",
        product: str = "BTC-USD",
        timeout: float = 4.0,
        clock: Callable[[], float] = time.time,
    ):
        self.session = session
        self.base = base
        self.product = product
        self.timeout = timeout
        self.clock = clock
        self._open_cache: dict[int, float] = {}

    def _slot_open(self, slot_start: int) -> float:
        if slot_start in self._open_cache:
            return self._open_cache[slot_start]

        def iso(ts: int) -> str:
            # Coinbase returns an empty list for "+00:00" offsets; it needs the "Z" form.
            return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        r = self.session.get(
            f"{self.base}/products/{self.product}/candles",
            params={"granularity": 60, "start": iso(slot_start), "end": iso(slot_start + 60)},
            timeout=self.timeout,
        )
        r.raise_for_status()
        # rows: [time, low, high, open, close, volume], newest first
        row = next((x for x in r.json() if int(x[0]) == slot_start), None)
        if row is None:
            raise PriceFeedError(f"no 1m candle at slot start {slot_start}")
        self._open_cache = {slot_start: float(row[3])}
        return self._open_cache[slot_start]

    def snapshot(self, slot_start: int) -> BtcSnapshot:
        try:
            open_price = self._slot_open(slot_start)
            r = self.session.get(f"{self.base}/products/{self.product}/ticker", timeout=self.timeout)
            r.raise_for_status()
            price = float(r.json()["price"])
        except (requests.RequestException, ValueError, TypeError, IndexError, KeyError) as e:
            raise PriceFeedError(f"coinbase: {e}") from None
        return BtcSnapshot(self.name, slot_start, open_price, price, self.clock())


SOURCES = {"binance": BinanceSource, "coinbase": CoinbaseSource}
SOURCE_NAMES = tuple(SOURCES)


class PriceFeed:
    """Tries sources in order and returns the first successful snapshot."""

    def __init__(self, sources: list):
        if not sources:
            raise ValueError("at least one price source is required")
        self.sources = sources
        self.last_error: Optional[str] = None

    def snapshot(self, slot_start: int) -> BtcSnapshot:
        errors = []
        for src in self.sources:
            try:
                snap = src.snapshot(slot_start)
                self.last_error = None
                return snap
            except PriceFeedError as e:
                errors.append(str(e))
        self.last_error = "; ".join(errors)
        raise PriceFeedError(self.last_error)


def build_price_feed(names: Iterable[str], session: requests.Session, clock: Callable[[], float] = time.time) -> PriceFeed:
    return PriceFeed([SOURCES[n](session, clock=clock) for n in names])
