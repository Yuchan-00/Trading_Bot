"""적대적 리뷰 테스트 — tradingbot/skills.py, tradingbot/reports.py (SPEC §9, §12).

각 테스트는 현재 코드에서 실패해야 하며, 결함이 고쳐지면 통과한다. 전부 오프라인, tmp_path만 사용.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tradingbot import reports
from tradingbot.models import BotState, DailyStats
from tradingbot.notify import Notifier
from tradingbot.skills import SkillRegistry, parse_frontmatter


def _make_skill(root: Path, name: str, body: str, roles: str = "[analyst]") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(f"---\nname: {name}\nroles: {roles}\nversion: 1\n---\n{body}\n", encoding="utf-8")
    return p


def _state(**daily) -> BotState:
    base = dict(date="2026-10-28", start_equity=10_000.0, realized_pnl=12.5, trades=2, wins=1, losses=1)
    base.update(daily)
    return BotState(mode="paper", daily=DailyStats(**base))


# ---------------------------------------------------------------- R1: daily_summary <-> notify 키 불일치
class TestDailySummaryLlmCostKey:
    def test_llm_cost_reaches_telegram_daily_summary(self):
        """notify._format_daily_summary 는 extra['llm_cost_usd'] 를 읽는데 reports 는 'cost_usd' 로 써서
        텔레그램 일일 요약에 LLM 비용 줄이 절대 나오지 않는다."""
        ev = reports.daily_summary(_state(llm_cost_usd=0.5), 10_012.5, 1.25)
        text = Notifier.format(ev)
        assert "LLM 비용" in text, text
        assert "1.25" in text
        assert isinstance(ev.extra.get("llm_cost_usd"), float)


# ---------------------------------------------------------------- R2: 들여쓰지 않은 YAML 목록 거부
class TestUnindentedYamlList:
    def test_frontmatter_accepts_list_items_at_column_zero(self):
        """``roles:`` 아래 ``- analyst`` 를 들여쓰지 않는 것은 가장 흔한 YAML 블록 목록 문법인데
        파서가 SkillError 를 던져 스킬 전체가 (ERROR 로그만 남기고) 조용히 빠진다."""
        meta, body = parse_frontmatter("---\nname: x\nroles:\n- analyst\n- head\n---\nbody")
        assert meta["roles"] == ["analyst", "head"]
        assert body.strip() == "body"

    def test_registry_loads_skill_with_unindented_list(self, tmp_path: Path):
        d = tmp_path / "trend"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: trend\nroles:\n- analyst\nversion: 1\n---\n# Trend\nrules\n", encoding="utf-8"
        )
        reg = SkillRegistry(tmp_path)
        assert reg.errors == {}
        assert [s.name for s in reg.for_role("analyst")] == ["trend"]


# ---------------------------------------------------------------- R3: 여러 줄 plain scalar 거부
class TestPlainScalarContinuation:
    def test_key_with_indented_continuation_only(self):
        """``description:`` 뒤 줄바꿈 후 들여쓴 문장(YAML plain multi-line scalar)은 유효한 문법이지만
        현재는 빈 목록으로 시작한 뒤 이어쓰기 줄에서 SkillError 가 난다."""
        meta, _ = parse_frontmatter("---\nname: x\ndescription:\n  long text\n  more text\n---\nbody")
        assert meta["description"] == "long text more text"


# ---------------------------------------------------------------- R4: 보고서가 잘못된 입력에 크래시
class TestCycleReportBadInput:
    def test_inf_llm_calls_does_not_crash(self, tmp_path: Path):
        """render_cycle_markdown 의 int(llm_calls) 는 TypeError/ValueError 만 잡아서 inf 면 OverflowError 로
        write_cycle_report 전체(JSON 포함 — JSON 은 이미 써진 뒤 .md 단계에서 터진다)가 실패한다."""
        p = reports.write_cycle_report(tmp_path, 1, {"ts": 1_700_000_000_000, "llm_calls": float("inf")})
        assert p.exists()
        assert p.with_suffix(".md").exists()


# ---------------------------------------------------------------- R5: 임의 객체 repr 이 보고서에 그대로 기록
class TestNoArbitraryReprInReports:
    def test_unknown_object_repr_not_written_verbatim(self, tmp_path: Path):
        """_json_default 가 모르는 객체를 str(obj) 로 떨어뜨려, repr 에 키가 들어있는 객체(SDK 클라이언트 등)를
        payload 에 실수로 넣으면 비밀이 logs/ 에 평문으로 남는다 (SPEC §0: 리포트에 비밀 금지)."""

        class Client:
            def __repr__(self) -> str:
                return "Client(api_key='sk-ant-SECRET123')"

        p = reports.write_cycle_report(tmp_path, 2, {"ts": 1_700_000_000_000, "client": Client()})
        assert "sk-ant-SECRET123" not in p.read_text(encoding="utf-8")
        dec = tmp_path / "decisions.jsonl"
        reports.append_decision(dec, {"cycle": 2, "client": Client()})
        assert "sk-ant-SECRET123" not in dec.read_text(encoding="utf-8")


# ---------------------------------------------------------------- R6: render 예산에 구분자 미포함
class TestRenderBudgetIncludesSeparators:
    def test_rendered_string_within_claimed_budget(self, tmp_path: Path):
        """구현자는 'len(out)//4 <= max_tokens 를 만족한다'고 적었지만 섹션별 floor 합산 + '\\n\\n' 구분자
        미포함 때문에 결과 문자열이 예산을 넘는다."""
        for n in ("a", "b", "c"):
            _make_skill(tmp_path, n, "xyz")  # "## Skill: a\nxyz" -> 15자 -> 3토큰(floor)
        reg = SkillRegistry(tmp_path)
        out = reg.render("analyst", max_tokens=9)
        assert len(out) // 4 <= 9, (len(out), out)
