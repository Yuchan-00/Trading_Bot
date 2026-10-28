# DESIGN — Claude API 멀티에이전트 Binance 현물 봇

작성 2026-10-28. 이전 세션 설계(코드 강제 하드 리밋, paper 기본, 7 에이전트)를 계승하되, LLM 호출을 `claude -p` 서브프로세스에서 **Claude API(Anthropic SDK)** 로 바꾸고, Vibe-Trading(MIT) 스킬을 역할별로 배정하며, 로컬 웹 대시보드와 텔레그램 거래 알림을 추가한다. 모듈 계약은 `SPEC.md`, 근거 자료는 `docs/research/`.

## 1. 원칙

1. **돈은 코드가 지킨다.** 한도(`config.HardLimits`)는 상수이고 LLM·설정·대시보드 어디서도 풀 수 없다. LLM 출력은 항상 `risk.py`/`gates.py`가 **조이기만** 한다(늘리지 못함).
2. **HOLD는 공짜다.** 코드 게이트(G0~G4)가 "결정할 가치가 있는 상황"을 걸러낸 뒤에만 LLM을 부른다. 아무것도 없으면 0회 호출로 사이클이 끝난다.
3. **증거 채널은 분리한다.** 기술분석·파생/자금흐름·뉴스 분석가는 서로의 출력을 보지 못한다. 강세/약세 변론가도 서로를 보지 못하고 절차가 다르다. 총괄은 불일치를 분류해 결정한다.
4. **숫자는 코드가 계산하고 LLM은 해석한다.** 지표·VaR·상관·유동성·사이즈는 코드가 만들고 LLM은 라벨된 JSON을 읽는다. 모든 근거는 입력 필드명을 인용해야 한다(숫자 날조 방지).
5. **실패는 기권(abstain)이다.** 타임아웃·스키마 오류·거부(refusal)는 "해당 채널 기권"으로 총괄에 전달되며 절대 강세 신호로 해석되지 않는다. 총괄이 실패하면 HOLD.
6. **모든 거래 이벤트는 텔레그램으로 간다.** 상태 저장 → 알림 순서. 알림 실패는 거래를 막지 않는다.
7. **paper 기본, live는 삼중 확인.** `MODE=live` + `CONFIRM_LIVE=YES` + 콘솔 "LIVE" 입력. 실자금 매매 실행은 사용자 책임이며 봇은 권하지 않는다.

## 2. 구성

```
스캐너(코드) ─ 후보·피처팩 ─▶ 분석가 3 (Haiku, 병렬·분리)
                                  technical (심볼별) / derivatives_flow (일괄) / news_sentiment (새 헤드라인 있을 때만)
                       G1 ─▶ 강세·약세 변론 (Sonnet, 후보별 병렬·독립)
                       G2 ─▶ 리스크 관리자 (Sonnet, 일괄, 거부권·사이즈 상한·손절/익절 제안·보유 포지션 건강도)
                       G3 ─▶ 총괄 (Opus, 일괄, 승인/조건부 후보와 플래그된 포지션만) ─▶ G4 ─▶ 주문 ─▶ 저장 ─▶ 텔레그램
                                                                      저널(Haiku, 청산 후·일일) / 검증기(코드, 오프라인)
```

| 역할 | 모델 | 호출 시점 | 배정 스킬(우리 식으로 다시 쓴 `skills/`) |
|---|---|---|---|
| scanner | 없음(코드) | 매 사이클 | ccxt, multi-factor(랭킹식), technical-basic/ichimoku/candlestick/smc/volatility(라벨 계산), perp-funding-basis/liquidation-heatmap(플로우 팩), correlation-regime/risk-analysis(리스크·레짐 팩), execution-model/market-microstructure(유동성 게이트), sentiment-analysis(종합 심리) |
| technical | claude-haiku-5-5 | 후보 ∪ 보유 종목별 | technical-basic, ichimoku, candlestick, smc, volatility, minute-analysis |
| derivatives_flow | claude-haiku-5-5 | 사이클당 1회 일괄 | perp-funding-basis, crypto-derivatives(펀딩 표만), liquidation-heatmap, investor-lenses(펜듈럼), market-microstructure |
| news_sentiment | claude-haiku-5-5 | 새 헤드라인 있을 때 1회 | event-driven, sentiment-analysis, research-discipline |
| bull | claude-sonnet-5-5 | G1 통과 후보별 | behavioral-finance, research-discipline, event-driven |
| bear | claude-sonnet-5-5 | G1 통과 후보별(bull과 병렬) | investor-lenses(역산), behavioral-finance, research-discipline, correlation-regime, perp-funding-basis |
| risk_manager | claude-sonnet-5-5 | 사이클당 1회 일괄(후보 또는 보유 있을 때) | risk-analysis, correlation-regime, correlation-analysis(롤링 상관만), asset-allocation(역변동성만), market-microstructure, execution-model, thesis-tracker, trade-journal, strategy-dev-manager(쇠퇴 상태), volatility, behavioral-finance |
| head | claude-opus-5-5 | 사이클당 1회 일괄(승인/조건부 후보 또는 플래그 포지션 있을 때) | investor-lenses(스태킹·불일치 분류), behavioral-finance, thesis-tracker, sentiment-analysis, event-driven, research-discipline |
| journal | claude-haiku-5-5 | 청산 직후 1회 + 매일 00:05 UTC | trade-journal, performance-attribution(라이트), report-generate, strategy-dev-manager |
| validator | 없음(코드, `backtest.py`) | 수동/야간 | backtest-diagnose, quant-statistics, factor-research(IC만) |

호출 수: 후보 2 + 보유 1 최악 11회(Haiku 5, Sonnet 5, Opus 1), 보통 7~8회, 셋업 없음 4~6회(Haiku 위주). 비용 대략치: 최악 $0.25/사이클, 보통 $0.08 → 하루 $6~10. **일일 상한(`LLM_DAILY_BUDGET_USD`, 기본 10) 초과 시 LLM 중단·HOLD**, 코드 손절/익절은 계속.

## 3. 코드 게이트 (LLM 0회)

- **G0 사전점검**: 킬스위치/STOP 파일, 낙폭 사다리(−3% 주의: 사이즈 50%, −4.5% 신규 금지, −6% STOP_ALL), 당일 −3%, 연속 3패, LLM 일시정지, 일일 예산, 데이터 품질(오래된 봉·NaN·이상치 → 종목 제외), 펀딩 정산 분(00/08/16 UTC ±1분 신규 주문 금지). 계정 단위 중단이면 보유 포지션의 코드 청산만 수행.
- **스캐너**: 유동성 게이트(24h 거래대금·스프레드·호가 깊이·15m 거래량), 보유/쿨다운(4)/복수매매 창(8)/급등(+15% 무거래량) 제외, 교차 z-점수 랭킹, 레짐 게이트(MACRO 알람 → 후보 없음, FUSED → 사이즈 50%·확신도 하한 0.75).
- **G1 후보 사전게이트**: technical.direction==long ∧ mtf_agreement ∧ confidence≥0.55 ∧ noise_check, 또는 derivatives washout_setup ∧ direction≠avoid. 탈락: block_new_longs, stop_inside_cascade, 연쇄청산 위험 high(2% 이내 군집), 뉴스 risk_event, 종합심리>80(평균회귀 셋업 제외), 포지션 3개, |상관|>0.8인 보유 종목.
- **G2 약세 킬**: bear.verdict==decline → 탈락(사유 기록, 저널이 반사실 추적).
- **G3 리스크 거부(코드가 다시 강제)**: veto 탈락, FUSED 사이즈 50%·하한 0.75, 쇠퇴/과매매·추격 심함 → 하한 0.75 또는 진입 중단, 주문액 > 24h 거래대금/96의 5% → 축소/차단, 스프레드·깊이 부족 차단, 예상 슬리피지+2×수수료 > 손절거리 25% 차단, CVaR 기반 사이즈 상한.
- **G4 최종**: 총괄 확신도 ≥ 하한(0.65/0.75), 사이즈 ≤ 리스크 상한 ≤ 10%, 손절 ≤ 2%, 익절 ≤ 4%, 순손익비 ≥ 1.5(손절만 조임), 최소 주문액. 위반은 HOLD로 강등 + 사유 텔레그램.

## 4. Claude API 사용 규칙 (`llm/anthropic_backend.py`)

- `client.messages.parse(... output_format=<pydantic>)` 구조화 출력. `tool_choice` 강제 없음, `thinking` 미지정(5.5 계열 기본 adaptive), `output_config.effort`로 깊이 제어(분석 low, 리서치 medium, 총괄 high).
- 시스템 프롬프트(역할 프롬프트 + 스킬 본문)는 **바이트 고정** + `cache_control {ephemeral, ttl 1h}`. 변동 데이터는 전부 user 메시지(JSON, `sort_keys`). 15분 주기라 1시간 TTL이 5분보다 싸다.
- 타임아웃: Haiku 25~30s, Sonnet 45~60s, Opus 90s. 재시도는 백엔드가 직접 최대 2회, 역할 타임아웃(과 사이클 기한) 안에서만 한다(SDK `max_retries=0`, 타임아웃 뒤 재시도 없음). 호출 전 예산 검사는 system 프롬프트를 캐시 쓰기 단가 × 토크나이저 여유 1.3 으로 잡는다. 실패·거부·max_tokens → 기권.
- 비용 원장(`cost_ledger.jsonl`): 호출마다 토큰·비용 기록, 호출 전 예산 검사. 인증 실패 → LLM 60분 일시정지.
- 비밀값은 SDK 클라이언트에만 전달. 프롬프트·로그·텔레그램·예외 메시지에 키가 들어가지 않는다(마스킹 테스트).
- 서버측 refusal fallback은 쓰지 않는다(거부는 HOLD로 충분하고 모델 티어를 고정하는 편이 비용·재현성에 낫다). 필요하면 설정으로 추가 가능.

## 5. 데이터와 피처팩 (`features.py`, `scanner.py`)

- 캔들 15m(300)/1h(200)/4h(200)/1d(120), 24h 티커, 호가 요약, 선물 공개 데이터(펀딩·OI·롱숏비; 실패 시 None), RSS 헤드라인, alternative.me 공포탐욕(선택, 캐시 1h).
- feature_pack(가격 구조 라벨: EMA 정렬·ADX·RSI14/5·볼린저 위치·OBV 추세·투표·MTF 일치, 일목(전환/기준 교차·구름 위치), 캔들 패턴 점수, SMC(BOS/ChoCH·FVG·근접 존), HV 백분위, ATR%, VWAP 거리, CGO_60, 비정상 거래량), flow_pack(펀딩 레짐·OI×펀딩 라벨·프리미엄·롱숏 백분위·괴리 플래그·추정 청산군집(추정 표시)·연쇄 위험·호가 불균형), risk_pack(VaR/CVaR 1h·실현변동성·첨도·BTC 상관/베타·보유종목 상관·역변동성/CVaR 사이즈·스프레드·깊이·충격 추정·손절 대 ATR 노이즈), regime_pack(평균 쌍상관·FUSED·BTC 4h 레짐·쇼크·NAME 알람), composite_sentiment(0~100).
- 추정값(청산군집 등)은 `estimated` 표시와 `data_confidence`를 달고, 낮은 신뢰도는 **차단에만** 쓰고 진입 근거로는 쓰지 않는다.

## 6. 포지션 논지 추적과 저널 (`journal.py`)

- 총괄의 BUY에는 반증 가능한 가정 2~4개와 **구조화된 레드라인**(지표·연산자·값)이 붙는다. 레드라인은 코드가 매 사이클 평가하고, 가정 상태(holding/weakening/impaired/broken)는 리스크 관리자가 평가한다. 건강도 ≤4가 2사이클 연속이거나 레드라인 적중이면 총괄에게 청산 결정을 묻는다. 코드 손절/익절/시간손절이 1차 청산이다.
- 편향 심각도(처분효과·과매매·추격·앵커링)와 전략 쇠퇴 상태는 코드가 계산하되, 청산 20건 미만이면 "표본 부족"으로 보고만 한다.
- 저널 역할(Haiku)은 청산마다 3~5줄 사후분석 + 교훈 태그, 매일 00:05 UTC 일일 리뷰를 만들어 `logs/journal.jsonl`과 텔레그램에 보낸다.

## 7. 텔레그램 이벤트 (`notify.py`)

진입 체결 / 청산 체결(모든 사유, 손익) / 부분체결·거부·거래소 오류 / 하드 게이트에 의한 강등(총괄 BUY→HOLD, 리스크 거부, 약세 킬) / 중단·해제(일일 한도·연속손실·낙폭 사다리·킬스위치·진입 일시중단) / 레짐 전환 / 뉴스 risk_event / 포지션 건강도 하락·레드라인 / 일일 리뷰 / 운영(시작·종료·크래시·LLM 일시정지·사이클 기한 초과·데이터 품질) / 하트비트(기본 4시간).

## 8. 대시보드 (`dashboard/`)

FastAPI + 단일 HTML(외부 CDN 없음), localhost 바인딩, 파일 읽기 전용. 상태 헤더, 포지션, 최근 결정(역할별 요약·게이트 사유), 거래 내역, 비용(오늘/이달/예산 막대), 백테스트 결과, 자본 스파크라인, STOP 버튼(토큰 필요 → `control/STOP` 파일 → 다음 사이클에 킬스위치).

## 9. 검증기 (`backtest.py`)

규칙 전용 15m 기준선(LLM 재현 불가). 선행편향 금지(신호 t 종가, 체결 t+1 시가), 수수료·슬리피지, 손절 우선, 워크포워드 3구간, 몬테카를로, 비용 2배 스트레스, 바이앤홀드 비교. 게이트 통과 못 하면 **NOT VALIDATED**로 정직하게 보고하고 파라미터를 손대지 않는다. LLM 파이프라인의 실제 성과는 paper 저널로만 측정한다.

## 10. 알려진 한계·위험 (요약; 상세는 `docs/research/agent_roster.md`)

과잉 보수(0거래)·비용 증가·지연·군집 사고·프롬프트 비대·추정 데이터 과신·스키마 드리프트·논지 재평가 변동·레짐 오탐·소표본 편향·paper→live 괴리·알림 장애. 각각의 완화책은 roster 문서 "위험과 완화" 절에 있고, 구현은 게이트 통계를 대시보드와 저널에 남겨 사람이 조정하게 한다.

## 11. 라이선스

Vibe-Trading(MIT)의 스킬은 **우리 말로 다시 쓰고** 공식·임계값만 가져오며 `skills/*/SKILL.md` frontmatter에 출처를 적는다. `THIRD_PARTY_NOTICES.md`에 MIT 고지. FinceptTerminal(AGPL)은 아이디어만 참고(코드·문구 없음).
