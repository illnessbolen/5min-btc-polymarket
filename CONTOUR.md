# BTC 5m Skill Contour (single source of truth)

## Canonical execution path
- Strategy runner (canonical):
  - `scripts/test_btc_5m_session_exit_sl.py`
- Unified control entrypoint:
  - `scripts/btc5m_ctl.sh` (`start|status|stop|report|logs`)
- Compatibility wrapper (deprecated path, forwards to canonical):
  - `scripts/run_btc_5m_threshold_test.py`
- Chat/start helper:
  - `scripts/btc5m_hot.sh`
- Watch helper:
  - `scripts/watch_btc_5m_threshold_and_enter.sh`
- PnL/report utility:
  - `scripts/btc5m_report.py`
- Latest-run completion reporter:
  - `scripts/btc5m_latest_report.py`
- Optional docker control:
  - `scripts/btc5m_docker.sh`

## Standalone bot path (no external dependency)
- Bot package (strategy, execution, risk, journal):
  - `btc5m_bot/` (`python -m btc5m_bot run|check|report|status`)
- Control entrypoint:
  - `scripts/btc5m_bot.sh` (`start|stop|status|logs|report|check|halt|resume`)
- Parameters: `config/btc_5m_profiles.yaml` (same profiles as the skill runner, plus the `bot` section)
- Auth: `.env` in the repo root (or `BTC5M_ENV_FILE`); template in `.env.example`
- Runtime: `runtime/bot/{paper,live}/state.json`, `runtime/bot/{paper,live}/trades.jsonl`,
  logs `runtime/bot/bot_<mode>_<profile>_<UTCSTAMP>.log`
- Orders go directly to the Polymarket CLOB; this path never calls `pm_live_trade_runner.py`.

## External dependency boundary
- Order placement/close engine is delegated to:
  - `<your-workspace>/pm-hl-conservative-plus-repo/src/live/pm_live_trade_runner.py`
- Auth source:
  - `<your-workspace>/pm-hl-conservative-plus-repo/.env` (or `BTC5M_ENV_FILE`)

## Runtime artifacts
- Primary runtime dir (skill-isolated):
  - `skills/btc-5m-live/runtime`
- BTC 5m run logs follow `btc5m_*` naming.

## Isolation guidance
- Keep BTC 5m cron/checkers scoped to this skill naming (`btc5m-*`).
- Avoid creating generic watchers in unrelated topics/chats.
- Keep all new BTC 5m automation pointing to one of the two canonical paths (skill runner or standalone bot).
- Do not run the skill runner and the standalone bot live on the same wallet at the same time.
- Active completion cron in this contour:
  - `btc5m-completion-autoreport-topic184` (`36d3b9e6-4638-4e93-80f6-abb268ebbe57`)
