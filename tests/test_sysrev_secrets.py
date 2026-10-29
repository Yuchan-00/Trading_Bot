"""시스템 리뷰 — 비밀값·데이터 위생 관점 (sysrev: secrets).

각 테스트는 현재 코드에서 실패해야 하는 결함 재현이다(고치기 전까지 빨간불). 전부 오프라인.
가짜 비밀값은 눈에 띄는 고유 문자열을 쓴다.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradingbot.config import load_settings
from tradingbot.dashboard import create_app

FAKE_TG = "555666777:SYSREV-TG-TOKEN-zzzz"


def _settings(data_dir: Path, **over):
    env = {"AGENT_BACKEND": "mock", "DATA_DIR": str(data_dir), "TELEGRAM_BOT_TOKEN": FAKE_TG,
           "TELEGRAM_CHAT_ID": "42"}
    env.update(over)
    return load_settings(env)


# ======================================================================= S1 토큰 파일이 쓰이는 순간 다른 사용자에게 읽힘
@pytest.mark.skipif(os.name == "nt", reason="POSIX 권한 비트(0600)는 Windows 에 없음 — Windows 는 사용자 프로필 ACL 로 보호")
def test_s1_dashboard_token_never_group_or_world_readable_while_on_disk(tmp_path: Path, monkeypatch):
    """``create_app`` 은 토큰을 ``write_text``(umask 기본 0644)로 먼저 쓰고 나서 ``chmod(0o600)`` 한다.

    토큰 내용이 디스크에 있는 동안 파일이 group/other 읽기 가능 → 같은 호스트의 다른 사용자가 토큰을 읽어
    ``POST /api/kill/clear`` 로 (낙폭 정지 포함) 킬스위치를 해제할 수 있는 창이 생긴다. chmod 가 실패하면
    (OSError 를 삼킴) 그 상태로 영구히 남는다. 토큰은 0600 으로 *생성*되어야 한다(os.open(..., 0o600)).
    """
    observed: list[int] = []
    real_write_text = Path.write_text

    def spy_write_text(self: Path, data, *a, **kw):
        n = real_write_text(self, data, *a, **kw)
        if self.name == "dashboard_token":
            observed.append(stat.S_IMODE(os.stat(self).st_mode))
        return n

    monkeypatch.setattr(Path, "write_text", spy_write_text)
    old = os.umask(0o022)  # 일반적인 기본 umask
    try:
        data_dir = tmp_path / "d"
        create_app(_settings(data_dir), data_dir)
    finally:
        os.umask(old)
    assert observed, "토큰 파일이 write_text 로 쓰이지 않음(테스트 가정 확인)"
    assert all(m & 0o077 == 0 for m in observed), f"토큰이 디스크에 있는 동안 권한 {oct(observed[0])}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX 권한 비트(0600)는 Windows 에 없음 — Windows 는 사용자 프로필 ACL 로 보호")
def test_s1b_dashboard_token_left_readable_when_chmod_fails(tmp_path: Path, monkeypatch):
    """chmod 실패를 조용히 삼켜 토큰이 0644 로 남는다(경고도 없음)."""
    real_chmod = Path.chmod

    def failing_chmod(self: Path, mode, *a, **kw):
        if self.name == "dashboard_token":
            raise PermissionError("chmod not permitted")
        return real_chmod(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "chmod", failing_chmod)
    old = os.umask(0o022)
    try:
        data_dir = tmp_path / "d"
        create_app(_settings(data_dir), data_dir)
    finally:
        os.umask(old)
    mode = stat.S_IMODE(os.stat(data_dir / "control" / "dashboard_token").st_mode)
    assert mode & 0o077 == 0, f"토큰 파일 권한 {oct(mode)} — 다른 사용자가 읽을 수 있음"


# ======================================================================= S2 Host 헤더 미검증(DNS 리바인딩)
@pytest.mark.parametrize("path", ["/api/state", "/api/trades", "/api/journal", "/api/costs"])
def test_s2_dashboard_rejects_foreign_host_header(tmp_path: Path, path: str):
    """대시보드는 localhost 바인딩만으로 '로컬 전용'을 가정하지만 Host 헤더를 검증하지 않는다.

    악성 웹페이지가 DNS 리바인딩(attacker.example → 127.0.0.1)으로 같은 출처가 되면 인증 없는 GET
    엔드포인트(포지션·잔고·거래 내역·저널·비용)를 그대로 읽어 간다. 127.0.0.1/localhost 외 Host 는
    거부(400/403/421)되어야 한다(예: TrustedHostMiddleware).
    """
    data_dir = tmp_path / "d"
    c = TestClient(create_app(_settings(data_dir), data_dir))
    r = c.get(path, headers={"Host": "attacker.example:8787"})
    assert r.status_code in (400, 403, 421), f"외부 Host 로 {path} 응답 {r.status_code}"


def test_s1c_token_permission_narrowing_is_posix_only(tmp_path: Path, monkeypatch, caplog):
    """POSIX 권한 비트가 없는 플랫폼(Windows)에서는 chmod 하지 않고 헛된 경고도 남기지 않는다."""
    import tradingbot.dashboard.app as app_mod

    monkeypatch.setattr(app_mod, "POSIX_PERMISSIONS", False)
    calls: list[str] = []
    real_chmod = Path.chmod

    def spy_chmod(self: Path, mode, *a, **kw):
        calls.append(self.name)
        return real_chmod(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "chmod", spy_chmod)
    data_dir = tmp_path / "d"
    (data_dir / "control").mkdir(parents=True)
    token = data_dir / "control" / "dashboard_token"
    token.write_text("existing-token-value-123456", encoding="utf-8")
    with caplog.at_level("WARNING"):
        create_app(_settings(data_dir), data_dir)
    assert "dashboard_token" not in calls
    assert not [r for r in caplog.records if "권한" in r.getMessage()]
    assert token.read_text(encoding="utf-8") == "existing-token-value-123456"
