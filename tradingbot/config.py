"""설정과 하드 리밋.

``HardLimits``는 상수 전용이며 환경변수·코드로 바꿀 수 없다(메타클래스가 대입을 막는다).
``Settings``는 ``.env``/환경변수에서 읽은 운용 설정이다. 비밀값은 ``redacted()``로만 로그에 낸다.
"""
from __future__ import annotations

import codecs
import io
import locale
import logging
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal

log = logging.getLogger("tradingbot.config")

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
    log_file: Path | None = None                     # LOG_FILE(상대 경로면 DATA_DIR 기준). None 이면 콘솔만
    scanner_env: tuple[tuple[str, str], ...] = ()    # SCANNER_* 원문(.env 포함) → scanner.Thresholds.from_env

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


_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
_PERCENT_VAR = re.compile(r"%[A-Za-z_][A-Za-z0-9_()]*%")
_WARNED_OVERRIDES: set[tuple[str, ...]] = set()
# 해석 실패 줄의 안내: ``NAME=`` 꼴이면(대소문자 무관) 따옴표 문제, ``set NAME=`` 이면 cmd 문법. 메시지에 보여 주는
# 이름은 대문자 설정 이름(최대 40자)뿐 — ``KEY: 값``·``KEY 값``·비밀값으로 시작하는 줄은 줄 번호만(비밀값이 안 나오게).
_DOTENV_ASSIGN = re.compile(r"((?i:export|set)\s+)?([A-Za-z_][A-Za-z0-9_]{0,63})\s*=")
_SHOWN_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,39}")
# 복사·붙여넣기로 섞이는 보이지 않는 글자(유니코드 서식 문자 Cf — U+200B·U+FEFF·U+2060·U+200E 등 — 와 한글 채움 문자)
_INVISIBLE_EXTRA = frozenset("\u3164\uffa0")
# UTF-16 으로 푼 글에서 이 블록(구르무키·구자라트·말라얄람·싱할라)의 글자는 덧붙인 8비트 줄의 CR·LF 바이트가 만든 것
_MIXED_UTF16 = re.compile(r"[\u0a00-\u0aff\u0d00-\u0dff]")
_NEWLINES = re.compile(r"\r\n|\n|\r")
_UTF8_FIX = "메모장에서 '다른 이름으로 저장' → 인코딩 'UTF-8' 로 다시 저장하세요"


def _visible(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf" and ch not in _INVISIBLE_EXTRA)


def _dotenv_present(path: Path) -> bool:
    # 폴더(``python -m venv .env`` 로 만든 가상환경 등)는 없는 것으로 친다(python-dotenv 도 건너뛴다). FIFO 는 읽는다.
    return path.exists() and not path.is_dir()


def dotenv_location(dotenv_path: Path | None = None) -> tuple[Path, bool]:
    """읽을 .env 의 절대 경로(기본: 현재 작업 폴더의 ``.env``)와 존재 여부."""
    path = dotenv_path if dotenv_path is not None else Path(".env")
    return Path(os.path.abspath(path)), _dotenv_present(path)


def _decode_dotenv(data: bytes, path: Path, *, lenient: bool, warnings: list[str]) -> str:
    """.env 바이트 → 텍스트. Windows 도구가 만든 인코딩을 알아보고, 모르는 것은 분명한 오류로 알린다.

    - UTF-8(BOM 있든 없든) → 그대로. BOM 은 버린다(python-dotenv 1.2.2 이하는 첫 키에 BOM 이 붙는다).
    - UTF-16 BOM(Windows PowerShell 5.1 의 ``>`` / ``Out-File``) → 풀어서 쓰되 경고. 다른 도구로 8비트 줄을 덧붙여
      풀 수 없으면 strict 는 ``ConfigError``, lenient 는 못 읽는 글자를 바꿔 넣고 경고(stop·status 는 계속 동작).
    - NUL 바이트(BOM 없는 UTF-16 — PowerShell 5.1 ``>>`` 로 덧붙인 줄) → strict 는 ``ConfigError``, lenient 는 NUL 을
      지우고 경고(키가 조용히 사라지지 않게).
    - UTF-8 이 아님(메모장 ANSI = cp949 로 저장한 한글 주석 등) → strict 는 줄 번호를 담은 ``ConfigError``,
      lenient 는 로캘 인코딩(한국어 Windows 는 cp949)으로 읽고 경고.
    """
    fix = _UTF8_FIX
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as e:
            msg = f"{path}: UTF-16 으로 저장됐지만 읽을 수 없습니다(다른 인코딩 줄이 덧붙은 듯) — {fix}"
            if not lenient:
                raise ConfigError(msg) from e
            warnings.append(msg)
            return data.decode("utf-16", errors="replace")
        msg = f"{path}: UTF-16 으로 저장돼 있습니다(PowerShell '>' 로 만든 파일) — {fix}"
        warnings.append(msg)
        mixed = _MIXED_UTF16.search(text)
        if mixed is not None:
            # 짝수 길이로 풀린 덧붙인 줄이 앞 줄 값에 붙었다(그 설정은 읽히지 않는다) — 조용히 기본값으로 돌지 않게
            line = len(_NEWLINES.findall(text, 0, mixed.start())) + 1
            msg = (f"{path} {line}번째 줄에 다른 인코딩으로 덧붙인 글자가 섞였습니다(UTF-16 파일에 cmd·Git Bash 로 줄을 "
                   f"덧붙이면 그 설정이 읽히지 않습니다) — {fix}")
            if not lenient:
                raise ConfigError(msg)
            warnings.append(msg)
        return text
    if b"\x00" in data:
        line = data[:data.index(b"\x00")].count(b"\n") + 1
        msg = (f"{path} {line}번째 줄 근처에 NUL 바이트가 있습니다 — Windows PowerShell 5.1 의 '>'·'>>' 는 UTF-16 으로 "
               f"씁니다. {fix}")
        if not lenient:
            raise ConfigError(msg)
        warnings.append(msg)
        data = data.replace(b"\x00", b"")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        line = data[:e.start].count(b"\n") + 1
        msg = f"{path} {line}번째 줄이 UTF-8 이 아닙니다(ANSI/cp949 로 저장된 듯) — {fix}"
        if not lenient:
            raise ConfigError(msg) from e
        warnings.append(msg)
        enc = locale.getpreferredencoding(False) or "utf-8"
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            return data.decode("utf-8", errors="replace")


def _dotenv_line_problem(binding: object, s: str, before: str, path: Path) -> str | None:
    """python-dotenv 가 해석하지 못했거나(오류) 조용히 버릴(``=`` 없는 비ASCII 키) 줄의 안내. 문제없으면 None.

    줄 번호는 앞 줄 원문(``before``)에서 센다 — python-dotenv 의 ``original.line`` 은 그 줄 앞의 빈 줄부터 센다.
    이름은 ``NAME=`` 꼴일 때만 보여준다(``KEY: 값`` 이나 비밀값으로 시작하는 줄의 내용은 쓰지 않는다).
    """
    error = bool(getattr(binding, "error", False))
    key = getattr(binding, "key", None)
    # 보이지 않는 글자만 있는 줄(빈 줄·주석 앞의 U+200B 등)은 python-dotenv 처럼 무시한다
    garbled = (not error and isinstance(key, str) and not _visible(key).isascii()
               and getattr(binding, "value", "") is None)
    if not (error or garbled):
        return None
    body = s.lstrip()
    line = len(_NEWLINES.findall(before + s[:len(s) - len(body)])) + 1
    if garbled:
        return (f"{path} {line}번째 줄에 '=' 없는 글자만 있습니다 — 메모라면 줄 앞에 # 를 붙이세요. 아니면 인코딩이 "
                f"섞인 것입니다(UTF-16 파일에 cmd·Git Bash 로 줄을 덧붙이면 그 줄의 설정이 사라집니다) — {_UTF8_FIX}")
    m = _DOTENV_ASSIGN.match(_visible(_NEWLINES.split(body, 1)[0]))
    shown = m.group(2) if m and _SHOWN_NAME.fullmatch(m.group(2)) else None
    if m and m.group(1) and m.group(1).split()[0].lower() == "set":
        return (f"{path} {line}번째 줄{f'({shown})' if shown else ''}을 해석하지 못했습니다 — cmd 의 set 은 .env 에 "
                f"쓰지 않습니다. {shown or 'NAME'}=값 처럼 쓰세요")
    if m:
        return (f"{path} {line}번째 줄{f'({shown}=…)' if shown else ''}을 해석하지 못했습니다 — 닫히지 않은 따옴표이거나 "
                "따옴표 안의 값이 역슬래시(\\)로 끝납니다(닫는 따옴표 뒤에 다른 글자가 있어도 그렇습니다). 경로는 "
                "따옴표 없이 쓰거나 B:/TradingBot/data 처럼 / 를 쓰세요(그 줄 뒤의 설정이 함께 무시될 수 있습니다)")
    return (f"{path} {line}번째 줄을 해석하지 못했습니다 — KEY=value 형식이 아닙니다(':' 나 공백 대신 '=' 를 "
            "쓰세요)")


def _dotenv_items(path: Path, *, lenient: bool, warnings: list[str]) -> dict[str, str]:
    """.env 를 읽어 {키: 값}. 해석하지 못한 줄(닫히지 않은 따옴표 — 예: ``DATA_DIR="X:\\data\\"`` 는 닫는 따옴표가
    역슬래시로 이스케이프돼 다음 줄들까지 삼킨다)은 strict 면 ``ConfigError``, lenient 면 경고."""
    try:
        from dotenv import dotenv_values
        from dotenv.parser import parse_stream
    except ImportError:  # pragma: no cover - 의존성은 requirements 에 있음
        return {}
    try:
        data = path.read_bytes()
    except OSError as e:
        msg = f"{path} 를 읽지 못했습니다({type(e).__name__})"
        if not lenient:
            raise ConfigError(msg) from e
        warnings.append(msg)
        return {}
    text = _decode_dotenv(data, path, lenient=lenient, warnings=warnings)
    consumed = ""                   # 앞 줄들의 원문(바인딩 원문을 이어 붙이면 파일 전체 — 줄 번호를 여기서 센다)
    for binding in parse_stream(io.StringIO(text)):
        s = getattr(getattr(binding, "original", None), "string", "") or ""
        msg = _dotenv_line_problem(binding, s, consumed, path)
        consumed += s
        if msg:
            if not lenient:
                raise ConfigError(msg)
            warnings.append(msg)
    dotenv_log = logging.getLogger("dotenv.main")
    prev_disabled = dotenv_log.disabled
    dotenv_log.disabled = True      # 위에서 이미 한국어로 알렸다(라이브러리의 영어 경고 중복 방지)
    try:
        values = dotenv_values(stream=io.StringIO(text))
    finally:
        dotenv_log.disabled = prev_disabled
    out: dict[str, str] = {}
    cleaned: list[str] = []
    for k, v in values.items():
        if v is None:
            continue
        name = _visible(k).strip()
        if name != k:
            # 이름에 섞인 보이지 않는 글자(복사·붙여넣기) 때문에 설정이 조용히 무시되지 않게 지우고 읽는다
            cleaned.append(name if _SHOWN_NAME.fullmatch(name) else "?")
            if not name:
                continue
        out[name] = v
    if cleaned:
        warnings.append(f"{path}: 이름에 보이지 않는 글자(복사·붙여넣기)가 섞여 지우고 읽었습니다: {', '.join(cleaned)}")
    return out


def read_env(dotenv_path: Path | None = None, *, lenient: bool = False,
             warnings: list[str] | None = None) -> dict[str, str]:
    """os.environ 위에 .env(기본 ``./.env``)를 얹은 사본. 이미 있는 환경변수가 우선한다.

    값 검증은 하지 않는다 — ``load_settings`` 가 실패해도 ``DATA_DIR`` 같은 값만 꺼내 쓸 때(CLI stop/status) 쓴다.
    파일 자체가 깨졌으면(인코딩·해석 실패) 기본은 ``ConfigError``. ``lenient=True``(파일만 읽는 명령)는 최대한
    읽고 경고를 ``warnings`` 에 담는다. 같은 키가 환경변수와 .env 에 다른 값으로 있으면(창에 남은 ``set``·``$env:``)
    이름만 한 번 경고한다(값은 쓰지 않는다). .env 의 빈 값(``.env.example`` 의 ``ANTHROPIC_API_KEY=`` 같은 자리
    표시)은 설정하지 않은 것과 같으므로 덮어쓰기로 치지 않는다.
    """
    merged: dict[str, str] = dict(os.environ)
    path = dotenv_path if dotenv_path is not None else Path(".env")
    sink: list[str] = warnings if warnings is not None else []
    if _dotenv_present(path):
        items = _dotenv_items(path, lenient=lenient, warnings=sink)
        overridden: list[str] = []
        for k, v in items.items():
            if k in merged:
                if str(v).strip() and str(merged[k]).strip() != str(v).strip():
                    overridden.append(k)
            else:
                merged[k] = v
        if overridden:
            key = tuple(sorted(overridden))
            if key not in _WARNED_OVERRIDES:
                _WARNED_OVERRIDES.add(key)
                msg = (f"환경변수가 .env 값을 덮어씁니다: {', '.join(key)} — 콘솔 창에 남은 set/$env: 값이거나 setx 로 "
                       "넣은 영구 값일 수 있습니다(.env 를 쓰려면 그 환경변수를 지우세요 — README §4)")
                if warnings is not None:
                    warnings.append(msg)
                else:
                    log.warning("%s", msg)
    if warnings is None:
        for w in sink:
            log.warning("%s", w)
    return merged


_read_env = read_env  # 하위 호환 별칭


def parse_data_dir(raw: str | None, *, key: str = "DATA_DIR", default: str = ".") -> Path:
    """경로 설정값 검사. Windows 경로를 .env 에 쓸 때 생기는 실수를 분명한 오류로 바꾼다.

    - 큰따옴표 안의 ``\\t \\b \\n \\r`` 같은 역슬래시는 python-dotenv 가 이스케이프로 바꾼다 → 제어 문자·따옴표가
      섞인 값은 ``ConfigError``(그대로 쓰면 엉뚱한 폴더·WinError 123).
    - ``%USERPROFILE%`` 같은 cmd 변수는 펼쳐지지 않는다 → ``ConfigError``(``${USERPROFILE}`` 은 python-dotenv 가 펼친다).
    """
    v = (raw or "").strip()
    if not v:
        return Path(default).expanduser()
    if _CONTROL_CHARS.search(v) or '"' in v:
        raise ConfigError(
            f"{key}: 경로에 제어 문자나 따옴표가 섞였습니다({v!r}) — .env 의 큰따옴표 안에서는 \\t \\b \\n \\r 같은 "
            f"역슬래시가 이스케이프로 바뀝니다. 따옴표 없이 쓰거나 B:/TradingBot/data 처럼 / 를 쓰세요")
    if _PERCENT_VAR.search(v):
        raise ConfigError(f"{key}: %변수% 는 펼쳐지지 않습니다({v!r}) — 실제 경로를 쓰거나 ${{USERPROFILE}} 형식을 쓰세요")
    return Path(v).expanduser()


def _log_file(src: Mapping[str, str], data_dir: Path) -> Path | None:
    raw = _get(src, "LOG_FILE")
    if not raw or raw.lower() in ("none", "off", "false", "0"):
        return None
    p = parse_data_dir(raw, key="LOG_FILE")
    return p if p.is_absolute() else data_dir / p


def load_settings(env: Mapping[str, str] | None = None, *, dotenv_path: Path | None = None) -> Settings:
    """환경(또는 주어진 매핑)에서 Settings를 만든다. 위험한 조합은 ConfigError."""
    if env is None:
        src: Mapping[str, str] = read_env(dotenv_path)
    elif dotenv_path is not None:
        merged = dict(env)
        if _dotenv_present(dotenv_path):
            warnings: list[str] = []
            for k, v in _dotenv_items(dotenv_path, lenient=False, warnings=warnings).items():
                if k not in merged:
                    merged[k] = v
            for w in warnings:
                log.warning("%s", w)
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

    data_dir = parse_data_dir(_get(src, "DATA_DIR"))
    log_file = _log_file(src, data_dir)
    scanner_env = tuple(sorted((k, str(v)) for k, v in src.items() if k.startswith("SCANNER_") and v is not None))

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
        log_file=log_file,
        scanner_env=scanner_env,
    )
