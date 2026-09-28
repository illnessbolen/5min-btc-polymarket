from __future__ import annotations

from types import SimpleNamespace

import pytest
from py_clob_client.exceptions import PolyApiException

from btc5m_bot.exchange import (
    ClobBooks,
    LiveExchange,
    OrderUncertain,
    PaperExchange,
    book_from_summary,
    parse_post_response,
)

from conftest import FakeClock, ScriptedBooks


def row(price, size):
    return SimpleNamespace(price=str(price), size=str(size))


def test_book_from_summary_sorts_and_filters():
    # The CLOB lists asks from worst to best and bids from worst to best.
    summary = SimpleNamespace(
        asset_id="tok",
        bids=[row(0.10, 5), row(0.78, 0), row(0.79, 100)],
        asks=[row(0.99, 5), row(0.85, 10), row(0.80, 50)],
        tick_size="0.01",
        min_order_size="5",
    )
    book = book_from_summary(summary, "tok", 1.0)
    assert (book.best_bid, book.best_ask) == (0.79, 0.80)
    assert [lvl.price for lvl in book.asks] == [0.80, 0.85, 0.99]
    assert len(book.bids) == 2  # zero-size level dropped
    assert book.spread == pytest.approx(0.01) and book.mark == pytest.approx(0.795)
    assert book.top_ask_notional == pytest.approx(40.0)


def test_parse_post_response():
    buy = parse_post_response(
        {"success": True, "status": "matched", "makingAmount": "5", "takingAmount": "6.25",
         "orderID": "0x1", "transactionsHashes": ["0xtx"], "errorMsg": ""},
        "BUY",
    )
    assert buy.ok and (buy.shares, buy.usdc, buy.tx) == (6.25, 5.0, "0xtx") and buy.avg_price == 0.8
    sell = parse_post_response({"success": True, "status": "matched", "makingAmount": "6.25", "takingAmount": "6"}, "SELL")
    assert sell.ok and (sell.shares, sell.usdc) == (6.25, 6.0)
    nothing = parse_post_response({"success": False, "errorMsg": "no match", "makingAmount": "0", "takingAmount": "0"}, "BUY")
    assert not nothing.ok and nothing.error == "no match"
    assert not parse_post_response("oops", "BUY").ok
    with pytest.raises(OrderUncertain):
        parse_post_response({"success": True, "status": "delayed", "orderID": "0x2"}, "BUY")


class FakeClient:
    def __init__(self, post=None, book=None):
        self.post = post
        self.created = []

    def create_market_order(self, args):
        self.created.append(args)
        return SimpleNamespace(args=args)

    def post_order(self, order, order_type):
        assert order_type == "FAK"
        if isinstance(self.post, Exception):
            raise self.post
        return self.post

    def get_balance_allowance(self, params):
        return {"balance": "6250000"}

    def update_balance_allowance(self, params):
        return None


def test_live_exchange_order_flow():
    client = FakeClient(post={"success": True, "status": "matched", "makingAmount": "4.99", "takingAmount": "6.2375"})
    ex = LiveExchange(client)
    fill = ex.buy("tok", 4.999, 0.82)
    args = client.created[0]
    assert (args.amount, args.side, args.price, args.order_type) == (4.99, "BUY", 0.82, "FAK")
    assert fill.ok and fill.shares == 6.2375
    assert ex.get_share_balance("tok", refresh=True) == 6.25
    assert not ex.sell("tok", 0.004, 0.5).ok  # below the 2-decimal size step


def test_live_exchange_error_classification():
    rejected = LiveExchange(FakeClient(post=PolyApiException(SimpleNamespace(status_code=400, json=lambda: {"error": "no orders found to match with FAK order"}))))
    fill = rejected.buy("tok", 5, 0.8)
    assert not fill.ok and fill.status == "rejected" and "no orders found" in fill.error
    for exc in (PolyApiException(error_msg="Request exception!"), PolyApiException(SimpleNamespace(status_code=502, json=lambda: "bad gateway"))):
        with pytest.raises(OrderUncertain):
            LiveExchange(FakeClient(post=exc)).sell("tok", 5, 0.5)


def test_clob_books_batch_and_fallback():
    class Client:
        def get_order_books(self, params):
            return [SimpleNamespace(asset_id=p.token_id, bids=[row(0.4, 10)], asks=[row(0.6, 10)], tick_size="0.01", min_order_size="5") for p in params[:1]]

        def get_order_book(self, token_id):
            return SimpleNamespace(asset_id=token_id, bids=[], asks=[row(0.5, 1)], tick_size="0.01", min_order_size="5")

        def get_server_time(self):
            return 1_800_000_000

    books = ClobBooks(Client(), clock=lambda: 5.0).get_books(["a", "b"])
    assert list(books) == ["a", "b"]
    assert books["a"].best_bid == 0.4 and books["b"].best_ask == 0.5 and books["b"].fetched_at == 5.0


def test_paper_exchange_walks_the_book():
    clock = FakeClock(0)
    books = ScriptedBooks(clock, {"t": [(0, [(0.78, 10), (0.70, 100)], [(0.80, 5), (0.82, 100), (0.90, 100)])]})
    ex = PaperExchange(books, cash=20.0)
    fill = ex.buy("t", 10.0, 0.82)  # 4.00 at 0.80 then 6.00 at 0.82
    assert fill.ok and fill.usdc == pytest.approx(10.0)
    assert fill.shares == pytest.approx(5 + 6 / 0.82)
    assert ex.cash == pytest.approx(10.0)
    assert not ex.buy("t", 5.0, 0.79).ok  # nothing at or below the limit
    sold = ex.sell("t", 12.0, 0.75)  # 10 at 0.78, the 0.70 bid is below the limit
    assert sold.shares == pytest.approx(10.0) and sold.usdc == pytest.approx(7.8)
    rest = ex.get_share_balance("t")
    assert rest == pytest.approx(5 + 6 / 0.82 - 10)
    ex.settle("t", 1.0)
    assert ex.positions == {} and ex.cash == pytest.approx(10.0 + 7.8 + rest)
    state = ex.to_dict()
    other = PaperExchange(books, cash=0)
    other.load_dict(state)
    assert other.cash == pytest.approx(ex.cash)
