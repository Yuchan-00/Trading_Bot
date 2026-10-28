"""설정과 하드 리밋.

``HardLimits``는 상수 전용이며 환경변수·코드로 바꿀 수 없다(메타클래스가 대입을 막는다).
``Settings``는 ``.env``/환경변수에서 읽은 운용 설정이다. 비밀값은 ``redacted()``로만 로그에 낸다.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal

Mode = Literal["paper", "testnet", "live"]
Tier = Literal["analysis", "research", "head"]

DEFAULT_SYMBOLS = "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT"
DEFAULT_NEWS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
)
_DATE_SUFFIX = re.compile(r"-\d{8}$")


class ConfigError(Exception):
    """잘못된/위험한 설정. 봇은 시작하지 않는다."""


def mask_secret(value: str | None, keep: int = 2) -> str:
    """비밀값을 로그용으로 가린다. 앞 ``keep``자와 길이만 남긴다."""
    if value is None or value == "":
        return "(없음)"
    value = str(value)
    if len(value) <= keep * 2:
        return "**"
    return f"{value[:keep]}…(len={len(value)})"


class _FrozenMeta(type):
    def __setattr__(cls, name, value):  # noqa: D105
        raise AttributeError("HardLimits는 변경할 수 없습니다")

    def __delattr__(cls, name):  # noqa: D105
        raise AttributeError("HardLimits는 변경할 수 없습니다")


class HardLimits(metaclass=_FrozenMeta):
    """코드가 강제하는 한도. LLM·설정·대시보드 어디서도 풀 수 없다."""

    MAX_POSITION_PCT = 0.10
    MAX_POSITIONS = 3
    MAX_STOP_LOSS_PCT = 0.02
    MAX_TAKE_PROFIT_PCT = 0.04
    DAILY_LOSS_LIMIT_PCT = 0.03
    MAX_CONSECUTIVE_LOSSES = 3
    MIN_CONFIDENCE = 0.65
    COOLDOWN_CYCLES = 4
    REVENGE_WINDOW_CYCLES = 8
    TIME_STOP_CYCLES = 96
    DRAWDOWN_CAUTION = 0.03
    DRAWDOWN_BLOCK = 0.045
    DRAWDOWN_STOP_ALL = 0.06
    RISK_PER_TRADE_PCT = 0.0015
    MIN_NET_RR = 1.5
    FEE_PCT = 0.001
    SLIPPAGE_PCT = 0.0005
    MIN_ORDER_USDT = 10.0
    ORDER_TYPES = ("market",)
    MARKET_TYPES = ("spot",)
    ENTRY_SIDES = ("buy",)

    def __init__(self) -> None:
        raise TypeError("HardLimits는 인스턴스를 만들 수 없습니다")


@dataclass(frozen=True)
class Settings:
    mode: Mode
    confirm_live: bool
    symbols: tuple[str, ...]
    cycle_minutes: int
    paper_start_balance: float
    max_candidates: int
    agent_backend: Literal["anthropic", "mock"]
    anthropic_api_key: str | None
    model_analysis: str
    model_research: str
    model_head: str
    llm_daily_budget_usd: float
    llm_monthly_budget_usd: float
    binance_api_key: str | None
    binance_api_secret: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    dashboard_host: str
    dashboard_port: int
    data_dir: Path
    news_feeds: tuple[str, ...]

    _SECRET_FIELDS = ("anthropic_api_key", "binance_api_key", "binance_api_secret", "telegram_bot_token")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def model_for(self, tier: Tier) -> str:
        if tier == "analysis":
            return self.model_analysis
        if tier == "research":
            return self.model_research
        if tier == "head":
            return self.model_head
        raise ValueError(f"알 수 없는 tier: {tier!r}")

    def redacted(self) -> dict:
        out: dict = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            v = getattr(self, f.name)
            if f.name in self._SECRET_FIELDS:
                v = mask_secret(v)
            elif isinstance(v, Path):
                v = str(v)
            out[f.name] = v
        return out

    def __repr__(self) -> str:  # 실수로 print해도 비밀이 안 나오게
        return f"Settings({self.redacted()})"


# ---------------------------------------------------------------- 파싱 도우미
def _get(env: Mapping[str, str], key: str, default: str = "") -> str:
    v = env.get(key)
    if v is None:
        return default
    return v.strip()


def _bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    v = _get(env, key).lower()
    if v == "":
        return default
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"{key}: 불리언이 아님: {v!r}")


def _int(env: Mapping[str, str], key: str, default: int, *, minimum: int | None = None) -> int:
    v = _get(env, key)
    try:
        out = int(v) if v != "" else default
    except ValueError as e:
        raise ConfigError(f"{key}: 정수가 아님: {v!r}") from e
    if minimum is not None and out < minimum:
        raise ConfigError(f"{key}: {minimum} 이상이어야 함 (현재 {out})")
    return out


def _float(env: Mapping[str, str], key: str, default: float, *, positive: bool = False) -> float:
    v = _get(env, key)
    try:
        out = float(v) if v != "" else default
    except ValueError as e:
        raise ConfigError(f"{key}: 숫자가 아님: {v!r}") from e
    if out != out or out in (float("inf"), float("-inf")):
        raise ConfigError(f"{key}: 유한한 숫자여야 함")
    if positive and out <= 0:
        raise ConfigError(f"{key}: 0보다 커야 함 (현재 {out})")
    return out


def _secret(env: Mapping[str, str], key: str) -> str | None:
    v = _get(env, key)
    return v or None


def _symbols(raw: str) -> tuple[str, ...]:
    out: list[str] = []
    for part in raw.split(","):
        s = part.strip().upper()
        if not s:
            continue
        if "/" not in s:
            raise ConfigError(f"SYMBOLS: 'BASE/QUOTE' 형식이 아님: {s!r}")
        base, quote = s.split("/", 1)
        if not base.isalnum() or quote != "USDT":
            raise ConfigError(f"SYMBOLS: USDT 현물 페어만 허용: {s!r}")
        if s not in out:
            out.append(s)
    if not out:
        raise ConfigError("SYMBOLS: 비어 있음")
    return tuple(out)


def _model(env: Mapping[str, str], key: str, default: str) -> str:
    v = _get(env, key) or default
    if not v.startswith("claude-"):
        raise ConfigError(f"{key}: Claude 모델 ID가 아님: {v!r}")
    if _DATE_SUFFIX.search(v):
        raise ConfigError(f"{key}: 날짜 접미사가 붙은 모델 ID는 쓰지 않음: {v!r}")
    return v


def _feeds(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return DEFAULT_NEWS_FEEDS
    feeds = tuple(u.strip() for u in raw.split(",") if u.strip())
    for u in feeds:
        if not (u.startswith("http://") or u.startswith("https://")):
            raise ConfigError(f"NEWS_FEEDS: URL이 아님: {u!r}")
    return feeds


def _read_env(dotenv_path: Path | None) -> dict[str, str]:
    """os.environ 위에 .env를 얹되, 이미 있는 환경변수가 우선."""
    merged: dict[str, str] = dict(os.environ)
    path = dotenv_path if dotenv_path is not None else Path(".env")
    if path.exists():
        try:
            from dotenv import dotenv_values
        except ImportError:  # pragma: no cover - 의존성은 requirements에 있음
            dotenv_values = None  # type: ignore[assignment]
        if dotenv_values is not None:
            for k, v in dotenv_values(path).items():
                if v is not None and k not in merged:
                    merged[k] = v
    return merged


def load_settings(env: Mapping[str, str] | None = None, *, dotenv_path: Path | None = None) -> Settings:
    """환경(또는 주어진 매핑)에서 Settings를 만든다. 위험한 조합은 ConfigError."""
    if env is None:
        src: Mapping[str, str] = _read_env(dotenv_path)
    elif dotenv_path is not None:
        merged = dict(env)
        if dotenv_path.exists():
            from dotenv import dotenv_values

            for k, v in dotenv_values(dotenv_path).items():
                if v is not None and k not in merged:
                    merged[k] = v
        src = merged
    else:
        src = env

    mode = (_get(src, "MODE", "paper").lower() or "paper")
    if mode not in ("paper", "testnet", "live"):
        raise ConfigError(f"MODE: paper/testnet/live 중 하나여야 함: {mode!r}")
    confirm_live = _get(src, "CONFIRM_LIVE").upper() == "YES"
    if mode == "live" and not confirm_live:
        raise ConfigError("MODE=live 에는 CONFIRM_LIVE=YES 가 필요합니다")

    backend = (_get(src, "AGENT_BACKEND", "anthropic").lower() or "anthropic")
    if backend not in ("anthropic", "mock"):
        raise ConfigError(f"AGENT_BACKEND: anthropic/mock 중 하나여야 함: {backend!r}")
    api_key = _secret(src, "ANTHROPIC_API_KEY")
    if backend == "anthropic" and not api_key:
        raise ConfigError("AGENT_BACKEND=anthropic 에는 ANTHROPIC_API_KEY 가 필요합니다 (오프라인 테스트는 AGENT_BACKEND=mock)")

    if mode == "testnet":
        b_key, b_sec = _secret(src, "BINANCE_TESTNET_API_KEY"), _secret(src, "BINANCE_TESTNET_API_SECRET")
        if not (b_key and b_sec):
            raise ConfigError("MODE=testnet 에는 BINANCE_TESTNET_API_KEY/_SECRET 이 필요합니다")
    elif mode == "live":
        b_key, b_sec = _secret(src, "BINANCE_LIVE_API_KEY"), _secret(src, "BINANCE_LIVE_API_SECRET")
        if not (b_key and b_sec):
            raise ConfigError("MODE=live 에는 BINANCE_LIVE_API_KEY/_SECRET 이 필요합니다")
    else:
        b_key, b_sec = None, None

    data_dir = Path(_get(src, "DATA_DIR") or ".").expanduser()

    return Settings(
        mode=mode,  # type: ignore[arg-type]
        confirm_live=confirm_live,
        symbols=_symbols(_get(src, "SYMBOLS", DEFAULT_SYMBOLS) or DEFAULT_SYMBOLS),
        cycle_minutes=_int(src, "CYCLE_MINUTES", 15, minimum=1),
        paper_start_balance=_float(src, "PAPER_START_BALANCE_USDT", 10000.0, positive=True),
        max_candidates=_int(src, "MAX_CANDIDATES_PER_CYCLE", 2, minimum=1),
        agent_backend=backend,  # type: ignore[arg-type]
        anthropic_api_key=api_key,
        model_analysis=_model(src, "MODEL_ANALYSIS", "claude-haiku-5-5"),
        model_research=_model(src, "MODEL_RESEARCH", "claude-sonnet-5-5"),
        model_head=_model(src, "MODEL_HEAD", "claude-opus-5-5"),
        llm_daily_budget_usd=_float(src, "LLM_DAILY_BUDGET_USD", 10.0, positive=True),
        llm_monthly_budget_usd=_float(src, "LLM_MONTHLY_BUDGET_USD", 200.0, positive=True),
        binance_api_key=b_key,
        binance_api_secret=b_sec,
        telegram_bot_token=_secret(src, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_secret(src, "TELEGRAM_CHAT_ID"),
        dashboard_host=_get(src, "DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1",
        dashboard_port=_int(src, "DASHBOARD_PORT", 8787, minimum=1),
        data_dir=data_dir,
        news_feeds=_feeds(_get(src, "NEWS_FEEDS")),
    )
