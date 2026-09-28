from __future__ import annotations

import json

import pytest
import requests

from btc5m_bot.markets import DOWN, UP, GammaClient, MarketParseError, parse_market, parse_resolution, slot_slug, slot_start
from btc5m_bot.pricefeed import BinanceSource, CoinbaseSource, PriceFeed, PriceFeedError

START = 1_800_000_000


def event(outcomes=("Up", "Down"), tokens=("t-up", "t-down"), prices=("0.5", "0.5"), closed=False, end=None, **extra):
    market = {
        "slug": slot_slug(START),
        "conditionId": "0xc",
        "question": "Bitcoin Up or Down",
        "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps(list(prices)),
        "clobTokenIds": json.dumps(list(tokens)),
        "endDate": end or "2027-01-15T08:05:00Z",
        "active": True,
        "closed": closed,
        "acceptingOrders": True,
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        **extra,
    }
    return {"slug": slot_slug(START), "markets": [market]}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes  # url substring -> payload | Exception | FakeResponse
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, FakeResponse) else FakeResponse(value)
        raise requests.ConnectionError(url)


def test_slot_math():
    assert slot_start(START + 299.9) == START and slot_start(START + 300) == START + 300
    assert slot_slug(START) == "btc-updown-5m-1800000000"


def test_parse_market_maps_tokens_by_label():
    m = parse_market(event(), START)
    assert (m.up_token_id, m.down_token_id) == ("t-up", "t-down")
    assert m.end_ts == START + 300 and m.tradable and m.tick_size == 0.01
    flipped = parse_market(event(outcomes=("Down", "Up")), START)
    assert (flipped.up_token_id, flipped.down_token_id) == ("t-down", "t-up")
    assert flipped.token_for(UP) == "t-down"


def test_parse_market_rejects_unknown_outcomes_and_uses_slot_end():
    with pytest.raises(MarketParseError):
        parse_market(event(outcomes=("Yes", "Maybe")), START)
    with pytest.raises(MarketParseError):
        parse_market(event(tokens=("only-one",)), START)
    m = parse_market(event(end="2027-01-15"), START)  # date-only endDateIso style value
    assert m.end_ts == START + 300
    assert not parse_market(event(acceptingOrders=False), START).tradable


def test_parse_resolution():
    assert parse_resolution(event(prices=("1", "0"), closed=True)) == UP
    assert parse_resolution(event(outcomes=("Down", "Up"), prices=("1", "0"), closed=True)) == DOWN
    assert parse_resolution(event(prices=("0.999", "0.001"), closed=False)) is None
    assert parse_resolution(event(prices=("0.6", "0.4"), closed=True)) is None


def test_gamma_client():
    session = FakeSession({"/events": [event()]})
    gamma = GammaClient(session)
    assert gamma.get_market(START).slug == slot_slug(START)
    assert session.calls[0][1] == {"slug": slot_slug(START)}
    assert GammaClient(FakeSession({"/events": []})).get_market(START) is None


def kline(open_time_ms, open_, close):
    return [open_time_ms, str(open_), "0", "0", str(close), "1", open_time_ms + 299_999]


def test_binance_snapshot_and_mirror_fallback():
    session = FakeSession({
        "api.binance.com": FakeResponse({"code": 0}, status=451),
        "data-api.binance.vision": [kline(START * 1000, 60000.5, 60090.25)],
    })
    snap = BinanceSource(session, clock=lambda: 1.0).snapshot(START)
    assert (snap.open_price, snap.price, snap.move) == (60000.5, 60090.25, 89.75)
    wrong_candle = FakeSession({"binance": [kline((START - 300) * 1000, 1, 2)]})
    with pytest.raises(PriceFeedError, match="does not start"):
        BinanceSource(wrong_candle).snapshot(START)


def test_coinbase_snapshot_caches_open():
    session = FakeSession({
        "/candles": [[START + 60, 1, 2, 60010, 60020, 5], [START, 59990, 60010, 60000, 60005, 7]],
        "/ticker": {"price": "59920.5"},
    })
    src = CoinbaseSource(session)
    assert src.snapshot(START).move == pytest.approx(-79.5)
    src.snapshot(START)
    assert sum("/candles" in url for url, _ in session.calls) == 1


def test_price_feed_falls_back_between_sources():
    down = BinanceSource(FakeSession({}))
    up = CoinbaseSource(FakeSession({"/candles": [[START, 0, 0, 100, 0, 0]], "/ticker": {"price": "170"}}))
    assert PriceFeed([down, up]).snapshot(START).source == "coinbase"
    with pytest.raises(PriceFeedError):
        PriceFeed([down]).snapshot(START)
