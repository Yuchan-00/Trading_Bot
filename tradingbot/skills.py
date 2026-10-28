"""스킬 로더 (SPEC §9).

``skills/<name>/SKILL.md`` 파일을 읽어 역할(role)별로 시스템 프롬프트에 붙일 지식 조각을 제공한다.
스킬 배정(어떤 역할이 어떤 스킬을 쓰는지)은 v2에서 확정하며, 여기서는 **로더**만 구현한다.

파일 형식::

    ---
    name: trend-following
    description: 추세 추종 진입 규칙
    roles: [analyst, head]        # 또는 YAML 스타일 "- analyst" 목록
    version: "1.0"
    source: https://example.com   # 선택
    license: MIT                  # 선택
    ---
    (본문: 마크다운)

규칙
- frontmatter는 파일의 **첫 두 ``---`` 줄 사이**이며, 파서는 표준 라이브러리만 쓴다(PyYAML 없음).
  지원 문법: ``key: value``, ``key: [a, b]``, ``key:`` 뒤의 ``  - a`` 목록, ``#`` 주석, 따옴표 벗기기.
- ``approx_tokens = len(body) // 4`` — 영문 기준 대략 4자/토큰 근사값. 정확한 토크나이저가 아니며
  예산(``max_tokens``) 초과 여부를 **보수적으로 빠르게** 판단하는 용도다.
- ``render()``는 바이트 단위로 결정적이다(이름순 정렬, 같은 입력 → 같은 출력). 합계가
  ``max_tokens``를 넘으면 **뒤쪽 스킬부터 통째로** 제외하고, 제외한 스킬 이름을 WARNING으로 남긴다.
- 깨진 스킬 파일 하나가 전체 로드를 막지 않는다(ERROR 로그 후 건너뜀). 비밀값은 다루지 않는다.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("tradingbot.skills")

SKILL_FILENAME = "SKILL.md"
FRONTMATTER_DELIM = "---"
ROLE_WILDCARD = "*"  # roles: ["*"] 이면 모든 역할에 포함
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(?:\s+(.*))?$")
_LIST_ITEM_RE = re.compile(r"^\s+-(?:\s+(.*))?$")
_NULL_TOKENS = {"null", "~", "none"}
_BLOCK_MARKERS = {"", ">", ">-", ">+", "|", "|-", "|+"}  # YAML 블록 스칼라 표시(최소 지원)


class SkillError(ValueError):
    """SKILL.md 파싱 실패."""


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    roles: list[str] = Field(default_factory=list)
    version: str = "0"
    source: str | None = None
    license: str | None = None
    body: str
    path: Path
    approx_tokens: int

    def applies_to(self, role: str) -> bool:
        """``roles``에 ``role``(또는 와일드카드 ``*``)이 들어 있으면 True."""
        return role in self.roles or ROLE_WILDCARD in self.roles

    def render_section(self) -> str:
        """프롬프트에 들어갈 한 섹션. ``render()``가 이것들을 이어 붙인다."""
        return f"## Skill: {self.name}\n{self.body}"


# ---------------------------------------------------------------- frontmatter
def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _strip_comment(line: str) -> str:
    """따옴표 밖의 ``#`` 이후를 제거한다 (``url#frag`` 같은 공백 없는 ``#``은 유지)."""
    out: list[str] = []
    quote: str | None = None
    prev = " "
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and prev.isspace():
            break
        else:
            out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def _parse_scalar(raw: str) -> str | list[str] | None:
    v = raw.strip()
    if v == "":
        return ""
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_unquote(p) for p in inner.split(",") if _unquote(p) != ""]
    if v.lower() in _NULL_TOKENS:
        return None
    return _unquote(v)


def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str] | None], str]:
    """``---`` 두 줄 사이의 frontmatter를 dict로, 나머지를 본문으로 돌려준다.

    frontmatter가 없으면 ``({}, text)``. 닫는 ``---``가 없거나 문법이 깨지면 ``SkillError``.
    값은 문자열(또는 문자열 목록, null)이며 숫자/불리언 변환은 하지 않는다.
    """
    lines = text.lstrip("﻿").split("\n")
    # 첫 줄 앞의 빈 줄은 허용
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines) or lines[start].strip() != FRONTMATTER_DELIM:
        return {}, text
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            end = i
            break
    if end is None:
        raise SkillError("frontmatter를 닫는 '---' 줄이 없음")

    meta: dict[str, str | list[str] | None] = {}
    current_list_key: str | None = None
    last_key: str | None = None
    for lineno, raw in enumerate(lines[start + 1 : end], start=start + 2):
        line = _strip_comment(raw.rstrip("\r"))
        if line.strip() == "":
            continue
        item = _LIST_ITEM_RE.match(line)
        if item and current_list_key is not None:
            value = _unquote(item.group(1) or "")
            if value != "":
                meta[current_list_key].append(value)  # type: ignore[union-attr]
            continue
        if line[0].isspace():
            # 들여쓴 이어쓰기 줄: 바로 앞 스칼라에 접어 넣는다 (YAML ">-" / "|" 블록의 최소 지원)
            prev = meta.get(last_key) if last_key is not None else None
            if last_key is None or not isinstance(prev, str):
                raise SkillError(f"{lineno}행: frontmatter 문법 오류: {raw.strip()!r}")
            cont = _unquote(line)
            meta[last_key] = cont if prev in _BLOCK_MARKERS else f"{prev} {cont}".strip()
            continue
        m = _KEY_RE.match(line)
        if not m:
            raise SkillError(f"{lineno}행: frontmatter 문법 오류: {raw.strip()!r}")
        key, rest = m.group(1), m.group(2)
        if key in meta:
            raise SkillError(f"{lineno}행: 키 중복: {key!r}")
        last_key = key
        if rest is None or rest.strip() == "":
            meta[key] = []  # 뒤따르는 "- 항목"이 없으면 빈 목록(=빈 값)
            current_list_key = key
            continue
        current_list_key = None
        meta[key] = _parse_scalar(rest)
    body = "\n".join(lines[end + 1 :])
    return meta, body


def _as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    else:
        parts = [str(p).strip() for p in value]
    out: list[str] = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return out


def _as_str(value: str | list[str] | None, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        return ", ".join(value) if value else default
    return value


def _as_opt_str(value: str | list[str] | None) -> str | None:
    s = _as_str(value, "")
    return s if s != "" else None


def load_skill(path: Path) -> Skill:
    """``SKILL.md`` 한 개를 읽어 ``Skill``로 만든다. 실패 시 ``SkillError``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SkillError(f"읽기 실패: {e.__class__.__name__}") from e
    meta, body = parse_frontmatter(text)
    body = body.strip()
    if not body:
        raise SkillError("본문이 비어 있음")
    name = _as_str(meta.get("name"), "").strip() or path.parent.name
    if not name:
        raise SkillError("name이 비어 있음")
    return Skill(
        name=name,
        description=_as_str(meta.get("description"), "").strip(),
        roles=_as_list(meta.get("roles")),
        version=_as_str(meta.get("version"), "0").strip() or "0",
        source=_as_opt_str(meta.get("source")),
        license=_as_opt_str(meta.get("license")),
        body=body,
        path=path,
        approx_tokens=len(body) // 4,
    )


# ---------------------------------------------------------------- registry
class SkillRegistry:
    """``root/<name>/SKILL.md`` 전부를 메모리에 올린다. 같은 입력이면 같은 순서·같은 결과."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._skills: dict[str, Skill] = {}
        self.errors: dict[str, str] = {}  # path -> 사유 (대시보드/디버그용)
        self.load()

    def load(self) -> None:
        """디스크에서 다시 읽는다. 깨진 파일은 ERROR 로그 후 건너뛴다."""
        self._skills = {}
        self.errors = {}
        if not self.root.is_dir():
            log.warning("스킬 디렉터리가 없음: %s (스킬 없이 진행)", self.root)
            return
        files = sorted(p for p in self.root.glob(f"*/{SKILL_FILENAME}") if p.is_file())
        for path in files:
            try:
                skill = load_skill(path)
            except SkillError as e:
                self.errors[str(path)] = str(e)
                log.error("스킬 로드 실패 %s: %s", path, e)
                continue
            if skill.name in self._skills:
                prev = self._skills[skill.name]
                log.warning("스킬 이름 중복 %r: %s 는 무시 (%s 유지)", skill.name, path, prev.path)
                self.errors[str(path)] = f"이름 중복: {skill.name}"
                continue
            if skill.name != path.parent.name:
                log.warning("스킬 이름 %r 이 디렉터리 이름 %r 과 다름", skill.name, path.parent.name)
            self._skills[skill.name] = skill
        log.info("스킬 %d개 로드 (%s)", len(self._skills), self.root)

    # -- 조회 ---------------------------------------------------------------
    def all(self) -> list[Skill]:
        return [self._skills[n] for n in sorted(self._skills)]

    @property
    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        return name in self._skills

    def for_role(self, role: str) -> list[Skill]:
        """``roles``에 ``role``이 포함된 스킬, 이름순."""
        return [s for s in self.all() if s.applies_to(role)]

    def render(self, role: str, max_tokens: int = 6000) -> str:
        """역할용 스킬 섹션을 ``"## Skill: name\\n<body>"`` 형태로 이어 붙인다.

        토큰 합계(``approx_tokens`` + 헤더 근사)가 ``max_tokens``를 넘으면 뒤(이름 역순) 스킬부터
        통째로 뺀다. 결정적: 같은 레지스트리·같은 인자면 바이트 단위로 같은 문자열.
        """
        if max_tokens < 0:
            raise ValueError("max_tokens는 0 이상이어야 함")
        selected = self.for_role(role)
        if not selected:
            return ""
        sections = [s.render_section() for s in selected]
        costs = [len(sec) // 4 for sec in sections]
        kept = len(selected)
        while kept > 0 and sum(costs[:kept]) > max_tokens:
            kept -= 1
        dropped = [s.name for s in selected[kept:]]
        if dropped:
            log.warning(
                "역할 %r 스킬 토큰 예산 초과(%d > %d): 제외 %s",
                role,
                sum(costs),
                max_tokens,
                ", ".join(dropped),
            )
        return "\n\n".join(sections[:kept])

    def __repr__(self) -> str:
        return f"SkillRegistry(root={str(self.root)!r}, skills={self.names})"
