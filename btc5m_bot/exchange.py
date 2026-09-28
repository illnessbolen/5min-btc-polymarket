"""Order books and order execution: live (py-clob-client) and paper (simulated on live books)."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .config import Credentials

log = logging.getLogger(__name__)

EPS = 1e-9
DUST_SHARES = 0.01  # below this a remainder is worth < $0.01 and cannot be sold anyway


def floor_to(x: float, decimals: int) -> float:
    q = 10 ** decimals
    return math.floor(x * q + 1e-7) / q


def floor_to_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-7) * tick, 6)


def clamp_price(price: float, tick: float) -> float:
    return min(max(price, tick), round(1 - tick, 6))


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    token_id: str
    bids: tuple[Level, ...]  # best (highest) first
    asks: tuple[Level, ...]  # best (lowest) first
    tick_size: float
    min_order_size: float
    fetched_at: float

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def mark(self) -> Optional[float]:
        """Mid when both sides are quoted, otherwise the best bid (what the position can be sold at)."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid

    @property
    def top_ask_notional(self) -> float:
        return self.asks[0].price * self.asks[0].size if self.asks else 0.0


def book_from_summary(summary: Any, token_id: str, fetched_at: float) -> Book:
    def levels(rows: Any, best_high: bool) -> tuple[Level, ...]:
        out = []
        for row in rows or []:
            price, size = _f(getattr(row, "price", None)), _f(getattr(row, "size", None))
            if size > 0 and 0 < price < 1:
                out.append(Level(price, size))
        out.sort(key=lambda lvl: lvl.price, reverse=best_high)
        return tuple(out)

    return Book(
        token_id=str(getattr(summary, "asset_id", None) or token_id),
        bids=levels(getattr(summary, "bids", None), True),
        asks=levels(getattr(summary, "asks", None), False),
        tick_size=_f(getattr(summary, "tick_size", None)) or 0.01,
        min_order_size=_f(getattr(summary, "min_order_size", None)),
        fetched_at=fetched_at,
    )


@dataclass(frozen=True)
class Fill:
    ok: bool
    shares: float
    usdc: float  # spent on a buy, received on a sell
    order_id: Optional[str] = None
    tx: Optional[str] = None
    status: str = ""
    error: Optional[str] = None
    raw: Optional[dict] = field(default=None, repr=False)

    @property
    def avg_price(self) -> Optional[float]:
        return self.usdc / self.shares if self.shares > 0 else None

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "shares": round(self.shares, 6),
            "usdc": round(self.usdc, 6),
            "avg_price": round(self.avg_price, 6) if self.avg_price else None,
            "order_id": self.order_id,
            "tx": self.tx,
            "status": self.status,
            "error": self.error,
        }


class OrderUncertain(RuntimeError):
    """The order may have reached the exchange but no definitive answer came back."""


def parse_post_response(resp: Any, side: str) -> Fill:
    if not isinstance(resp, dict):
        return Fill(False, 0.0, 0.0, status="error", error=f"unexpected response: {resp!r}"[:300])
    status = str(resp.get("status") or "").lower()
    if status == "delayed":
        raise OrderUncertain(f"order {resp.get('orderID')} is delayed")
    making, taking = _f(resp.get("makingAmount")), _f(resp.get("takingAmount"))
    shares, usdc = (taking, making) if side == "BUY" else (making, taking)
    ok = resp.get("success") is not False and shares > 0 and usdc > 0
    hashes = resp.get("transactionsHashes") or []
    return Fill(
        ok=ok,
        shares=shares if ok else 0.0,
        usdc=usdc if ok else 0.0,
        order_id=resp.get("orderID") or None,
        tx=hashes[0] if hashes else None,
        status=status,
        error=resp.get("errorMsg") or None,
        raw=resp,
    )


def _error_text(msg: Any) -> str:
    if isinstance(msg, dict):
        msg = msg.get("error") or msg.get("errorMsg") or msg
    return str(msg)[:300]


class ClobBooks:
    """Read-only CLOB access (works with an unauthenticated ClobClient)."""

    def __init__(self, client: Any, clock: Callable[[], float] = time.time):
        self.client = client
        self.clock = clock

    def get_book(self, token_id: str) -> Book:
        return book_from_summary(self.client.get_order_book(token_id), token_id, self.clock())

    def get_books(self, token_ids: Iterable[str]) -> dict[str, Book]:
        token_ids = list(token_ids)
        try:
            from py_clob_client.clob_types import BookParams

            summaries = self.client.get_order_books([BookParams(token_id=t) for t in token_ids])
            now = self.clock()
            books = {str(s.asset_id): book_from_summary(s, str(s.asset_id), now) for s in summaries}
        except Exception as e:
            log.debug("batch book request failed (%s); falling back to single requests", e)
            books = {}
        for t in token_ids:
            if t not in books:
                books[t] = self.get_book(t)
        return {t: books[t] for t in token_ids}

    def server_time(self) -> float:
        return _f(self.client.get_server_time())


class LiveExchange:
    mode = "live"

    def __init__(self, client: Any, clock: Callable[[], float] = time.time):
        self.client = client
        self.books = ClobBooks(client, clock)

    @classmethod
    def connect(cls, creds: Credentials, clock: Callable[[], float] = time.time) -> "LiveExchange":
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            host=creds.clob_host,
            chain_id=POLYGON,
            key=creds.private_key,
            signature_type=creds.signature_type,
            funder=creds.funder,
        )
        if creds.has_api_creds:
            client.set_api_creds(ApiCreds(creds.api_key, creds.api_secret, creds.api_passphrase))
        else:
            client.set_api_creds(client.create_or_derive_api_creds())
        return cls(client, clock)

    def get_book(self, token_id: str) -> Book:
        return self.books.get_book(token_id)

    def get_books(self, token_ids: Iterable[str]) -> dict[str, Book]:
        return self.books.get_books(token_ids)

    def prepare(self, token_ids: Iterable[str]) -> None:
        """Warm the client's neg-risk/fee caches so the entry order is not delayed by lookups."""
        for t in token_ids:
            try:
                self.client.get_neg_risk(t)
                self.client.get_fee_rate_bps(t)
            except Exception as e:
                log.debug("prepare %s failed: %s", t, e)

    def buy(self, token_id: str, usdc: float, limit_price: float) -> Fill:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        amount = floor_to(usdc, 2)
        return self._post(MarketOrderArgs(token_id=token_id, amount=amount, side="BUY", price=limit_price, order_type=OrderType.FAK), "BUY")

    def sell(self, token_id: str, shares: float, min_price: float) -> Fill:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        amount = floor_to(shares, 2)
        if amount <= 0:
            return Fill(False, 0.0, 0.0, status="skipped", error="nothing to sell")
        return self._post(MarketOrderArgs(token_id=token_id, amount=amount, side="SELL", price=min_price, order_type=OrderType.FAK), "SELL")

    def _post(self, args: Any, side: str) -> Fill:
        from py_clob_client.clob_types import OrderType
        from py_clob_client.exceptions import PolyApiException

        try:
            order = self.client.create_market_order(args)
        except Exception as e:  # nothing was sent yet
            return Fill(False, 0.0, 0.0, status="rejected", error=f"create order: {e}"[:300])
        try:
            resp = self.client.post_order(order, OrderType.FAK)
        except PolyApiException as e:
            if e.status_code is None or e.status_code >= 500:
                raise OrderUncertain(_error_text(e.error_msg)) from e
            return Fill(False, 0.0, 0.0, status="rejected", error=_error_text(e.error_msg))
        return parse_post_response(resp, side)

    def get_share_balance(self, token_id: str, refresh: bool = False) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        if refresh:
            try:
                self.client.update_balance_allowance(params)
            except Exception as e:
                log.debug("balance refresh for %s failed: %s", token_id, e)
        resp = self.client.get_balance_allowance(params) or {}
        return _f(resp.get("balance")) / 1e6

    def get_usdc_balance(self) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        resp = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)) or {}
        return _f(resp.get("balance")) / 1e6

    def settle(self, token_id: str, payout: float) -> None:
        """Resolved positions are redeemed on Polymarket itself; nothing to do locally."""


class PaperExchange:
    """Simulated FAK fills against live order books; fees are not modelled."""

    mode = "paper"

    def __init__(self, books: Any, cash: float):
        self.books = books
        self.cash = float(cash)
        self.positions: dict[str, float] = {}
        self._seq = 0

    def get_book(self, token_id: str) -> Book:
        return self.books.get_book(token_id)

    def get_books(self, token_ids: Iterable[str]) -> dict[str, Book]:
        return self.books.get_books(token_ids)

    def prepare(self, token_ids: Iterable[str]) -> None:
        pass

    def _order_id(self) -> str:
        self._seq += 1
        return f"paper-{int(time.time())}-{self._seq}"

    def buy(self, token_id: str, usdc: float, limit_price: float) -> Fill:
        book = self.get_book(token_id)
        budget = min(floor_to(usdc, 2), self.cash)
        spent = shares = 0.0
        for lvl in book.asks:
            if lvl.price > limit_price + EPS or budget - spent <= EPS:
                break
            take = min(lvl.price * lvl.size, budget - spent)
            spent += take
            shares += take / lvl.price
        if shares <= 0:
            return Fill(False, 0.0, 0.0, status="unmatched", error="no asks at or below the limit price")
        self.cash -= spent
        self.positions[token_id] = self.positions.get(token_id, 0.0) + shares
        return Fill(True, shares, spent, order_id=self._order_id(), status="matched")

    def sell(self, token_id: str, shares: float, min_price: float) -> Fill:
        qty = min(floor_to(shares, 2), self.positions.get(token_id, 0.0))
        if qty <= 0:
            return Fill(False, 0.0, 0.0, status="skipped", error="nothing to sell")
        book = self.get_book(token_id)
        sold = proceeds = 0.0
        for lvl in book.bids:
            if lvl.price < min_price - EPS or qty - sold <= EPS:
                break
            take = min(lvl.size, qty - sold)
            sold += take
            proceeds += take * lvl.price
        if sold <= 0:
            return Fill(False, 0.0, 0.0, status="unmatched", error="no bids at or above the limit price")
        self.positions[token_id] = self.positions.get(token_id, 0.0) - sold
        if self.positions[token_id] <= EPS:
            del self.positions[token_id]
        self.cash += proceeds
        return Fill(True, sold, proceeds, order_id=self._order_id(), status="matched")

    def get_share_balance(self, token_id: str, refresh: bool = False) -> float:
        return self.positions.get(token_id, 0.0)

    def get_usdc_balance(self) -> float:
        return self.cash

    def settle(self, token_id: str, payout: float) -> None:
        self.cash += self.positions.pop(token_id, 0.0) * payout

    def to_dict(self) -> dict[str, Any]:
        return {"cash": self.cash, "positions": dict(self.positions)}

    def load_dict(self, data: dict[str, Any]) -> None:
        self.cash = float(data.get("cash", self.cash))
        self.positions = {str(k): float(v) for k, v in (data.get("positions") or {}).items()}
