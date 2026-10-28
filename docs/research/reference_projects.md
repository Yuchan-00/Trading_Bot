# 참조 프로젝트 조사: FinceptTerminal, Vibe-Trading

조사일: 2026-10-28. 목적: 기존 Binance 멀티에이전트 봇(`B:\TradingBot`, `claude -p` 서브프로세스 에이전트 + `risk.py` 하드 리밋 + paper 기본)에 "터미널"과 "트레이딩 스킬" 기능을 추가할 때 무엇을, 어떤 라이선스 조건으로 가져올 수 있는지 정리.

## 1. FinceptTerminal (Fincept-Corporation/FinceptTerminal)

- 저장소: https://github.com/Fincept-Corporation/FinceptTerminal
- 라이선스: **AGPL-3.0-or-later** (강한 카피레프트). README: "Fincept no longer sells a separate commercial or academic licence for this repository." 상표는 파생물·리브랜딩에 서면 허가 필요.
  - 결론: **코드·문구 복사 금지. 기능 구성과 아키텍처 아이디어만 참고.** (기존 세션의 `docs/SKILL_REVIEW.md` 원칙과 동일: 문구 복사 없음, 원본은 `vendor/`에 LICENSE와 함께만 보관)
- 상태: 2026-06 공지로 공개판은 월 1회 업데이트. 유료 Enterprise 판과 Quantcept(별도 유료 리서치)로 집중. 최신 공개 릴리스 v4.5.1.
- 스택: C++20 · Qt6 · 내장 Python 3.11 (UV 부트스트랩) · 단일 바이너리. `PythonRunner`가 QProcess로 Python 스크립트를 실행하고 JSON(stdout)으로 결과를 받음(동시 3개, 콜드스타트 0.5~1.5s, API 키 22개를 SecureStorage에서 호출마다 주입, 그 외 자격증명형 env는 제거).

### 1.1 기능 목록 (README 기준, 자체 마케팅 수치)
| 영역 | 내용 |
|---|---|
| Analytics | DCF, 포트폴리오 최적화, VaR/Sharpe, 파생상품 가격결정, 채권, 대체투자, "18-module QuantLib suite" |
| AI agents | "37 trader/investor, economic and geopolitics agents". BYOK: OpenAI, Anthropic, Gemini, Groq, DeepSeek, OpenRouter, Ollama |
| Data connectors | "100+": FRED, IMF, World Bank, DBnomics, AkShare, Polygon, Kraken, Yahoo Finance, 정부 API |
| Trading | 암호화폐·주식 피드, paper-trading engine, 16 broker integrations |
| Automation | 비주얼 노드 에디터(DAG 실행, 스케줄 플로우), MCP tools, AI Quant Lab(ML, 팩터 발굴, RL) |
| Intelligence | 해상(AIS) 추적, 지정학 분석, 관계 맵 |
| UI | Equity Research, Portfolio, News, Node Editor 탭 |

### 1.2 아키텍처 (docs/ARCHITECTURE.md v5.0.0-draft, 2026-05-15)
- "modular monolith by intent": 한 바이너리, 13개 bounded context — Markets, News, Economics, Geopolitics, Trading, Portfolio, Crypto, Derivatives, Predictions, Agents, AI Chat, Workflow, Identity.
- 계층: Presentation(screens, dashboard widgets, DockManager) → Application(bounded contexts) → Data Plane(**DataHub** pub/sub, CacheManager) → Integration Adapters(Broker Adapter, MCP Tools, Python Runner, HTTP Client, WS Feed) → Infrastructure(Logger, AppConfig, EventBus, SessionManager, AuthManager, Database, SecureStorage) → Platform.
- 의존성은 아래 방향만. 컨텍스트 간 호출은 DataHub 토픽(`domain:subdomain:id[:modifier]`)이나 typed event로만. 프로듀서 실패 시 last-known-good 반환. TopicPolicy(TTL, 최소 간격, push-only).
- 에이전트: 종류 "hedge fund, geopolitics, economics, trader, finagent". `AgentService::start_task` → PythonRunner가 `scripts/agents/finagent_core/main.py` 실행 → 스텝별 JSON 라인 → SQLite `agent_tasks`에 저장 + DataHub `agent:<kind>:run:<id>` 토픽 발행 → 화면이 구독. SQL에 상태가 있어 크래시 후 재개 가능.
- 거래: `PaperTrading`, `OrderMatcher`(SL/TP 트리거), `UnifiedTrading`(실거래 라우팅). paper 모드면 `UnifiedTrading::place_order`가 PaperTrading으로 라우팅. 토픽 `paper:*`.
- MCP: `McpService`(통합 도구 표면) / `McpProvider`(내부 도구 레지스트리, JSON 스키마) / `McpManager`(외부 MCP 서버) / `ToolDispatcher`(다중 라운드) / 40+ tools.
- 데이터 소스(아키텍처 문서): FRED, DBnomics, HDX, ACLED, Polymarket, Kalshi, 옵션 체인, 온체인, Hyperliquid·Kraken WebSocket.

### 1.3 우리 봇에 가져올 "아이디어" 후보 (코드 아님)
1. **터미널(대시보드) 화면**: 포지션/자본/일일 손익/쿨다운/LLM 일시정지 상태, 최근 결정 로그, 사이클 보고서, 백테스트 결과를 한 화면에. 기존 `state.json`, `logs/decisions.jsonl`, `logs/cycles/`, `backtest_results/`가 이미 데이터 소스.
2. **DataHub식 토픽/캐시 계층**: 지표·뉴스·잔고를 "한 번 가져와 여러 소비자"로, 실패 시 last-known-good + 신선도(staleness) 표시. 기존 `quality.py`의 오래된 봉 검사와 결합.
3. **에이전트 실행 기록의 영속화**: 스텝별 JSON 라인을 SQLite/JSONL로 저장해 재시작 후 재개·감사(audit). 기존 `logs/cycles/`를 구조화.
4. **분석 모듈**: VaR/Sharpe/최대낙폭/포트폴리오 heat 리포트(이미 `risk.py`·`reports.py`에 일부), DCF 등 주식용 모듈은 암호화폐 봇엔 불필요.
5. **노드 에디터**는 범위 밖(UI 비용 대비 효용 낮음). 대신 "파이프라인 정의를 YAML로"가 현실적.

## 2. Vibe-Trading (HKUDS/Vibe-Trading)

- 저장소: https://github.com/HKUDS/Vibe-Trading (홈 vibetrading.wiki). 약 36.9k stars, 3,465 commits.
- 라이선스: **MIT** (+ `NOTICE`). → 스킬·도구 설계·일부 코드 채택 가능. 저작권 고지 유지 필요. 한국어 README(`README_ko.md`) 있음.
- 스택: Python 3.11+, FastAPI 백엔드, React 19 프런트, PyPI `vibe-trading-ai`. 디렉터리: `agent/`, `tools/`, `frontend/`, `desktop/electron/`, `schema/tool_contracts/`, `scripts/`, `wiki/`, `pixel-office/`, `zimaos/`.
- 모델 제공자: `claude-cli`(= `claude -p` 서브프로세스, 우리 봇과 동일 방식), OpenRouter(기본), OpenCode, MiniMax, Moonshot, Codex, OpenAI(`LANGCHAIN_PROVIDER`).
- 채널: Telegram, Feishu, WeChat, WeCom, Slack, Discord, DingTalk, QQ, Email, WebSocket, Matrix, Signal.

### 2.1 핵심 구성 (README 앞부분 10만자 기준; 전체 41만자, 저장소 사본으로 보완 예정)
- **Agent loop**: 컨텍스트 압축(compaction), 도구 호출 중복 제거 원장(de-dup ledger), 메모리 recall/GC/indexing.
- **Swarm**: 다중 에이전트 실행, 워커별 `skills:` 허용목록을 `load_skill`이 강제. "investment-committee workers".
- **Skills**: SKILL.md(Agent Skills 표준) 기반, `load_skill` 도구로 로드. 집계 사이트 기준 약 79~90개.
- **Tools**(일부 이름): `get_market_data`, `backtest`, `paper_trade_step`, `technical_indicators`, `search_symbol`, `read_file`, `load_skill`, `refresh_strategy_evidence`. 도구 계약은 `schema/tool_contracts/`.
- **Alpha Zoo**: 478 팩터(v0.1.17). 각 알파는 `min_warmup_bars`와 입력 의존성을 선언, 레지스트리가 입력 누락 봉의 출력을 마스킹. 알려진 결함: 52개 알파가 워밍업 구간에서 값을 냄.
- **Quant Library**: 23 모듈 306 함수(2026-08-28): Heston 가격, HRP, 코퓰라, VPIN/Roll/Amihud/Kyle 미시구조 추정, 배리어 옵션 Greeks, Key Rate Duration 등.
- **데이터 소스**: yfinance, Tencent, Eastmoney, AKShare, BaoStock, mootdx, Sina, Stooq, Tushare, FMP, Tiingo, **CCXT**, Gildata, QVeris, Longbridge.
- **브로커(18)**: Robinhood(읽기), IBKR, Futu, Longbridge, KIS, Upbit, Toss Securities, Scalable Capital, Zerodha, Alpaca, Dhan, Shoonya, MT5, eToro, **Binance**, Trading 212, Nobitex, Wallex, extraETF(CSV).
- **Paper trading**: `paper_trade_step` + 저장된 paper 계정이 "완료된 일봉에 대해 주문/전략을 체결, 브로커에 닿지 않음, 계정은 앞으로만 진행".
- **Live gates**: 오류 시 fail-closed, 킬스위치 스윕, 중단(halt) 처리, cancel/flatten.
- **평가**: 오프라인 evals 하네스는 계측 누락 시 `NOT_EVALUABLE`을 냄(통과로 처리하지 않음). 자체 보고 수치는 미검증.
- **보안 공지**: X `VibeTrading_HKU`, Virtuals 프로젝트, 토큰 컨트랙트는 비공식. "We have never launched or endorsed any token or memecoin."

### 2.2 우리 봇에 가져올 후보
1. **스킬 레이어**: `prompts/<agent>.md`에 더해 `skills/<name>/SKILL.md`를 두고, 에이전트별 허용목록(`skills:`)만 로드. 기존 7개 프롬프트를 스킬로 재구성 가능. 평가 하네스의 `NOT_EVALUABLE` 원칙 채택.
2. **도구 계약(JSON schema)**: `claude -p --json-schema`로 이미 구조화 출력을 쓰므로, 입력 측 도구(지표, 시장 데이터, 백테스트, paper step)도 계약 파일로 고정.
3. **Alpha/팩터 라이브러리의 워밍업·입력 의존성 선언** 패턴 → `indicators.py`에 `min_warmup_bars`와 입력 마스킹 적용(선행편향·부족 데이터 방지. 기존 `quality.py`와 결합).
4. **paper_trade_step**: 사이클과 분리된 "한 스텝 전진" API → 터미널에서 재생(replay)·리플레이 검증에 사용.
5. **de-dup ledger / compaction**: 에이전트 호출 비용 절감(기존 봇은 사이클당 12회 호출).

## 3. 라이선스 요약
| 프로젝트 | 라이선스 | 코드 채택 | 아이디어 채택 | 조건 |
|---|---|---|---|---|
| FinceptTerminal | AGPL-3.0-or-later | ✗ | ✓ | 상표 사용 금지, 문구 복사 금지 |
| Vibe-Trading | MIT | ✓ | ✓ | 저작권 고지·라이선스 사본 유지(`vendor/` 또는 `THIRD_PARTY_NOTICES.md`) |

## 출처
- https://github.com/Fincept-Corporation/FinceptTerminal (README, 2026-10-28 조회)
- https://raw.githubusercontent.com/Fincept-Corporation/FinceptTerminal/main/docs/ARCHITECTURE.md
- https://github.com/HKUDS/Vibe-Trading (README 앞부분, 저장소 메타데이터)
- 검색: https://themenonlab.blog/blog/bloomberg-fincept-terminal-open-source-alternative , https://pyshine.com/FinceptTerminal-Open-Source-Financial-Intelligence-Platform/ , https://www.vibeindex.ai/skills/mphinance/alpha-skills/technical-analyst , https://vibeindex.ai/skills/tradermonty/claude-trading-skills/trade-performance-coach (3자 집계 사이트, 미검증)
