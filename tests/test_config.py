from __future__ import annotations

import pytest

from btc5m_bot.config import (
    ConfigError,
    apply_overrides,
    credentials_from_env,
    load_config,
    telegram_from_env,
)


def test_profiles_map_yaml_values():
    c = load_config(profile="conservative")
    assert (c.entry.window_end_sec_left, c.entry.window_start_sec_left) == (90, 150)
    assert c.entry.btc_move_usd_min == 70
    assert (c.entry.threshold_price, c.entry.max_entry_price) == (0.70, 0.90)
    assert (c.entry.max_spread, c.entry.min_top_ask_notional_usd, c.entry.max_quote_age_sec) == (0.03, 30, 8)
    assert (c.sizing.stake_usd, c.sizing.max_notional_usd, c.sizing.max_trades_per_day) == (5, 8, 12)
    assert (c.sizing.risk_per_trade_pct_equity, c.sizing.daily_max_loss_pct) == (8, 10)
    assert c.hedge.enabled and (c.hedge.trigger_side_price_gte, c.hedge.trigger_seconds_left_lte) == (0.95, 45)
    assert (c.exit.stop_loss_pct, c.exit.exit_before_sec) == (0.25, 20)
    assert c.runtime.max_consecutive_errors == 3
    assert c.runtime.price_sources == ("binance", "coinbase")

    a = load_config(profile="aggressive")
    assert (a.exit.stop_loss_pct, a.sizing.max_trades_per_day, a.hedge.notional_usd_max) == (0.30, 20, 3)


def test_unknown_profile():
    with pytest.raises(ConfigError, match="not found"):
        load_config(profile="yolo")


def test_overrides_and_validation():
    c = apply_overrides(load_config(), {"stake_usd": 3, "threshold": 0.75, "hedge_enabled": False, "poll_sec": None})
    assert (c.sizing.stake_usd, c.entry.threshold_price, c.hedge.enabled) == (3, 0.75, False)
    with pytest.raises(ConfigError, match="unknown override"):
        apply_overrides(c, {"leverage": 10})
    with pytest.raises(ConfigError, match="exit_before_sec"):
        apply_overrides(c, {"exit_before_sec": 95})
    with pytest.raises(ConfigError, match="max_entry_price"):
        apply_overrides(c, {"max_entry_price": 0.6})


def test_profile_sections_override_shared(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(
        """
strategy_reference: {entry_window_seconds_left_target: 100, entry_window_seconds_left_tolerance: 20, btc_move_usd_min: 50}
shared_rules:
  session_timing: {min_entry_seconds_left: 60, exit_before_sec: 20}
profiles:
  fast:
    session_timing: {exit_before_sec: 0}
    signal: {threshold_price: 0.8, btc_move_usd_min: 0}
    sizing: {stake_usd: 2}
bot: {price_sources: coinbase}
""",
        encoding="utf-8",
    )
    c = load_config(path, "fast")
    assert (c.entry.window_end_sec_left, c.entry.window_start_sec_left) == (80, 120)
    assert c.entry.btc_move_usd_min == 0 and c.exit.exit_before_sec == 0
    assert c.runtime.price_sources == ("coinbase",)
    assert not c.hedge.enabled


def test_credentials_from_env():
    with pytest.raises(ConfigError, match="PM_PRIVATE_KEY"):
        credentials_from_env({})
    with pytest.raises(ConfigError, match="PM_FUNDER"):
        credentials_from_env({"PM_PRIVATE_KEY": "0xabc"})
    creds = credentials_from_env({"PM_PRIVATE_KEY": "0xsecret", "PM_ADDRESS": "0xfunder", "PM_API_KEY": "k"})
    assert creds.signature_type == 2 and creds.funder == "0xfunder" and not creds.has_api_creds
    assert "0xsecret" not in repr(creds)
    eoa = credentials_from_env({"PM_PRIVATE_KEY": "0xsecret", "PM_SIGNATURE_TYPE": "0"})
    assert eoa.funder is None
    with pytest.raises(ConfigError):
        credentials_from_env({"PM_PRIVATE_KEY": "0xsecret", "PM_SIGNATURE_TYPE": "5"})


def test_telegram_from_env():
    assert telegram_from_env({}) is None
    tg = telegram_from_env({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100", "TELEGRAM_THREAD_ID": "184"})
    assert tg.thread_id == 184 and not tg.commands and "123:abc" not in repr(tg)
    assert telegram_from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "TELEGRAM_COMMANDS": "yes"}).commands
