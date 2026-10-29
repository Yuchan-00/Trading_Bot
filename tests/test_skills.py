"""tradingbot.skills 테스트 (SPEC §9). 전부 오프라인, tmp_path 사용."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tradingbot.skills import (
    Skill,
    SkillError,
    SkillRegistry,
    load_skill,
    parse_frontmatter,
)


# ---------------------------------------------------------------- 도우미
def make_skill(root: Path, dirname: str, body: str = "본문입니다.", *, frontmatter: str | None = None, **meta) -> Path:
    """skills/<dirname>/SKILL.md 생성. frontmatter를 직접 주거나 meta로 조립."""
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    if frontmatter is None:
        lines = ["---"]
        for k, v in meta.items():
            if isinstance(v, list):
                lines.append(f"{k}: [{', '.join(v)}]")
            else:
                lines.append(f"{k}: {v}")
        lines.append("---")
        frontmatter = "\n".join(lines)
    p = d / "SKILL.md"
    p.write_text(frontmatter + "\n" + body + "\n", encoding="utf-8")
    return p


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "skills"
    r.mkdir()
    return r


# ---------------------------------------------------------------- parse_frontmatter
class TestParseFrontmatter:
    def test_key_value_and_body(self):
        meta, body = parse_frontmatter("---\nname: alpha\ndescription: 설명\n---\n본문\n둘째 줄\n")
        assert meta == {"name": "alpha", "description": "설명"}
        assert body == "본문\n둘째 줄\n"

    def test_inline_list(self):
        meta, _ = parse_frontmatter("---\nroles: [analyst, head]\n---\nx")
        assert meta["roles"] == ["analyst", "head"]

    def test_inline_list_with_quotes_and_spaces(self):
        meta, _ = parse_frontmatter('---\nroles: [ "analyst" ,  \'head\' , ]\n---\nx')
        assert meta["roles"] == ["analyst", "head"]

    def test_empty_inline_list(self):
        meta, _ = parse_frontmatter("---\nroles: []\n---\nx")
        assert meta["roles"] == []

    def test_yaml_block_list(self):
        meta, _ = parse_frontmatter("---\nroles:\n  - analyst\n  - head\nversion: 2\n---\nx")
        assert meta["roles"] == ["analyst", "head"]
        assert meta["version"] == "2"

    def test_yaml_block_list_quoted_items(self):
        meta, _ = parse_frontmatter('---\nroles:\n  - "analyst"\n  - \'risk\'\n---\nx')
        assert meta["roles"] == ["analyst", "risk"]

    def test_key_without_value_is_empty_list(self):
        meta, _ = parse_frontmatter("---\nroles:\nname: a\n---\nx")
        assert meta["roles"] == []
        assert meta["name"] == "a"

    def test_quoted_scalars(self):
        meta, _ = parse_frontmatter("---\nname: \"quoted name\"\nversion: '1.0'\n---\nx")
        assert meta["name"] == "quoted name"
        assert meta["version"] == "1.0"

    def test_values_stay_strings(self):
        meta, _ = parse_frontmatter("---\nversion: 1.0\nflag: true\n---\nx")
        assert meta["version"] == "1.0"
        assert meta["flag"] == "true"

    def test_null_values(self):
        meta, _ = parse_frontmatter("---\nsource: null\nlicense: ~\n---\nx")
        assert meta["source"] is None
        assert meta["license"] is None

    def test_comments_and_blank_lines_ignored(self):
        meta, _ = parse_frontmatter("---\n# 주석\nname: a  # 뒤 주석\n\nroles: [x] # c\n---\nx")
        assert meta == {"name": "a", "roles": ["x"]}

    def test_hash_inside_url_kept(self):
        meta, _ = parse_frontmatter("---\nsource: https://ex.com/a#frag\n---\nx")
        assert meta["source"] == "https://ex.com/a#frag"

    def test_hash_inside_quotes_kept(self):
        meta, _ = parse_frontmatter('---\ndescription: "a # b"\n---\nx')
        assert meta["description"] == "a # b"

    def test_no_frontmatter_returns_whole_text(self):
        text = "# 제목\n본문"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_leading_blank_lines_and_bom_allowed(self):
        meta, body = parse_frontmatter("﻿\n\n---\nname: a\n---\nb")
        assert meta == {"name": "a"}
        assert body == "b"

    def test_crlf_lines(self):
        meta, body = parse_frontmatter("---\r\nname: a\r\nroles:\r\n  - x\r\n---\r\nbody\r\n")
        assert meta["name"] == "a"
        assert meta["roles"] == ["x"]
        assert body.strip() == "body"

    def test_unterminated_frontmatter_raises(self):
        with pytest.raises(SkillError):
            parse_frontmatter("---\nname: a\nbody without closing")

    def test_bad_syntax_raises(self):
        with pytest.raises(SkillError):
            parse_frontmatter("---\nthis is not a key value\n---\nx")

    def test_duplicate_key_raises(self):
        with pytest.raises(SkillError):
            parse_frontmatter("---\nname: a\nname: b\n---\nx")

    def test_body_dashes_do_not_confuse(self):
        meta, body = parse_frontmatter("---\nname: a\n---\n---\n본문 안의 구분선\n---\n")
        assert meta == {"name": "a"}
        assert "본문 안의 구분선" in body

    def test_colon_in_value(self):
        meta, _ = parse_frontmatter("---\ndescription: 비율: 2:1 손익비\n---\nx")
        assert meta["description"] == "비율: 2:1 손익비"

    def test_hyphenated_key(self):
        meta, _ = parse_frontmatter("---\nmy-key: v\n---\nx")
        assert meta["my-key"] == "v"


# ---------------------------------------------------------------- load_skill / Skill
class TestLoadSkill:
    def test_full_frontmatter(self, root: Path):
        p = make_skill(
            root, "trend", "# 추세\n규칙 본문",
            name="trend", description="추세 추종", roles=["analyst", "head"],
            version="1.2", source="https://ex.com", license="MIT",
        )
        s = load_skill(p)
        assert isinstance(s, Skill)
        assert s.name == "trend"
        assert s.description == "추세 추종"
        assert s.roles == ["analyst", "head"]
        assert s.version == "1.2"
        assert s.source == "https://ex.com"
        assert s.license == "MIT"
        assert s.body == "# 추세\n규칙 본문"
        assert s.path == p
        assert s.approx_tokens == len(s.body) // 4

    def test_defaults_when_fields_missing(self, root: Path):
        p = make_skill(root, "bare", "본문", frontmatter="---\n---")
        s = load_skill(p)
        assert s.name == "bare"  # 디렉터리 이름
        assert s.description == ""
        assert s.roles == []
        assert s.version == "0"
        assert s.source is None
        assert s.license is None

    def test_no_frontmatter_at_all(self, root: Path):
        p = make_skill(root, "plain", "그냥 본문", frontmatter="")
        s = load_skill(p)
        assert s.name == "plain"
        assert s.body == "그냥 본문"

    def test_roles_from_comma_string(self, root: Path):
        p = make_skill(root, "a", "b", frontmatter="---\nroles: analyst, head\n---")
        assert load_skill(p).roles == ["analyst", "head"]

    def test_roles_dedupe_and_strip(self, root: Path):
        p = make_skill(root, "a", "b", frontmatter="---\nroles: [ analyst, analyst ,head ]\n---")
        assert load_skill(p).roles == ["analyst", "head"]

    def test_roles_yaml_block(self, root: Path):
        p = make_skill(root, "a", "b", frontmatter="---\nname: a\nroles:\n  - analyst\n  - risk\n---")
        assert load_skill(p).roles == ["analyst", "risk"]

    def test_body_stripped(self, root: Path):
        p = make_skill(root, "a", "\n\n본문\n\n", frontmatter="---\nname: a\n---")
        assert load_skill(p).body == "본문"

    def test_empty_body_raises(self, root: Path):
        p = make_skill(root, "a", "   \n", frontmatter="---\nname: a\n---")
        with pytest.raises(SkillError):
            load_skill(p)

    def test_unterminated_raises(self, root: Path):
        p = make_skill(root, "a", "body", frontmatter="---\nname: a")
        with pytest.raises(SkillError):
            load_skill(p)

    def test_missing_file_raises_skill_error(self, tmp_path: Path):
        with pytest.raises(SkillError):
            load_skill(tmp_path / "nope" / "SKILL.md")

    def test_invalid_utf8_raises_skill_error(self, root: Path):
        d = root / "bin"
        d.mkdir()
        (d / "SKILL.md").write_bytes(b"---\nname: bin\n---\n\xff\xfe body")
        with pytest.raises(SkillError):
            load_skill(d / "SKILL.md")

    def test_approx_tokens_documented_formula(self, root: Path):
        body = "x" * 403
        p = make_skill(root, "a", body, frontmatter="---\nname: a\n---")
        assert load_skill(p).approx_tokens == 403 // 4 == 100

    def test_version_number_as_string(self, root: Path):
        p = make_skill(root, "a", "b", frontmatter="---\nversion: 3\n---")
        assert load_skill(p).version == "3"

    def test_skill_model_forbids_extra(self):
        with pytest.raises(Exception):
            Skill(name="a", body="b", path=Path("x"), approx_tokens=0, bogus=1)

    def test_applies_to_and_wildcard(self):
        s = Skill(name="a", body="b", path=Path("x"), approx_tokens=0, roles=["*"])
        assert s.applies_to("anything")
        t = Skill(name="a", body="b", path=Path("x"), approx_tokens=0, roles=["head"])
        assert t.applies_to("head") and not t.applies_to("analyst")

    def test_render_section_format(self):
        s = Skill(name="abc", body="line1\nline2", path=Path("x"), approx_tokens=0)
        assert s.render_section() == "## Skill: abc\nline1\nline2"


# ---------------------------------------------------------------- SkillRegistry
class TestRegistryLoad:
    def test_loads_all_sorted(self, root: Path):
        make_skill(root, "zeta", name="zeta", roles=["head"])
        make_skill(root, "alpha", name="alpha", roles=["head"])
        make_skill(root, "mid", name="mid", roles=["analyst"])
        reg = SkillRegistry(root)
        assert len(reg) == 3
        assert reg.names == ["alpha", "mid", "zeta"]
        assert [s.name for s in reg.all()] == ["alpha", "mid", "zeta"]
        assert "alpha" in reg and "nope" not in reg
        assert reg.get("mid").roles == ["analyst"]
        assert reg.get("nope") is None

    def test_missing_root_is_empty_with_warning(self, tmp_path: Path, caplog):
        with caplog.at_level(logging.WARNING, logger="tradingbot.skills"):
            reg = SkillRegistry(tmp_path / "absent")
        assert len(reg) == 0
        assert reg.for_role("head") == []
        assert reg.render("head") == ""
        assert any("스킬 디렉터리" in r.getMessage() for r in caplog.records)

    def test_empty_root(self, root: Path):
        reg = SkillRegistry(root)
        assert len(reg) == 0

    def test_ignores_files_not_named_skill_md(self, root: Path):
        make_skill(root, "ok", name="ok")
        (root / "stray.md").write_text("---\nname: stray\n---\nx", encoding="utf-8")
        (root / "ok" / "README.md").write_text("설명", encoding="utf-8")
        (root / "other").mkdir()
        (root / "other" / "skill.md").write_text("---\nname: lower\n---\nx", encoding="utf-8")  # 소문자
        reg = SkillRegistry(root)
        assert reg.names == ["ok"]

    def test_ignores_nested_deeper_dirs(self, root: Path):
        make_skill(root, "top", name="top")
        make_skill(root / "top" / "sub", "deep", name="deep")
        reg = SkillRegistry(root)
        assert reg.names == ["top"]

    def test_broken_skill_skipped_with_error_log(self, root: Path, caplog):
        make_skill(root, "good", name="good", roles=["head"])
        bad = make_skill(root, "bad", "body", frontmatter="---\nname: bad\nno closing")
        with caplog.at_level(logging.ERROR, logger="tradingbot.skills"):
            reg = SkillRegistry(root)
        assert reg.names == ["good"]
        assert str(bad) in reg.errors
        assert any("스킬 로드 실패" in r.getMessage() for r in caplog.records)

    def test_empty_body_skill_skipped(self, root: Path):
        make_skill(root, "good", name="good")
        make_skill(root, "empty", "", frontmatter="---\nname: empty\n---")
        reg = SkillRegistry(root)
        assert reg.names == ["good"]

    def test_duplicate_name_keeps_first_by_path(self, root: Path, caplog):
        make_skill(root, "a_dir", "첫째", name="dup")
        make_skill(root, "b_dir", "둘째", name="dup")
        with caplog.at_level(logging.WARNING, logger="tradingbot.skills"):
            reg = SkillRegistry(root)
        assert reg.names == ["dup"]
        assert reg.get("dup").body == "첫째"
        assert any("이름 중복" in r.getMessage() for r in caplog.records)

    def test_name_mismatch_warns_but_loads(self, root: Path, caplog):
        make_skill(root, "dirname", name="other")
        with caplog.at_level(logging.WARNING, logger="tradingbot.skills"):
            reg = SkillRegistry(root)
        assert reg.names == ["other"]
        assert any("디렉터리 이름" in r.getMessage() for r in caplog.records)

    def test_reload_picks_up_changes(self, root: Path):
        make_skill(root, "a", name="a")
        reg = SkillRegistry(root)
        assert reg.names == ["a"]
        make_skill(root, "b", name="b")
        reg.load()
        assert reg.names == ["a", "b"]

    def test_root_stored_as_path(self, root: Path):
        reg = SkillRegistry(str(root))  # type: ignore[arg-type]
        assert reg.root == root

    def test_repr_has_no_bodies(self, root: Path):
        make_skill(root, "a", "SECRET-BODY-TEXT", name="a")
        r = repr(SkillRegistry(root))
        assert "a" in r and "SECRET-BODY-TEXT" not in r

    def test_uses_pathlib_glob_on_relative_root(self, root: Path, monkeypatch):
        make_skill(root, "a", name="a")
        monkeypatch.chdir(root.parent)
        reg = SkillRegistry(Path("skills"))
        assert reg.names == ["a"]


class TestForRole:
    def test_filters_and_sorts(self, root: Path):
        make_skill(root, "z", name="z", roles=["analyst", "head"])
        make_skill(root, "b", name="b", roles=["head"])
        make_skill(root, "a", name="a", roles=["analyst"])
        make_skill(root, "n", name="n", roles=[])
        reg = SkillRegistry(root)
        assert [s.name for s in reg.for_role("head")] == ["b", "z"]
        assert [s.name for s in reg.for_role("analyst")] == ["a", "z"]
        assert reg.for_role("risk") == []

    def test_role_match_is_exact(self, root: Path):
        make_skill(root, "a", name="a", roles=["head"])
        reg = SkillRegistry(root)
        assert reg.for_role("Head") == []
        assert reg.for_role("hea") == []

    def test_wildcard_role(self, root: Path):
        make_skill(root, "all", name="all", roles=["*"])
        make_skill(root, "one", name="one", roles=["head"])
        reg = SkillRegistry(root)
        assert [s.name for s in reg.for_role("whatever")] == ["all"]
        assert [s.name for s in reg.for_role("head")] == ["all", "one"]

    def test_returns_new_list_each_time(self, root: Path):
        make_skill(root, "a", name="a", roles=["head"])
        reg = SkillRegistry(root)
        l1 = reg.for_role("head")
        l1.clear()
        assert len(reg.for_role("head")) == 1


class TestRender:
    def test_format_and_order(self, root: Path):
        make_skill(root, "b", "B 본문", name="b", roles=["head"])
        make_skill(root, "a", "A 본문\n둘째", name="a", roles=["head"])
        reg = SkillRegistry(root)
        out = reg.render("head")
        assert out == "## Skill: a\nA 본문\n둘째\n\n## Skill: b\nB 본문"

    def test_empty_role_renders_empty_string(self, root: Path):
        make_skill(root, "a", name="a", roles=["head"])
        assert SkillRegistry(root).render("none") == ""

    def test_deterministic_across_instances(self, root: Path):
        for i in range(5):
            make_skill(root, f"s{i}", f"본문 {i} " * 30, name=f"s{i}", roles=["head"])
        a = SkillRegistry(root).render("head")
        b = SkillRegistry(root).render("head")
        c = SkillRegistry(root).render("head")
        assert a.encode("utf-8") == b.encode("utf-8") == c.encode("utf-8")

    def test_deterministic_regardless_of_creation_order(self, root: Path):
        make_skill(root, "m", "M", name="m", roles=["head"])
        make_skill(root, "a", "A", name="a", roles=["head"])
        make_skill(root, "z", "Z", name="z", roles=["head"])
        out = SkillRegistry(root).render("head")
        assert out.index("## Skill: a") < out.index("## Skill: m") < out.index("## Skill: z")

    def test_drops_from_end_when_over_budget(self, root: Path, caplog):
        make_skill(root, "a", "x" * 400, name="a", roles=["head"])  # ~100 tokens + 헤더
        make_skill(root, "b", "y" * 400, name="b", roles=["head"])
        make_skill(root, "c", "z" * 400, name="c", roles=["head"])
        reg = SkillRegistry(root)
        with caplog.at_level(logging.WARNING, logger="tradingbot.skills"):
            out = reg.render("head", max_tokens=215)
        assert "## Skill: a" in out and "## Skill: b" in out and "## Skill: c" not in out
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert msgs and "c" in msgs[-1] and "head" in msgs[-1]
        assert len(out) // 4 <= 215

    def test_drops_multiple_names_in_warning(self, root: Path, caplog):
        for n in ("a", "b", "c", "d"):
            make_skill(root, n, "x" * 400, name=n, roles=["head"])
        reg = SkillRegistry(root)
        with caplog.at_level(logging.WARNING, logger="tradingbot.skills"):
            out = reg.render("head", max_tokens=110)
        assert out.startswith("## Skill: a") and "## Skill: b" not in out
        msg = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING][-1]
        assert "b, c, d" in msg

    def test_budget_zero_drops_everything(self, root: Path, caplog):
        make_skill(root, "a", "x" * 40, name="a", roles=["head"])
        reg = SkillRegistry(root)
        with caplog.at_level(logging.WARNING, logger="tradingbot.skills"):
            assert reg.render("head", max_tokens=0) == ""
        assert any("a" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    def test_no_warning_when_within_budget(self, root: Path, caplog):
        make_skill(root, "a", "x" * 40, name="a", roles=["head"])
        reg = SkillRegistry(root)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="tradingbot.skills"):
            out = reg.render("head", max_tokens=6000)
        assert "## Skill: a" in out
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_drop_is_whole_skill_not_truncation(self, root: Path):
        make_skill(root, "a", "A" * 400, name="a", roles=["head"])
        make_skill(root, "b", "B" * 400, name="b", roles=["head"])
        out = SkillRegistry(root).render("head", max_tokens=150)
        assert out == "## Skill: a\n" + "A" * 400
        assert "B" not in out

    def test_default_budget_is_6000(self, root: Path):
        make_skill(root, "big", "x" * (6000 * 4 - 100), name="big", roles=["head"])
        make_skill(root, "small", "y" * 400, name="small", roles=["head"])
        out = SkillRegistry(root).render("head")
        assert "## Skill: big" in out and "## Skill: small" not in out

    def test_negative_budget_raises(self, root: Path):
        with pytest.raises(ValueError):
            SkillRegistry(root).render("head", max_tokens=-1)

    def test_render_does_not_mutate_registry(self, root: Path):
        for n in ("a", "b"):
            make_skill(root, n, "x" * 400, name=n, roles=["head"])
        reg = SkillRegistry(root)
        reg.render("head", max_tokens=110)
        assert reg.names == ["a", "b"]
        assert len(reg.for_role("head")) == 2

    def test_render_korean_bytes_stable(self, root: Path):
        make_skill(root, "k", "한국어 본문 — 결정성 확인 ✓", name="k", roles=["head"])
        reg = SkillRegistry(root)
        assert reg.render("head") == "## Skill: k\n한국어 본문 — 결정성 확인 ✓"


class TestRealisticSkillFile:
    def test_vibe_trading_style_frontmatter(self, root: Path):
        text = (
            "---\n"
            "name: support-resistance\n"
            "description: >-\n"
            "  지지/저항 레벨 식별\n"
            "roles:\n"
            "  - analyst\n"
            "  - head\n"
            'version: "0.3"\n'
            "source: https://github.com/example/vibe-trading\n"
            "license: MIT\n"
            "---\n"
            "\n"
            "# Support / Resistance\n"
            "\n"
            "1. 최근 20봉 고가/저가를 본다.\n"
        )
        p = make_skill(root, "support-resistance", "", frontmatter=text.rstrip("\n"))
        # make_skill은 frontmatter + "\n" + body + "\n" 로 쓰므로 위 text 그대로 들어간다
        s = load_skill(p)
        assert s.name == "support-resistance"
        assert s.roles == ["analyst", "head"]
        assert s.version == "0.3"
        assert s.license == "MIT"
        assert s.body.startswith("# Support / Resistance")
        # ">-" 폴드 스칼라: 들여쓴 이어쓰기 줄을 접어 넣는다
        assert s.description == "지지/저항 레벨 식별"

    def test_folded_scalar_multiple_lines(self):
        meta, _ = parse_frontmatter("---\ndescription: >-\n  첫 줄\n  둘째 줄\nname: a\n---\nx")
        assert meta["description"] == "첫 줄 둘째 줄"
        assert meta["name"] == "a"

    def test_plain_scalar_continuation(self):
        meta, _ = parse_frontmatter("---\ndescription: 앞\n  뒤\n---\nx")
        assert meta["description"] == "앞 뒤"

    def test_indented_line_without_key_raises(self):
        with pytest.raises(SkillError):
            parse_frontmatter("---\n  stray\nname: a\n---\nx")
