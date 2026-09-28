# Example commands

Dry-run (safe validation):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative
```

Real execution (conservative):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Real execution (aggressive):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile aggressive --execute
```

## Standalone bot

Check config, connectivity and (with `--execute`) credentials:

```bash
.venv/bin/python -m btc5m_bot check
.venv/bin/python -m btc5m_bot check --execute --telegram
```

Paper trading (default), one hour:

```bash
.venv/bin/python -m btc5m_bot run --profile conservative --duration-min 60
```

Live trading with a smaller stake and without the hedge:

```bash
.venv/bin/python -m btc5m_bot run --profile conservative --stake-usd 3 --no-hedge --execute
```

Hold to resolution instead of selling 20s before the close:

```bash
.venv/bin/python -m btc5m_bot run --profile aggressive --exit-before-sec 0
```

Background control and reports:

```bash
scripts/btc5m_bot.sh start --profile conservative
scripts/btc5m_bot.sh status
scripts/btc5m_bot.sh report --since 2026-09-01
scripts/btc5m_bot.sh stop
```
