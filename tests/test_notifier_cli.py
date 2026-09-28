from __future__ import annotations

import json

from btc5m_bot import cli
from btc5m_bot.config import TelegramSettings
from btc5m_bot.journal import Journal, Leg, Position
from btc5m_bot.notifier import TelegramCommands

SETTINGS = TelegramSettings(token="123:secret", chat_id="42", thread_id=184, commands=True)


class FakeApi:
    def __init__(self):
        self.sent = []

    def send_message(self, text, thread_id=None):
        self.sent.append((text, thread_id))


def update(update_id, chat_id, text, thread=None):
    msg = {"chat": {"id": chat_id}, "text": text}
    if thread is not None:
        msg["message_thread_id"] = thread
    return {"update_id": update_id, "message": msg}


def test_commands_only_from_configured_chat():
    seen = []
    cmds = TelegramCommands(SETTINGS, lambda c: seen.append(c) or f"ok {c}")
    cmds.api = FakeApi()
    cmds.handle_update(update(10, 999, "/pause"))  # stranger
    cmds.handle_update(update(11, 42, "hello"))  # not a command
    cmds.handle_update(update(12, 42, "/Status@my_bot extra", thread=184))
    assert seen == ["/status"]
    assert cmds.api.sent == [("ok /status", 184)]
    assert cmds._offset == 13


def test_telegram_errors_do_not_leak_the_token(caplog):
    import requests

    from btc5m_bot.notifier import _TelegramApi

    api = _TelegramApi(SETTINGS)

    class Boom:
        def post(self, url, json=None, timeout=None):
            raise requests.ConnectionError(f"failed to reach {url}")

    api.session = Boom()
    with caplog.at_level("WARNING"):
        assert api.call("sendMessage", {}) is None
    assert "failed to reach" in caplog.text and "123:secret" not in caplog.text


def test_report_and_status_commands(tmp_path, capsys):
    j = Journal(tmp_path / "paper")
    main = Leg(role="main", side="UP", token_id="u", shares=6.25, cost=5.0, opened_at=1_800_000_150,
               closed_shares=6.25, proceeds=6.0)
    pos = Position(trade_id="t", mode="paper", profile="conservative", slug="btc-updown-5m-1800000000",
                   slot_start=1_800_000_000, end_ts=1_800_000_300, up_token_id="u", down_token_id="d",
                   tick_size=0.01, main=main, close_reason="time_exit")
    j.append_trade(pos.record(None, 1.0, 1_800_000_280))
    j.save_state({"mode": "paper", "profile": "conservative", "updated_at": "x",
                  "risk": {"day": "2027-01-15", "trades_today": 1, "realized_pnl_today": 1.0},
                  "position": None, "pending": [pos.to_dict()], "paper": {"cash": 101.0}})

    assert cli.main(["report", "--runtime-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "paper trades: 1" in out and "+1.00" in out and "time_exit" in out

    assert cli.main(["report", "--runtime-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pnl_total"] == 1.0

    assert cli.main(["status", "--runtime-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "awaiting resolution: btc-updown-5m-1800000000" in out and "paper cash: 101.00" in out

    assert cli.main(["status", "--runtime-dir", str(tmp_path), "--mode", "live"]) == 0
    assert "no live state yet" in capsys.readouterr().out


def test_run_rejects_bad_overrides(tmp_path):
    assert cli.main(["run", "--runtime-dir", str(tmp_path), "--exit-before-sec", "120"]) == 2


def test_live_run_requires_credentials(tmp_path, monkeypatch):
    for var in ("PM_PRIVATE_KEY", "BTC5M_ENV_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("btc5m_bot.config.DEFAULT_ENV_FILE", tmp_path / "missing.env")
    assert cli.main(["run", "--runtime-dir", str(tmp_path), "--execute"]) == 2


def test_read_only_commands_do_not_create_directories(tmp_path):
    assert cli.main(["status", "--runtime-dir", str(tmp_path / "rt"), "--mode", "live"]) == 0
    assert cli.main(["report", "--runtime-dir", str(tmp_path / "rt"), "--mode", "live"]) == 0
    assert not (tmp_path / "rt").exists()
