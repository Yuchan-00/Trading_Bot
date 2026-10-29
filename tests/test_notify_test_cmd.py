"""main ``notify-test`` — 텔레그램 연결 테스트 명령 (http_post 주입, 네트워크 없음)."""
from __future__ import annotations

import io

from tradingbot import main as M

TOKEN = "123456789:AAH-SuperSecretTelegramToken_xyz"
CHAT = "987654321"


def env_for(tmp_path, **extra) -> dict:
    # AGENT_BACKEND=anthropic 인데 ANTHROPIC_API_KEY 없음 → load_settings 는 실패하지만 notify-test 는 동작해야 한다
    env = {"AGENT_BACKEND": "anthropic", "MODE": "paper", "DATA_DIR": str(tmp_path / "data"),
           "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
    env.update(extra)
    return env


def call(env, http_post):
    out, err = io.StringIO(), io.StringIO()
    code = M.main(["notify-test"], env=env, out=out, err=err, http_post=http_post)
    return code, out.getvalue(), err.getvalue()


class Recorder:
    def __init__(self, result=200, exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = result
        self.exc = exc

    def __call__(self, url: str, payload: dict):
        self.calls.append((url, payload))
        if self.exc is not None:
            raise self.exc
        return self.result


def test_notify_test_success_without_anthropic_key(tmp_path):
    rec = Recorder(200)
    code, out, err = call(env_for(tmp_path, MODE="testnet"), rec)
    assert code == 0
    assert len(rec.calls) == 1
    url, payload = rec.calls[0]
    assert url.endswith("/sendMessage") and payload["chat_id"] == CHAT
    assert payload["text"] == "✅ 텔레그램 연결 테스트 [testnet] — 이 메시지가 보이면 거래 알림이 정상 전송됩니다"
    assert "성공" in out
    assert TOKEN not in out + err and CHAT not in out + err


def test_notify_test_http_error_masked(tmp_path):
    rec = Recorder(401)
    code, out, err = call(env_for(tmp_path), rec)
    assert code == 1
    assert "텔레그램 전송 실패" in err and "401" in err
    assert TOKEN not in out + err


def test_notify_test_exception_with_token_in_message_masked(tmp_path):
    rec = Recorder(exc=OSError(f"connect failed https://api.telegram.org/bot{TOKEN}/sendMessage token={TOKEN}"))
    code, out, err = call(env_for(tmp_path), rec)
    assert code == 1
    assert "텔레그램 전송 실패" in err
    assert TOKEN not in out + err and TOKEN[10:30] not in err


def test_notify_test_rate_limited_is_failure(tmp_path):
    code, out, err = call(env_for(tmp_path), Recorder((429, 5)))
    assert code == 1 and "429" in err and TOKEN not in err


def test_notify_test_missing_env(tmp_path):
    rec = Recorder(200)
    code, out, err = call(env_for(tmp_path, TELEGRAM_BOT_TOKEN=""), rec)
    assert code == 2 and rec.calls == []
    assert "TELEGRAM_BOT_TOKEN" in err and "비어 있습니다" in err and "TELEGRAM_CHAT_ID" not in err
    env = env_for(tmp_path)
    env.pop("TELEGRAM_CHAT_ID")
    code, out, err = call(env, rec)
    assert code == 2 and rec.calls == [] and "TELEGRAM_CHAT_ID" in err and TOKEN not in err
    code, out, err = call({"MODE": "paper", "TELEGRAM_BOT_TOKEN": "  ", "TELEGRAM_CHAT_ID": ""}, rec)
    assert code == 2 and "TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID" in err


def test_notify_test_default_mode_paper(tmp_path):
    rec = Recorder(200)
    env = env_for(tmp_path)
    env.pop("MODE")
    code, _, _ = call(env, rec)
    assert code == 0 and "[paper]" in rec.calls[0][1]["text"]


def test_notify_test_in_parser():
    args = M.build_parser().parse_args(["notify-test"])
    assert args.command == "notify-test"
