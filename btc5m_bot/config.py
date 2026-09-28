"""Bot configuration: strategy profiles from YAML, credentials from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .pricefeed import SOURCE_NAMES

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "btc_5m_profiles.yaml"
DEFAULT_RUNTIME_DIR = REPO_ROOT / "runtime" / "bot"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EntryParams:
    window_start_sec_left: float  # window opens when this many seconds are left
    window_end_sec_left: float  # no new entries below this many seconds left
    btc_move_usd_min: float  # 0 disables the impulse filter (pure skew/threshold mode)
    threshold_price: float  # min best ask of the traded side (skew confirmation)
    max_entry_price: float  # max best ask we are willing to pay
    max_spread: float
    min_top_ask_notional_usd: float
    max_quote_age_sec: float
    slippage: float  # FAK buy limit = best ask + slippage (capped by max_entry_price)


@dataclass(frozen=True)
class SizingParams:
    stake_usd: float
    max_notional_usd: float  # 0 = no cap
    risk_per_trade_pct_equity: float  # 0 = no equity-based cap
    daily_max_loss_pct: float  # 0 = disabled
    max_trades_per_day: int  # 0 = unlimited
    min_order_usd: float


@dataclass(frozen=True)
class HedgeParams:
    enabled: bool
    trigger_side_price_gte: float
    trigger_seconds_left_lte: float
    share_of_main_pct: float
    notional_usd_min: float
    notional_usd_max: float


@dataclass(frozen=True)
class ExitParams:
    stop_loss_enabled: bool
    stop_loss_pct: float
    exit_before_sec: float  # 0 = hold to resolution
    slippage: float  # first FAK sell limit = best bid - slippage, widened on retries


@dataclass(frozen=True)
class RuntimeParams:
    poll_sec: float
    idle_poll_sec: float
    max_consecutive_errors: int
    error_cooldown_sec: float
    price_sources: tuple[str, ...]


@dataclass(frozen=True)
class BotConfig:
    profile: str
    entry: EntryParams
    sizing: SizingParams
    hedge: HedgeParams
    exit: ExitParams
    runtime: RuntimeParams


# CLI override name -> (section, field)
OVERRIDES: dict[str, tuple[str, str]] = {
    "stake_usd": ("sizing", "stake_usd"),
    "max_notional_usd": ("sizing", "max_notional_usd"),
    "max_trades_per_day": ("sizing", "max_trades_per_day"),
    "daily_max_loss_pct": ("sizing", "daily_max_loss_pct"),
    "threshold": ("entry", "threshold_price"),
    "move_usd": ("entry", "btc_move_usd_min"),
    "max_entry_price": ("entry", "max_entry_price"),
    "stop_loss_pct": ("exit", "stop_loss_pct"),
    "exit_before_sec": ("exit", "exit_before_sec"),
    "hedge_enabled": ("hedge", "enabled"),
    "poll_sec": ("runtime", "poll_sec"),
}


def _first(*values: Any, default: Any = None) -> Any:
    for v in values:
        if v is not None:
            return v
    return default


def _section(mapping: Any, key: str) -> dict[str, Any]:
    value = mapping.get(key) if isinstance(mapping, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def load_config(path: Path | str = DEFAULT_CONFIG_PATH, profile: str = "conservative") -> BotConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path}: top level must be a mapping")

    profiles = _section(raw, "profiles")
    if profile not in profiles:
        raise ConfigError(f"profile {profile!r} not found in {path}; available: {', '.join(sorted(profiles)) or 'none'}")
    prof = _section(profiles, profile)

    ref = _section(raw, "strategy_reference")
    shared = _section(raw, "shared_rules")
    # Profile-level sections override the shared ones key by key.
    safety = {**_section(shared, "execution_safety"), **_section(prof, "execution_safety")}
    timing = {**_section(shared, "session_timing"), **_section(prof, "session_timing")}
    bot = {**_section(raw, "bot"), **_section(prof, "bot")}
    signal = _section(prof, "signal")
    sizing = _section(prof, "sizing")
    hedge = _section(prof, "hedge")
    stop = _section(prof, "stop_loss")

    try:
        target = float(_first(signal.get("entry_window_seconds_left_target"), ref.get("entry_window_seconds_left_target"), default=120))
        tolerance = float(_first(signal.get("entry_window_seconds_left_tolerance"), ref.get("entry_window_seconds_left_tolerance"), default=30))
        min_entry_left = float(_first(timing.get("min_entry_seconds_left"), default=60))
        entry = EntryParams(
            window_start_sec_left=target + tolerance,
            window_end_sec_left=max(target - tolerance, min_entry_left),
            btc_move_usd_min=float(_first(signal.get("btc_move_usd_min"), ref.get("btc_move_usd_min"), default=70)),
            threshold_price=float(_first(signal.get("threshold_price"), default=0.70)),
            max_entry_price=float(_first(signal.get("max_entry_price"), default=0.95)),
            max_spread=float(_first(safety.get("skip_if_spread_gt"), default=0.03)),
            min_top_ask_notional_usd=float(_first(safety.get("skip_if_top_ask_notional_usd_lt"), default=30)),
            max_quote_age_sec=float(_first(safety.get("skip_if_quote_stale_sec_gt"), default=8)),
            slippage=float(_first(safety.get("entry_slippage"), default=0.02)),
        )
        sizing_p = SizingParams(
            stake_usd=float(_first(sizing.get("stake_usd"), default=5)),
            max_notional_usd=float(_first(sizing.get("max_notional_usd"), default=0)),
            risk_per_trade_pct_equity=float(_first(sizing.get("risk_per_trade_pct_equity"), default=0)),
            daily_max_loss_pct=float(_first(sizing.get("daily_max_loss_pct"), default=0)),
            max_trades_per_day=int(_first(sizing.get("max_trades_per_day"), default=0)),
            min_order_usd=float(_first(bot.get("min_order_usd"), default=1.0)),
        )
        hedge_p = HedgeParams(
            enabled=bool(_first(hedge.get("enabled"), default=False)),
            trigger_side_price_gte=float(_first(hedge.get("trigger_side_price_gte"), default=0.95)),
            trigger_seconds_left_lte=float(_first(hedge.get("trigger_seconds_left_lte"), default=45)),
            share_of_main_pct=float(_first(hedge.get("hedge_share_of_main_pct"), default=3)),
            notional_usd_min=float(_first(hedge.get("hedge_notional_usd_min"), default=1)),
            notional_usd_max=float(_first(hedge.get("hedge_notional_usd_max"), default=2)),
        )
        exit_p = ExitParams(
            stop_loss_enabled=bool(_first(stop.get("enabled"), default=True)),
            stop_loss_pct=float(_first(stop.get("stop_loss_pct_from_entry"), default=0.25)),
            exit_before_sec=float(_first(timing.get("exit_before_sec"), default=20)),
            slippage=float(_first(safety.get("exit_slippage"), default=0.05)),
        )
        sources = bot.get("price_sources") or ["binance", "coinbase"]
        if isinstance(sources, str):
            sources = [sources]
        runtime_p = RuntimeParams(
            poll_sec=float(_first(bot.get("poll_sec"), default=1.0)),
            idle_poll_sec=float(_first(bot.get("idle_poll_sec"), default=10.0)),
            max_consecutive_errors=int(_first(safety.get("skip_if_dns_or_api_errors_consecutive"), default=3)),
            error_cooldown_sec=float(_first(bot.get("error_cooldown_sec"), default=300)),
            price_sources=tuple(str(s).strip().lower() for s in sources),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{path}: bad value in profile {profile!r}: {e}") from None

    cfg = BotConfig(profile=profile, entry=entry, sizing=sizing_p, hedge=hedge_p, exit=exit_p, runtime=runtime_p)
    validate(cfg)
    return cfg


def apply_overrides(cfg: BotConfig, overrides: Mapping[str, Any]) -> BotConfig:
    """Return a copy of cfg with flat CLI overrides applied (None values are ignored)."""
    grouped: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        if key not in OVERRIDES:
            raise ConfigError(f"unknown override: {key}")
        section, name = OVERRIDES[key]
        grouped.setdefault(section, {})[name] = value
    for section, fields in grouped.items():
        cfg = replace(cfg, **{section: replace(getattr(cfg, section), **fields)})
    validate(cfg)
    return cfg


def validate(cfg: BotConfig) -> None:
    e, s, h, x, r = cfg.entry, cfg.sizing, cfg.hedge, cfg.exit, cfg.runtime
    problems: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            problems.append(msg)

    check(0 < e.threshold_price < 1, "threshold_price must be in (0, 1)")
    check(e.threshold_price <= e.max_entry_price < 1, "max_entry_price must be in [threshold_price, 1)")
    check(300 >= e.window_start_sec_left > e.window_end_sec_left >= 0,
          "entry window is empty or outside the 5m slot (check entry_window_* and min_entry_seconds_left)")
    check(e.btc_move_usd_min >= 0, "btc_move_usd_min must be >= 0")
    check(e.max_spread > 0, "skip_if_spread_gt must be > 0")
    check(e.min_top_ask_notional_usd >= 0, "skip_if_top_ask_notional_usd_lt must be >= 0")
    check(e.max_quote_age_sec > 0, "skip_if_quote_stale_sec_gt must be > 0")
    check(0 <= e.slippage < 1, "entry_slippage must be in [0, 1)")
    check(s.stake_usd > 0, "stake_usd must be > 0")
    check(s.max_notional_usd >= 0, "max_notional_usd must be >= 0")
    check(0 <= s.risk_per_trade_pct_equity <= 100, "risk_per_trade_pct_equity must be in [0, 100]")
    check(0 <= s.daily_max_loss_pct <= 100, "daily_max_loss_pct must be in [0, 100]")
    check(s.max_trades_per_day >= 0, "max_trades_per_day must be >= 0")
    check(s.min_order_usd > 0, "min_order_usd must be > 0")
    check(not x.stop_loss_enabled or 0 < x.stop_loss_pct < 1, "stop_loss_pct_from_entry must be in (0, 1)")
    check(0 <= x.exit_before_sec < e.window_end_sec_left,
          "exit_before_sec must be >= 0 and below the entry window end, otherwise positions close right after entry")
    check(0 <= x.slippage < 1, "exit_slippage must be in [0, 1)")
    if h.enabled:
        check(0 < h.trigger_side_price_gte < 1, "hedge.trigger_side_price_gte must be in (0, 1)")
        check(h.trigger_seconds_left_lte > 0, "hedge.trigger_seconds_left_lte must be > 0")
        check(h.share_of_main_pct >= 0, "hedge.hedge_share_of_main_pct must be >= 0")
        check(0 < h.notional_usd_min <= h.notional_usd_max, "hedge notional bounds must satisfy 0 < min <= max")
    check(r.poll_sec > 0 and r.idle_poll_sec > 0, "poll intervals must be > 0")
    check(r.max_consecutive_errors >= 1, "skip_if_dns_or_api_errors_consecutive must be >= 1")
    check(r.error_cooldown_sec >= 0, "error_cooldown_sec must be >= 0")
    unknown = [src for src in r.price_sources if src not in SOURCE_NAMES]
    check(bool(r.price_sources) and not unknown,
          f"price_sources must be a non-empty subset of {list(SOURCE_NAMES)} (got {list(r.price_sources)})")
    if problems:
        raise ConfigError("invalid config: " + "; ".join(problems))


def describe(cfg: BotConfig) -> str:
    e, s, h, x = cfg.entry, cfg.sizing, cfg.hedge, cfg.exit
    impulse = f"|BTC move| >= ${e.btc_move_usd_min:g}" if e.btc_move_usd_min > 0 else "impulse filter off"
    hedge = (
        f"hedge {h.notional_usd_min:g}-{h.notional_usd_max:g}$ when side >= {h.trigger_side_price_gte:g} "
        f"and <= {h.trigger_seconds_left_lte:g}s left"
        if h.enabled else "hedge off"
    )
    exit_rule = f"exit {x.exit_before_sec:g}s before close" if x.exit_before_sec > 0 else "hold to resolution"
    stop = f"stop-loss {x.stop_loss_pct:.0%}" if x.stop_loss_enabled else "stop-loss off"
    return (
        f"profile={cfg.profile}: entry {e.window_end_sec_left:g}-{e.window_start_sec_left:g}s left, {impulse}, "
        f"ask in [{e.threshold_price:g}, {e.max_entry_price:g}], spread <= {e.max_spread:g}, "
        f"top ask >= ${e.min_top_ask_notional_usd:g}; stake ${s.stake_usd:g} (cap ${s.max_notional_usd:g}, "
        f"{s.risk_per_trade_pct_equity:g}% equity), max {s.max_trades_per_day or 'inf'} trades/day, "
        f"daily loss {s.daily_max_loss_pct:g}%; {stop}; {exit_rule}; {hedge}"
    )


@dataclass(frozen=True)
class Credentials:
    private_key: str = field(repr=False)
    funder: Optional[str]
    signature_type: int
    api_key: Optional[str] = field(default=None, repr=False)
    api_secret: Optional[str] = field(default=None, repr=False)
    api_passphrase: Optional[str] = field(default=None, repr=False)
    clob_host: str = DEFAULT_CLOB_HOST

    @property
    def has_api_creds(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


def credentials_from_env(env: Mapping[str, str] = os.environ) -> Credentials:
    key = (env.get("PM_PRIVATE_KEY") or "").strip()
    if not key:
        raise ConfigError("PM_PRIVATE_KEY is not set (required for --execute)")
    sig_raw = (env.get("PM_SIGNATURE_TYPE") or "2").strip()
    try:
        sig = int(sig_raw)
    except ValueError:
        raise ConfigError(f"PM_SIGNATURE_TYPE must be an integer, got {sig_raw!r}") from None
    if sig not in (0, 1, 2):
        raise ConfigError("PM_SIGNATURE_TYPE must be 0 (EOA), 1 (email/Magic proxy) or 2 (browser-wallet proxy)")
    funder = (env.get("PM_FUNDER") or env.get("PM_ADDRESS") or "").strip() or None
    if sig in (1, 2) and not funder:
        raise ConfigError("PM_FUNDER (the Polymarket proxy wallet address) is required for PM_SIGNATURE_TYPE 1 or 2")
    return Credentials(
        private_key=key,
        funder=funder,
        signature_type=sig,
        api_key=(env.get("PM_API_KEY") or "").strip() or None,
        api_secret=(env.get("PM_API_SECRET") or "").strip() or None,
        api_passphrase=(env.get("PM_API_PASSPHRASE") or "").strip() or None,
        clob_host=(env.get("PM_CLOB_HOST") or DEFAULT_CLOB_HOST).strip().rstrip("/"),
    )


@dataclass(frozen=True)
class TelegramSettings:
    token: str = field(repr=False)
    chat_id: str
    thread_id: Optional[int] = None
    commands: bool = False


def telegram_from_env(env: Mapping[str, str] = os.environ) -> Optional[TelegramSettings]:
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return None
    thread_raw = (env.get("TELEGRAM_THREAD_ID") or "").strip()
    try:
        thread_id = int(thread_raw) if thread_raw else None
    except ValueError:
        raise ConfigError(f"TELEGRAM_THREAD_ID must be an integer, got {thread_raw!r}") from None
    commands = (env.get("TELEGRAM_COMMANDS") or "").strip().lower() in ("1", "true", "yes", "on")
    return TelegramSettings(token=token, chat_id=chat_id, thread_id=thread_id, commands=commands)


def load_env_file(path: Optional[Path | str] = None) -> Optional[Path]:
    """Load KEY=VALUE pairs into os.environ without overriding variables that are already set."""
    from dotenv import load_dotenv

    if path is not None:
        candidate = Path(path)
        if not candidate.is_file():
            raise ConfigError(f"env file not found: {candidate}")
    elif os.environ.get("BTC5M_ENV_FILE"):
        candidate = Path(os.environ["BTC5M_ENV_FILE"])
        if not candidate.is_file():
            raise ConfigError(f"BTC5M_ENV_FILE points to a missing file: {candidate}")
    else:
        candidate = DEFAULT_ENV_FILE
        if not candidate.is_file():
            return None
    load_dotenv(candidate, override=False)
    return candidate
