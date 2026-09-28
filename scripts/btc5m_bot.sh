#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="${BTC5M_RUNTIME_DIR:-$ROOT/runtime/bot}"
if [[ -n "${BTC5M_PYTHON:-}" ]]; then
  PY="$BTC5M_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python3"
fi

PIDFILE="$RUNTIME_DIR/bot.pid"
LATEST_LINK="$RUNTIME_DIR/latest.log"
HALT_FILE="$RUNTIME_DIR/HALT"

mkdir -p "$RUNTIME_DIR"

usage() {
  cat <<'USAGE'
Usage:
  btc5m_bot.sh start [--profile conservative|aggressive] [--execute] [other `python -m btc5m_bot run` flags]
  btc5m_bot.sh stop
  btc5m_bot.sh status
  btc5m_bot.sh logs [N]
  btc5m_bot.sh report [--mode paper|live] [--since YYYY-MM-DD]
  btc5m_bot.sh check [--execute] [--telegram]
  btc5m_bot.sh halt      # block new entries (open positions are still managed)
  btc5m_bot.sh resume    # allow new entries again

Notes:
- Standalone bot: no external trading repo needed; credentials come from .env (see .env.example).
- Paper trading unless --execute is passed.
- Runtime files: runtime/bot/{paper,live}/state.json and trades.jsonl, logs in runtime/bot.
USAGE
}

is_running() {
  [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
}

cmd_start() {
  if is_running; then
    echo "already_running pid=$(cat "$PIDFILE")"
    return 0
  fi
  local args=("$@") profile="${BTC5M_PROFILE:-conservative}" mode="paper" i
  for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
      --profile) profile="${args[$((i + 1))]:-$profile}" ;;
      --profile=*) profile="${args[$i]#--profile=}" ;;
      --execute) mode="live" ;;
    esac
  done

  local log
  log="$RUNTIME_DIR/bot_${mode}_${profile}_$(date -u +%Y%m%dT%H%M%SZ).log"
  (
    cd "$ROOT"
    nohup "$PY" -m btc5m_bot run --runtime-dir "$RUNTIME_DIR" "$@" >"$log" 2>&1 &
    echo $! >"$PIDFILE"
  )
  ln -sfn "$log" "$LATEST_LINK"

  sleep 2
  if is_running; then
    echo "started pid=$(cat "$PIDFILE") mode=$mode profile=$profile log=$log"
  else
    echo "failed_to_start, last log lines:"
    tail -n 20 "$log" || true
    rm -f "$PIDFILE"
    exit 1
  fi
}

cmd_stop() {
  if ! is_running; then
    rm -f "$PIDFILE"
    echo "already_stopped"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  kill -TERM "$pid"
  # The bot finishes its current step first (an exit in progress can take up to ~20s).
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "still running after 30s, sending SIGKILL"
    kill -KILL "$pid" || true
  fi
  rm -f "$PIDFILE"
  echo "stopped pid=$pid"
}

cmd_status() {
  if is_running; then
    echo "running pid=$(cat "$PIDFILE")"
    ps -p "$(cat "$PIDFILE")" -o pid=,etime=,command= || true
  else
    echo "stopped"
  fi
  [[ -L "$LATEST_LINK" ]] && echo "latest_log=$(readlink "$LATEST_LINK")"
  [[ -f "$HALT_FILE" ]] && echo "HALT file present: new entries are blocked"
  cd "$ROOT"
  "$PY" -m btc5m_bot status --runtime-dir "$RUNTIME_DIR" --mode paper
  "$PY" -m btc5m_bot status --runtime-dir "$RUNTIME_DIR" --mode live
}

main() {
  local cmd="${1:-}"
  [[ -z "$cmd" ]] && { usage; exit 2; }
  shift || true
  case "$cmd" in
    start) cmd_start "$@" ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    logs)
      if [[ -L "$LATEST_LINK" ]]; then tail -n "${1:-120}" "$(readlink "$LATEST_LINK")"; else echo "no_logs"; fi
      ;;
    report) cd "$ROOT" && "$PY" -m btc5m_bot report --runtime-dir "$RUNTIME_DIR" "$@" ;;
    check) cd "$ROOT" && "$PY" -m btc5m_bot check --runtime-dir "$RUNTIME_DIR" "$@" ;;
    halt) touch "$HALT_FILE" && echo "halted: no new entries until '$0 resume'" ;;
    resume) rm -f "$HALT_FILE" && echo "resumed" ;;
    -h|--help|help) usage ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
