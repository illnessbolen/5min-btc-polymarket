# 5min BTC Polymarket Skill

Open-source OpenClaw skill for **BTC 5-minute Up/Down** markets on Polymarket.

Repository: https://github.com/Novals83/5min-btc-polymarket

Two ways to run the strategy:
- **Standalone bot** (`btc5m_bot/`) — self-contained: market discovery, BTC impulse feed, order books and order placement, risk limits, trade journal, Telegram notifications. Paper trading by default. See [Standalone Bot](#standalone-bot-btc5m_bot).
- **OpenClaw skill contour** (`scripts/`) — delegates order placement to an external execution repo. See [OpenClaw Skill Contour](#openclaw-skill-contour-external-execution-repo).

## Strategy (Momentum into Close)
This skill is aligned with a short-horizon momentum strategy:

1. Trade BTC 5m event markets near expiry.
2. Main entry window: around **2 minutes left**.
3. Confirm that BTC has already moved by about **$70-$100** in the active interval.
4. Check market skew (crowd positioning). If flow supports the move direction, enter **with** momentum.
5. Typical sizing: around **50% of trading allocation** (user-defined risk tolerance).
6. Optional micro-hedge when skew is extreme (for example, 95/5): place a small opposite position ($1-$2 equivalent) to reduce tail risk.

This is a momentum-following approach, not a reversal strategy.

## Standalone Bot (`btc5m_bot`)
A self-contained bot for this strategy. It does not need the external `pm-hl-conservative-plus-repo`:
it finds the current market, measures the BTC impulse, reads CLOB order books and places orders itself through
[`py-clob-client`](https://github.com/Polymarket/py-clob-client). It runs **paper trading by default**
(fills simulated against the live order book) and sends real orders only with `--execute`.

### How it trades
Each 5-minute slot (`btc-updown-5m-<start>`) gets at most one trade:

| Step | Rule (conservative defaults) | Config key (`config/btc_5m_profiles.yaml`) |
|---|---|---|
| Entry window | 90–150 s before the close (~2 min left) | `strategy_reference.entry_window_seconds_left_*`, `session_timing.min_entry_seconds_left` |
| Impulse | BTC moved ≥ $70 since the slot open; the move picks UP or DOWN | `strategy_reference.btc_move_usd_min` (0 = off, follow the favoured side) |
| Skew confirmation | best ask of that side is ≥ 0.70 (the crowd agrees) and ≤ 0.90 | `signal.threshold_price`, `signal.max_entry_price` |
| Execution safety | quotes fresher than 8 s, bid present, spread ≤ 0.03, top ask ≥ $30 | `execution_safety.*` |
| Sizing | $5 stake, capped by `max_notional_usd` and % of balance | `sizing.stake_usd`, `sizing.max_notional_usd`, `sizing.risk_per_trade_pct_equity` |
| Order | FAK buy, limit = ask + 0.02, never above the max entry price | `execution_safety.entry_slippage` |
| Stop-loss | sell when the side's mid falls 25% below the entry price | `stop_loss.*` |
| Micro-hedge | side ≥ 0.95 with ≤ 45 s left (and before the exit): buy $1–2 of the opposite side; it is sold together with the main position | `hedge.*` |
| Exit | sell everything 20 s before the close; `exit_before_sec: 0` holds to resolution | `session_timing.exit_before_sec`, `execution_safety.exit_slippage` |
| Risk | max trades/day, daily loss limit (unresolved positions count at full cost), pause after 3 consecutive API errors | `sizing.max_trades_per_day`, `sizing.daily_max_loss_pct`, `execution_safety.skip_if_dns_or_api_errors_consecutive`, `bot.error_cooldown_sec` |

Exits use FAK orders with a widening limit (bid − slippage, bid − 2×slippage, then any price) until one second
before the close; anything left unsold is held to resolution and settled from the market outcome.

### Quick start (paper trading)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # optional for paper trading (Telegram), required for live trading
.venv/bin/python -m btc5m_bot check
.venv/bin/python -m btc5m_bot run --profile conservative
```
Paper mode starts from a $100 virtual balance (`--paper-equity`) and does not model taker fees.

### Live trading
1. Fill `.env`: `PM_PRIVATE_KEY`, `PM_FUNDER` (the Polymarket proxy wallet address that holds your USDC) and
   `PM_SIGNATURE_TYPE` (`1` email/Magic login, `2` browser wallet, `0` plain EOA). CLOB API credentials are derived
   from the key when `PM_API_KEY` / `PM_API_SECRET` / `PM_API_PASSPHRASE` are empty.
2. EOA wallets (`0`) need token allowances for the exchange contracts; proxy wallets created by polymarket.com already have them.
3. Verify the setup, then start:
```bash
.venv/bin/python -m btc5m_bot check --execute
.venv/bin/python -m btc5m_bot run --profile conservative --execute
```
With `exit_before_sec: 0` (hold to resolution) winning shares have to be redeemed on polymarket.com.

Command-line overrides: `--stake-usd`, `--max-notional-usd`, `--threshold`, `--move-usd`, `--max-entry-price`,
`--stop-loss-pct`, `--exit-before-sec`, `--max-trades-per-day`, `--daily-max-loss-pct`, `--no-hedge`,
`--duration-min`, `--flatten-on-stop` (see `python -m btc5m_bot run --help`).

### Background control and Docker
```bash
scripts/btc5m_bot.sh start --profile conservative             # paper
scripts/btc5m_bot.sh start --profile conservative --execute   # live
scripts/btc5m_bot.sh status | logs | report | check | stop
scripts/btc5m_bot.sh halt       # no new entries; an open position is still managed
scripts/btc5m_bot.sh resume
```
`stop` sends SIGTERM: the bot finishes the current step, keeps an open position in `state.json` and resumes
managing it on the next start (`--flatten-on-stop` sells it instead).

```bash
cp .env.example .env
docker compose -f docker-compose.bot.yml up -d --build   # append "--execute" to `command` for live trading
docker compose -f docker-compose.bot.yml logs -f
docker compose -f docker-compose.bot.yml run --rm btc5m-bot report
```

### Telegram
Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (plus `TELEGRAM_THREAD_ID` for a forum topic) to get entries,
hedges, exits with PnL, kill-switch alerts and daily summaries. With `TELEGRAM_COMMANDS=1` the bot also answers
`/status`, `/pause`, `/resume` and `/report` from that chat only. Use a bot token that no other service polls
(Telegram allows one `getUpdates` consumer per token).

### Runtime files
- `runtime/bot/<paper|live>/state.json` — open position, positions awaiting resolution, daily counters (used to resume after a restart)
- `runtime/bot/<paper|live>/trades.jsonl` — one JSON record per finished trade: legs, fills, entry signal, PnL
- `python -m btc5m_bot report [--mode live] [--since 2026-09-01]` — trades, win rate and PnL by day
- delete `runtime/bot/paper/` (with the bot stopped) to reset paper trading to a fresh `--paper-equity` balance

### Limitations
- The BTC impulse is measured on Binance spot candles (fallback: Coinbase), while markets resolve on the Chainlink
  BTC/USD stream. The filter works on tens of dollars, so the basis between venues matters only for moves right at the threshold.
- PnL is the cash flow reported by order responses; paper mode ignores taker fees.
- Make sure trading on Polymarket is allowed where you live.

### Tests
```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Repository Structure
- `btc5m_bot/` — standalone bot: engine, strategy, exchange (live/paper), market and price feeds, risk, journal, notifications, CLI
- `tests/` — offline test suite for the bot
- `SKILL.md` — skill definition and operating rules
- `config/` — profiles and risk parameters (shared by the bot and the skill runner)
- `scripts/` — runners/wrappers/hot commands; `btc5m_bot.sh` controls the standalone bot
- `examples/` — practical command examples

## OpenClaw Skill Contour (external execution repo)
### Prerequisites
- OpenClaw environment
- Polymarket execution stack available at:
  - `<your-workspace>/pm-hl-conservative-plus-repo`
- Python virtual env for runner scripts
- Valid API credentials configured outside this repository

### Quick Start
```bash
git clone https://github.com/Novals83/5min-btc-polymarket.git
cd 5min-btc-polymarket
```

Read:
- `SKILL.md`
- `config/btc_5m_profiles.yaml`

Run a conservative real test (example):
```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Run aggressive profile:
```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile aggressive --execute
```

Unified skill control (recommended):
```bash
scripts/btc5m_ctl.sh start --profile conservative
scripts/btc5m_ctl.sh status
scripts/btc5m_ctl.sh report --limit 20
scripts/btc5m_ctl.sh stop
```

Runtime isolation:
- skill runtime dir: `./runtime`
- auth/env source (default): `<your-workspace>/pm-hl-conservative-plus-repo/.env`
- overrides: `BTC5M_REPO`, `BTC5M_ENV_FILE`, `BTC5M_RUNNER`
- completion auto-report cron (topic 184): `btc5m-completion-autoreport-topic184`

Optional Docker isolation:
```bash
scripts/btc5m_docker.sh up
scripts/btc5m_docker.sh status
scripts/btc5m_docker.sh down
```

## Execution Checklist (Before Live Trade)
Use this quick pre-flight checklist before any real order:

1. **Market validity**
   - Confirm the BTC 5m market is active and not about to close unexpectedly.
2. **Time-to-close window**
   - Prefer entries around ~120 seconds left (with reasonable tolerance).
3. **Impulse confirmation**
   - Confirm the observed BTC move is meaningful (strategy reference: ~$70-$100).
4. **Skew confirmation**
   - Verify market skew supports the intended direction (do not fade strong momentum by default).
5. **Liquidity/spread checks**
   - Ensure spread and top-of-book notional pass your minimum thresholds.
6. **Sizing guardrails**
   - Validate stake, max notional, and daily loss limits before execution.
7. **Stop / exit controls**
   - Confirm stop-loss and `exit_before_sec` are configured.
8. **Execution mode**
   - Start in dry-run when changing parameters; switch to `--execute` only after validation.

## Risk Controls Template
Suggested baseline controls (adapt to your risk profile):

- **Per-trade risk cap**: 1%-15% of account equity (profile dependent)
- **Daily max loss**: hard stop at 10%-15%
- **Max trades/day**: fixed ceiling to avoid overtrading
- **Max notional/trade**: strict upper bound
- **Quote staleness guard**: skip if market data is stale
- **Spread guard**: skip when spread exceeds threshold
- **Liquidity guard**: skip when top ask/bid notional is too thin
- **Extreme skew hedge**: optional small opposite hedge in 95/5-type scenarios
- **Operational kill switch**: immediate stop on repeated API/DNS/execution failures

## Risk Notice
This repository is educational/operational infrastructure, not financial advice.
Use your own risk limits, daily loss caps, and capital controls.

## Contributing
- Fork the repository
- Create a feature branch
- Commit changes
- Open a PR to `main`

PRs are welcome.
