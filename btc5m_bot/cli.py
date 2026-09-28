"""Command line entry point: run / check / report / status."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import requests

from .config import (
    DEFAULT_CLOB_HOST,
    DEFAULT_CONFIG_PATH,
    DEFAULT_RUNTIME_DIR,
    ConfigError,
    apply_overrides,
    credentials_from_env,
    describe,
    load_config,
    load_env_file,
    telegram_from_env,
)
from .journal import Journal, summarize

log = logging.getLogger("btc5m_bot")


class Clock:
    """time.time() corrected by the measured offset to the CLOB server clock."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def setup_logging(level: str) -> None:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("urllib3", "httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def public_clob_client() -> Any:
    from py_clob_client.client import ClobClient

    return ClobClient(os.environ.get("PM_CLOB_HOST", DEFAULT_CLOB_HOST).rstrip("/"))


def sync_clock(clock: Clock, books: Any) -> None:
    try:
        t0 = time.time()
        server = books.server_time()
        t1 = time.time()
    except Exception as e:
        log.warning("could not read the CLOB server time (%s); using the local clock", e)
        return
    if server > 1e11:  # milliseconds
        server /= 1000.0
    if server <= 0:
        return
    offset = server - (t0 + t1) / 2
    if abs(offset) >= 2.0:
        clock.offset = offset
        log.warning("local clock differs from the CLOB by %+.1fs; compensating (enable NTP to fix this)", offset)


def _mode_dir(args: argparse.Namespace, mode: str) -> Path:
    return Path(args.runtime_dir) / mode


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import Bot
    from .exchange import ClobBooks, LiveExchange, PaperExchange
    from .markets import GammaClient
    from .notifier import Notifier, TelegramCommands, TelegramNotifier
    from .pricefeed import build_price_feed

    load_env_file(args.env_file)
    cfg = apply_overrides(
        load_config(args.config, args.profile),
        {
            "stake_usd": args.stake_usd,
            "max_notional_usd": args.max_notional_usd,
            "threshold": args.threshold,
            "move_usd": args.move_usd,
            "max_entry_price": args.max_entry_price,
            "stop_loss_pct": args.stop_loss_pct,
            "exit_before_sec": args.exit_before_sec,
            "max_trades_per_day": args.max_trades_per_day,
            "daily_max_loss_pct": args.daily_max_loss_pct,
            "poll_sec": args.poll_sec,
            "hedge_enabled": False if args.no_hedge else None,
        },
    )
    mode = "live" if args.execute else "paper"
    clock = Clock()
    if args.execute:
        creds = credentials_from_env()
        exchange = LiveExchange.connect(creds, clock=clock)
        books = exchange.books
        log.warning("LIVE TRADING: real orders will be placed for %s", creds.funder or "the signer address")
    else:
        books = ClobBooks(public_clob_client(), clock=clock)
        exchange = PaperExchange(books, cash=args.paper_equity)
    sync_clock(clock, books)

    session = requests.Session()
    telegram = telegram_from_env()
    notifier = TelegramNotifier(telegram, prefix=f"BTC5m {mode}") if telegram else Notifier()
    runtime_root = Path(args.runtime_dir)
    bot = Bot(
        cfg,
        exchange,
        GammaClient(session),
        build_price_feed(cfg.runtime.price_sources, session, clock=clock),
        Journal(runtime_root / mode),
        notifier,
        clock=clock,
        halt_file=runtime_root / "HALT",
        flatten_on_stop=args.flatten_on_stop,
    )

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("signal %s received, stopping after the current step", signum)
        bot.request_stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    commands = None
    if telegram and telegram.commands:
        commands = TelegramCommands(telegram, bot.handle_command)
        commands.start()
    try:
        bot.run(duration_sec=args.duration_min * 60 if args.duration_min else None)
    finally:
        if commands:
            commands.stop()
        notifier.close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .exchange import ClobBooks, LiveExchange
    from .markets import GammaClient, slot_slug, slot_start
    from .notifier import _TelegramApi
    from .pricefeed import build_price_feed

    ok = True

    def line(status: str, name: str, detail: Any) -> None:
        print(f"[{status:>4}] {name}: {detail}")

    load_env_file(args.env_file)
    try:
        cfg = load_config(args.config, args.profile)
        line("ok", "config", describe(cfg))
    except ConfigError as e:
        line("FAIL", "config", e)
        return 1

    session = requests.Session()
    now = time.time()
    start = slot_start(now)
    market = None
    try:
        market = GammaClient(session).get_market(start)
        if market:
            line("ok", "gamma", f"{market.slug} tradable={market.tradable}, {market.seconds_left(now):.0f}s left")
        else:
            line("WARN", "gamma", f"no event found for {slot_slug(start)}")
    except Exception as e:
        ok = False
        line("FAIL", "gamma", e)

    working_sources = 0
    for name in cfg.runtime.price_sources:
        try:
            snap = build_price_feed([name], session).snapshot(start)
            working_sources += 1
            line("ok", f"price:{name}", f"open {snap.open_price:,.2f}, now {snap.price:,.2f}, move {snap.move:+,.2f}")
        except Exception as e:
            line("WARN", f"price:{name}", e)
    if not working_sources:
        ok = False
        line("FAIL", "price feed", "no BTC price source is reachable")

    books = ClobBooks(public_clob_client())
    try:
        line("ok", "clob", f"server clock offset {books.server_time() - time.time():+.1f}s")
        if market:
            got = books.get_books([market.up_token_id, market.down_token_id])
            up, down = got[market.up_token_id], got[market.down_token_id]
            line("ok", "clob books", f"UP {up.best_bid}/{up.best_ask}, DOWN {down.best_bid}/{down.best_ask} (bid/ask)")
    except Exception as e:
        ok = False
        line("FAIL", "clob", e)

    if args.execute:
        try:
            creds = credentials_from_env()
            exchange = LiveExchange.connect(creds)
            line("ok", "auth", f"signature type {creds.signature_type}, funder {creds.funder or 'signer address'}, "
                               f"USDC balance {exchange.get_usdc_balance():,.2f}")
            if not creds.has_api_creds:
                line("info", "api creds", "derived from PM_PRIVATE_KEY (set PM_API_KEY/SECRET/PASSPHRASE to skip this)")
        except Exception as e:
            ok = False
            line("FAIL", "auth", e)

    telegram = telegram_from_env()
    if telegram is None:
        line("info", "telegram", "not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    elif args.telegram:
        sent = _TelegramApi(telegram).call("sendMessage", {
            "chat_id": telegram.chat_id,
            "text": "BTC5m bot: test message",
            **({"message_thread_id": telegram.thread_id} if telegram.thread_id is not None else {}),
        })
        ok = ok and sent is not None
        line("ok" if sent else "FAIL", "telegram", "test message sent" if sent else "sendMessage failed (see log)")
    else:
        line("ok", "telegram", f"configured, commands {'on' if telegram.commands else 'off'} (--telegram sends a test message)")
    return 0 if ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    trades = Journal(_mode_dir(args, args.mode), create=False).load_trades()
    summary = summarize(trades, since=args.since)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    win_rate = "n/a" if summary["win_rate"] is None else f"{summary['win_rate']:.0%}"
    print(f"{args.mode} trades: {summary['trades']}  wins/losses: {summary['wins']}/{summary['losses']} ({win_rate})  "
          f"PnL: {summary['pnl_total']:+.2f} USDC")
    for day, row in summary["by_day"].items():
        print(f"  {day}: {row['trades']} trades, {row['pnl']:+.2f} USDC")
    if summary["close_reasons"]:
        print("  close reasons: " + ", ".join(f"{k} {v}" for k, v in summary["close_reasons"].items()))
    rows = [t for t in trades if not args.since or str(t.get("finished_at") or "") >= args.since][-args.last:]
    if rows:
        print(f"last {len(rows)}:")
    for t in rows:
        pnl = t.get("pnl")
        pnl_text = "   n/a" if pnl is None else f"{pnl:+6.2f}"
        print(f"  {t.get('finished_at')} {t.get('side', ''):>4} {t.get('slug')} entry {t.get('entry_price', 0):.3f} "
              f"{pnl_text} {t.get('close_reason')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = Journal(_mode_dir(args, args.mode), create=False).load_state()
    if not state:
        print(f"no {args.mode} state yet")
        return 0
    risk = state.get("risk") or {}
    print(f"{state.get('mode')} {state.get('profile')}, updated {state.get('updated_at')}")
    print(f"day {risk.get('day')}: {risk.get('trades_today')} trades, PnL {risk.get('realized_pnl_today', 0):+.2f} USDC, "
          f"day start balance {risk.get('day_start_equity')}")
    pos = state.get("position")
    if pos:
        main = pos.get("main") or {}
        print(f"open position: {main.get('side')} {pos.get('slug')} {main.get('shares', 0):.2f} sh, cost {main.get('cost', 0):.2f}")
    for p in state.get("pending") or []:
        print(f"awaiting resolution: {p.get('slug')} ({p.get('close_reason')})")
    if state.get("paper"):
        print(f"paper cash: {state['paper'].get('cash', 0):.2f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m btc5m_bot", description="Polymarket BTC 5m Up/Down momentum bot")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", default=os.environ.get("BTC5M_PROFILE", "conservative"))
        p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="strategy profiles YAML")
        p.add_argument("--env-file", default=None, help="env file with credentials (default: .env in the repo root)")
        p.add_argument("--runtime-dir", default=os.environ.get("BTC5M_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR)))
        p.add_argument("--log-level", default=os.environ.get("BTC5M_LOG_LEVEL", "INFO"))

    run = sub.add_parser("run", help="run the trading loop (paper trading unless --execute)")
    common(run)
    run.add_argument("--execute", action="store_true", help="place real orders with the configured wallet")
    run.add_argument("--paper-equity", type=float, default=100.0, help="starting paper balance in USDC")
    run.add_argument("--stake-usd", type=float)
    run.add_argument("--max-notional-usd", type=float)
    run.add_argument("--threshold", type=float, help="min best ask of the traded side (skew confirmation)")
    run.add_argument("--move-usd", type=float, help="min BTC move in USD since the slot open; 0 disables the filter")
    run.add_argument("--max-entry-price", type=float)
    run.add_argument("--stop-loss-pct", type=float, help="0.25 = exit when the side drops 25%% below entry")
    run.add_argument("--exit-before-sec", type=float, help="sell this many seconds before close; 0 = hold to resolution")
    run.add_argument("--max-trades-per-day", type=int)
    run.add_argument("--daily-max-loss-pct", type=float)
    run.add_argument("--poll-sec", type=float)
    run.add_argument("--no-hedge", action="store_true")
    run.add_argument("--duration-min", type=float, help="stop taking entries after N minutes and exit once flat")
    run.add_argument("--flatten-on-stop", action="store_true", help="sell an open position when stopped")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="verify config, connectivity and (with --execute) credentials")
    common(check)
    check.add_argument("--execute", action="store_true", help="also verify trading credentials and balance")
    check.add_argument("--telegram", action="store_true", help="send a Telegram test message")
    check.set_defaults(func=cmd_check)

    for name, func, text in (("report", cmd_report, "summarize the trade journal"), ("status", cmd_status, "show saved state")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--mode", choices=("paper", "live"), default="paper")
        p.add_argument("--runtime-dir", default=os.environ.get("BTC5M_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR)))
        p.add_argument("--log-level", default="WARNING")
        p.set_defaults(func=func)
        if name == "report":
            p.add_argument("--since", help="only trades finished at/after this ISO date, e.g. 2026-09-01")
            p.add_argument("--last", type=int, default=10, help="list the last N trades")
            p.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return int(args.func(args) or 0)
    except ConfigError as e:
        log.error("%s", e)
        return 2
