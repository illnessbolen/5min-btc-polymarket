"""BTC 5m market discovery and resolution via the Polymarket Gamma API."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import requests

SLOT_SEC = 300
SLUG_PREFIX = "btc-updown-5m-"
GAMMA_BASE = "https://gamma-api.polymarket.com"

UP = "UP"
DOWN = "DOWN"

log = logging.getLogger(__name__)


class MarketParseError(ValueError):
    pass


def opposite(side: str) -> str:
    return DOWN if side == UP else UP


def slot_start(ts: float) -> int:
    t = int(ts)
    return t - t % SLOT_SEC


def slot_slug(start: int) -> str:
    return f"{SLUG_PREFIX}{int(start)}"


def _json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _parse_iso(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _outcome_indexes(outcomes: Any) -> tuple[int, int]:
    """Return (up_index, down_index); refuse to guess on unexpected labels."""
    labels = [str(x).strip().lower() for x in outcomes] if isinstance(outcomes, list) else []
    if len(labels) == 2:
        if labels[0] in ("up", "yes") and labels[1] in ("down", "no"):
            return 0, 1
        if labels[1] in ("up", "yes") and labels[0] in ("down", "no"):
            return 1, 0
    raise MarketParseError(f"unexpected outcomes: {outcomes!r}")


@dataclass(frozen=True)
class Market:
    slug: str
    condition_id: str
    question: str
    up_token_id: str
    down_token_id: str
    start_ts: int
    end_ts: float
    active: bool
    closed: bool
    accepting_orders: bool
    tick_size: float
    min_order_size: float

    def seconds_left(self, now: float) -> float:
        return self.end_ts - now

    def token_for(self, side: str) -> str:
        return self.up_token_id if side == UP else self.down_token_id

    @property
    def tradable(self) -> bool:
        return self.active and not self.closed and self.accepting_orders


def parse_market(event: dict[str, Any], start: int) -> Market:
    markets = event.get("markets") or []
    if not markets:
        raise MarketParseError("event has no markets")
    m = markets[0]
    up_i, down_i = _outcome_indexes(_json_field(m.get("outcomes")))
    token_ids = _json_field(m.get("clobTokenIds"))
    if not isinstance(token_ids, list) or len(token_ids) != 2 or not all(token_ids):
        raise MarketParseError(f"bad clobTokenIds: {m.get('clobTokenIds')!r}")

    expected_end = start + SLOT_SEC
    end_ts = _parse_iso(m.get("endDate"))
    if end_ts is None or abs(end_ts - expected_end) > 60:
        if end_ts is not None:
            log.warning("market %s endDate %s disagrees with slug; using slot end", m.get("slug"), m.get("endDate"))
        end_ts = float(expected_end)

    accepting = m.get("acceptingOrders")
    return Market(
        slug=slot_slug(start),  # event slug: used for every later lookup
        condition_id=str(m.get("conditionId") or ""),
        question=str(m.get("question") or event.get("title") or ""),
        up_token_id=str(token_ids[up_i]),
        down_token_id=str(token_ids[down_i]),
        start_ts=int(start),
        end_ts=end_ts,
        active=m.get("active") is not False,
        closed=m.get("closed") is True,
        accepting_orders=accepting is not False,
        tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
        min_order_size=float(m.get("orderMinSize") or 5),
    )


def parse_resolution(event: dict[str, Any]) -> Optional[str]:
    """Winning side once the market is closed with settled prices, else None."""
    markets = event.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    if m.get("closed") is not True:
        return None
    try:
        up_i, down_i = _outcome_indexes(_json_field(m.get("outcomes")))
        prices = _json_field(m.get("outcomePrices"))
        up_p, down_p = float(prices[up_i]), float(prices[down_i])
    except (MarketParseError, TypeError, ValueError, IndexError):
        return None
    if up_p >= 0.99 and down_p <= 0.01:
        return UP
    if down_p >= 0.99 and up_p <= 0.01:
        return DOWN
    return None


class GammaClient:
    def __init__(self, session: requests.Session, base: str = GAMMA_BASE, timeout: float = 8.0):
        self.session = session
        self.base = base.rstrip("/")
        self.timeout = timeout

    def fetch_event(self, slug: str) -> Optional[dict[str, Any]]:
        r = self.session.get(f"{self.base}/events", params={"slug": slug}, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data or None

    def get_market(self, start: int) -> Optional[Market]:
        event = self.fetch_event(slot_slug(start))
        return parse_market(event, start) if event else None

    def get_resolution(self, slug: str) -> Optional[str]:
        event = self.fetch_event(slug)
        return parse_resolution(event) if event else None
