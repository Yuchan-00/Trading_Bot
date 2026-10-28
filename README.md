# Trading_Bot — Claude API 멀티에이전트 Binance 현물 봇

Claude 에이전트들이 역할을 나눠(기술분석 · 파생/자금흐름 · 뉴스 · 강세/약세 변론 · 리스크 관리 · 총괄 · 저널) 거래를 판단하고, **돈을 지키는 규칙은 전부 코드가 강제**하는 Binance 현물(USDT 페어, 롱 전용) 자동매매 봇입니다. 기본은 paper(가상 체결)이고, **모든 거래 이벤트는 텔레그램으로 알립니다**. 로컬 웹 대시보드로 상태를 봅니다.

> ⚠️ 투자 조언이 아니며 수익을 보장하지 않습니다. 규칙 전용 백테스트는 **NOT VALIDATED**가 나올 수 있고, LLM 결정은 백테스트로 재현되지 않습니다. 실자금(`MODE=live`)은 사용자 본인 책임입니다.

설계 [DESIGN.md](DESIGN.md) · 모듈 계약 [SPEC.md](SPEC.md) · 작업 기록 [docs/WORKLOG.md](docs/WORKLOG.md) · 조사 자료 [docs/research/](docs/research/) · 라이선스 고지 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

## 1. 구성 한눈에

```
스캐너(코드) ─ 후보·피처팩 ─▶ 분석가 3 (Haiku, 병렬, 서로 분리)
                                 technical(종목별) / derivatives_flow(일괄) / news_sentiment(새 헤드라인 있을 때만)
                      G1 ─▶ 강세·약세 변론 (Sonnet, 후보별, 서로 못 봄)
                      G2 ─▶ 리스크 관리자 (Sonnet, 거부권)
                      G3 ─▶ 총괄 (Opus, 결정할 게 있을 때만) ─▶ G4 ─▶ 주문 ─▶ 상태 저장 ─▶ 텔레그램
                                                         저널(Haiku, 청산 후·일일 리뷰) / 검증기(코드 백테스트)
```

| 역할 | 모델 | effort | 언제 | 스킬(`skills/`) |
|---|---|---|---|---|
| scanner | 없음(코드) | – | 매 사이클 | 지표·플로우·리스크·레짐 팩 계산 |
| technical | claude-haiku-5-5 | low | 후보 ∪ 보유 종목별 | technical-basic, ichimoku, candlestick, smc, volatility, minute-analysis |
| derivatives_flow | claude-haiku-5-5 | low | 사이클당 1회 | perp-funding-basis, crypto-derivatives, liquidation-heatmap, investor-lenses, market-microstructure |
| news_sentiment | claude-haiku-5-5 | low | 새 헤드라인 있을 때 | event-driven, sentiment-analysis, research-discipline |
| bull | claude-sonnet-5-5 | medium | G1 통과 후보별 | behavioral-finance, research-discipline, event-driven |
| bear | claude-sonnet-5-5 | medium | G1 통과 후보별 | investor-lenses, behavioral-finance, research-discipline, correlation-regime, perp-funding-basis |
| risk_manager | claude-sonnet-5-5 | medium | 후보 생존 또는 보유 있을 때 1회 | risk-analysis, correlation-regime, correlation-analysis, asset-allocation, market-microstructure, execution-model, thesis-tracker, trade-journal, strategy-dev-manager, volatility, behavioral-finance |
| head | claude-opus-5-5 | medium | 승인 후보·점검 대상 포지션 있을 때 1회 | investor-lenses, behavioral-finance, thesis-tracker, sentiment-analysis, event-driven, research-discipline |
| journal | claude-haiku-5-5 | low | 청산 직후·매일 00:05 UTC 이후 첫 사이클 | trade-journal, performance-attribution, report-generate, strategy-dev-manager |

스킬은 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)(MIT)의 90개 스킬을 전수 조사해 현물 롱 전용·공개 데이터에 맞는 25개를 골라 **우리 말로 다시 쓴** 것입니다([조사표](docs/research/vibe_skill_survey.md), [역할 구성 근거](docs/research/agent_roster.md)). FinceptTerminal(AGPL)은 아이디어만 참고했고 코드·문구는 없습니다.

## 2. 돈을 지키는 규칙 (코드 강제 — LLM·설정·대시보드로 못 바꿈)

| 한도 | 값 |
|---|---|
| 종목당 최대 비중 | 자본의 10% |
| 동시 포지션 | 3개 |
| 손절 거리 | ≤ 2% (체결가 기준으로 다시 고정) |
| 익절 거리 | ≤ 4% |
| 비용 반영 손익비 | ≥ 1.5 (왕복 비용 0.30% 반영, 손절만 조여서 맞춤) |
| 거래당 위험 | 자본의 0.15% (수수료 포함) |
| 확신도 하한 | 0.65 (레짐 FUSED·전략 쇠퇴·편향 심각·약세 기권 시 0.75) |
| 당일 손실 | −3% → 당일 신규 진입 중단 |
| 연속 손실 | 3회 → 당일 신규 진입 중단 |
| 낙폭 사다리 | −3% 사이즈 50% / −4.5% 신규 금지 / −6% 자동 STOP_ALL(사람이 해제) |
| 재진입 | 청산 후 4사이클, 손실 청산 후 8사이클 금지 |
| 시간 손절 | 96사이클(15분 기준 24시간) |
| 실행비용 | 예상 비용 > 최종 손절 거리의 25%면 진입 안 함 |
| 주문 | 현물 시장가, 롱 전용, 최소 주문액 이상 |
| LLM 비용 | 일일/월간 상한 초과 시 LLM 중단 → HOLD (손절·익절은 계속) |

총괄이 BUY를 내도 리스크 거부권, 확신도 하한, 사이즈·손절 상한, 유동성·실행비용 검사, 최소 주문액을 모두 통과해야 주문이 나갑니다. 손절/익절/시간손절은 LLM을 기다리지 않고 **매 사이클 가장 먼저** 코드가 집행합니다. 체결 후 거래소 응답 오류가 나도 주문을 다시 조회해 장부에 반영하고, testnet/live에서는 매 사이클 거래소 잔고와 장부를 대조합니다.

## 3. 설치

Python 3.11 이상.

```bash
git clone https://github.com/Yuchan-00/Trading_Bot.git
```

```bash
cd Trading_Bot
```

```bash
python -m venv .venv
```

```bash
# Windows PowerShell: .venv\Scripts\Activate.ps1   /  macOS·Linux:
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
# Windows: copy .env.example .env
cp .env.example .env
```

## 4. `.env` 설정

| 변수 | 설명 |
|---|---|
| `MODE` | `paper`(기본, 가상 체결) / `testnet` / `live` |
| `CONFIRM_LIVE` | live 에서만 `YES` 필요 |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com 에서 발급. `AGENT_BACKEND=anthropic` 일 때 필수 |
| `AGENT_BACKEND` | `anthropic`(기본) / `mock`(LLM 없이 고정 응답) |
| `MOCK_SCENARIO` | mock 일 때 `conservative`(진입 없음, 기본) / `approve`(paper 진입까지) |
| `MODEL_ANALYSIS` / `MODEL_RESEARCH` / `MODEL_HEAD` | 기본 `claude-haiku-5-5` / `claude-sonnet-5-5` / `claude-opus-5-5` |
| `LLM_DAILY_BUDGET_USD` / `LLM_MONTHLY_BUDGET_USD` | 기본 10 / 200 |
| `BINANCE_TESTNET_API_KEY/_SECRET` | testnet 키(https://testnet.binance.vision) |
| `BINANCE_LIVE_API_KEY/_SECRET` | live 키(**출금 권한 없이**, 가능하면 IP 제한) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 텔레그램 알림(아래 §6) |
| `SYMBOLS` | 기본 `BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT` (USDT 페어만) |
| `CYCLE_MINUTES` / `MAX_CANDIDATES_PER_CYCLE` / `PAPER_START_BALANCE_USDT` | 기본 15 / 2 / 10000 |
| `DATA_DIR` | 상태·로그 저장 폴더(기본 현재 폴더) |
| `NEWS_FEEDS` | RSS URL 쉼표 목록(기본 CoinDesk·CoinTelegraph·Decrypt·Bitcoin Magazine) |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | 기본 `127.0.0.1` / `8787` |
| `SCANNER_MIN_QUOTE_VOLUME_24H` 등 | 후보 선별 임계값 덮어쓰기(`SCANNER_MAX_SPREAD_BPS`, `SCANNER_MIN_DEPTH_1PCT_USDT`, `SCANNER_MIN_VOL_RATIO_15M`, `SCANNER_MAX_SPIKE_24H_PCT`, `SCANNER_SPIKE_VOLUME_MULT`, `SCANNER_FUNDING_WINDOW_S`) |

비밀값은 로그·텔레그램·대시보드·LLM 입력·예외 메시지에 들어가지 않습니다(마스킹 테스트 포함).

## 5. 처음 실행하는 순서 (권장)

**① 키 없이 체험** — 합성 시세 + mock 백엔드, 네트워크 없음, paper 전용. 데이터는 `<DATA_DIR>/offline_fixture/`에 따로 저장됩니다.

```bash
MODE=paper AGENT_BACKEND=mock MOCK_SCENARIO=approve python -m tradingbot.main run --once --offline-fixture
```

**② 텔레그램 연결 확인** — Claude 키 없이 동작합니다. 채팅에 테스트 메시지가 오면 정상입니다.

```bash
python -m tradingbot.main notify-test
```

**③ paper + 실제 Claude** — `.env`에 `MODE=paper`, `AGENT_BACKEND=anthropic`, `ANTHROPIC_API_KEY`를 넣고 1사이클만 돌려 비용(`cost_ledger.jsonl`)과 결정(`logs/decisions.jsonl`)을 확인합니다.

```bash
python -m tradingbot.main run --once
```

**④ 계속 실행 + 대시보드** — 다른 창에서 대시보드를 엽니다(http://127.0.0.1:8787).

```bash
python -m tradingbot.main run
```

```bash
python -m tradingbot.main dashboard
```

**⑤ testnet** — `MODE=testnet` + 테스트넷 키. 실제 거래소 주문 경로를 검증합니다.

**⑥ live** — `MODE=live` + `CONFIRM_LIVE=YES` + 실계정 키, 그리고 시작할 때 **콘솔(TTY)에서 `LIVE`를 직접 입력**해야 합니다. 파이프·리다이렉트(`echo LIVE | ...`)로는 시작되지 않습니다.

로그는 표준출력/표준에러로 나옵니다. 파일로 남기려면 PowerShell 에서 `python -m tradingbot.main run *>> bot.out`, macOS·Linux 에서 `python -m tradingbot.main run >> bot.out 2>&1`.

## 6. 명령어

| 명령 | 설명 |
|---|---|
| `run` | 사이클 경계 + 5초마다 실행(기본 15분). `--once` 1사이클만, `--cycle-minutes N` 주기 변경, `--offline-fixture` 합성 시세 체험 |
| `dashboard` | 로컬 대시보드(읽기 전용 + STOP/해제) |
| `backtest --days 90 [--symbols BTC/USDT ETH/USDT]` | 규칙 전용 기준선 백테스트(LLM 없음, Binance 공개 시세 필요) |
| `status` | 상태 요약(파일만 읽음, API 키 없어도 동작) |
| `stop --reason "..."` | `control/STOP` 작성 → 다음 사이클에 킬스위치 |
| `clear-stop` | `control/STOP` 삭제 + `control/CLEAR` 작성 → 다음 사이클에 해제 |
| `notify-test` | 텔레그램 테스트 메시지 1통(성공 0 / 실패 1 / 설정 없음 2) |

모두 `python -m tradingbot.main <명령>` 으로 실행합니다. 킬스위치는 자동으로 풀리지 않습니다(사람이 `clear-stop` 또는 대시보드 **해제**). `bot.lock`이 동시 실행을 막고, 죽은 프로세스나 오래 갱신되지 않은 잠금(`max(3×사이클, 30분)`)은 새 실행이 넘겨받습니다.

## 7. 텔레그램 알림

설정: 텔레그램에서 @BotFather 로 봇을 만들어 토큰을 받고(`TELEGRAM_BOT_TOKEN`), 그 봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 의 `chat.id` 를 `TELEGRAM_CHAT_ID` 에 넣습니다. 그다음 `notify-test`.

| 이벤트 | 메시지 |
|---|---|
| 진입 체결 | 🟢 진입 [모드] 종목 · 수량 @ 가격(금액) · 손절/익절 가격과 % · 확신도 · 근거·가정·약세 최강 논점 |
| 청산 체결(손절·익절·시간손절·총괄 매도·킬스위치) | 🔴(손실)/🔵(이익) 청산 · 사유 · 손익 USDT·% · 자본 |
| 부분 청산·트림 | 🟠 부분 청산 |
| 주문 거부·게이트 강등(총괄 BUY → HOLD) | ⚠️ 사유(게이트 이름·리스크 거부·약세 킬) |
| 신규 진입 중단/재개 | ⛔ 일일 −3%·연속 3패·낙폭·예산·LLM 일시정지 / ✅ 재개 |
| 킬스위치 | ⛔ STOP(CLI·대시보드)·낙폭 −6% 자동 STOP_ALL |
| 레짐 전환·NAME 알람 | 🌐 이전→현재와 적용 규칙(FUSED 사이즈 50%·하한 0.75 / MACRO 신규 금지) |
| 뉴스 위험 | 📰 종목·사건 유형·점수·요약·보유 여부 |
| 포지션 점검 | 🩺 건강도·레드라인·리스크 조치·총괄 결정 |
| 사후분석·일일 리뷰 | 📝 청산 직후 / 📊 매일(손익·승률·편향·게이트 통계·LLM 비용) |
| 예산 초과·LLM 일시정지·오류 | 💸 / ⏸ / ❗ |
| 시작·종료·하트비트(4시간) | ▶️ / ⏹ / 💓 자본·당일 손익·보유·노출·LLM 비용 |

알림은 **상태 저장 뒤** 큐에 넣고 별도 스레드가 보냅니다. 텔레그램 장애가 거래를 막지 않고(429 응답은 `retry_after` 만큼 대기 후 재시도), 끝내 실패한 메시지는 `logs/failed_notifications.jsonl`에 남습니다.

## 8. 대시보드

`dashboard` 실행 후 http://127.0.0.1:8787. 외부 CDN 없이 동작하고 파일만 읽습니다(상태·포지션·결정·거래·비용·백테스트·자본 스파크라인). **STOP/해제** 버튼은 `<DATA_DIR>/control/dashboard_token`(권한 0600)의 토큰을 입력해야 동작하며, `control/STOP`·`control/CLEAR` 파일만 씁니다. Host 헤더를 검사해 외부 사이트가 대시보드를 읽지 못하게 합니다.

## 9. 비용

| 모델 | 입력 / 캐시 읽기 / 출력 (USD per 1M 토큰) |
|---|---|
| claude-haiku-5-5 | 0.10 / 0.01 / 0.50 |
| claude-sonnet-5-5 | 2.00 / 0.20 / 10.00 |
| claude-opus-5-5 | 4.00 / 0.20 / 20.00 |

역할별 시스템 프롬프트(프롬프트 + 스킬, 최대 약 1.5만 토큰)는 바이트 단위로 고정되어 1시간 캐시에 올라가므로, 매 호출 비용은 주로 입력 데이터와 출력(사고 토큰 포함)입니다. 대략치: 셋업 없는 사이클 ≈ $0.005, 보유 포지션 점검 사이클 ≈ $0.05, 후보 2개가 끝까지 가는 사이클 ≈ $0.35. 하루 96사이클 중 결정 사이클이 10회 안팎이면 하루 약 $5~7입니다. 시장이 활발해 결정 사이클이 많으면 **일일 상한(`LLM_DAILY_BUDGET_USD`, 기본 $10)에서 그날 LLM 호출이 멈추고 HOLD** 합니다(손절·익절은 계속). 호출 전에 최악 비용(1시간 캐시 쓰기 2배 포함)으로 예산을 검사합니다. 실제 사용량은 `cost_ledger.jsonl`과 대시보드 "비용" 패널에서 확인합니다.

## 10. `DATA_DIR`에 생기는 파일

| 파일 | 내용 |
|---|---|
| `state.json` | 포지션·잔고(paper)·당일 통계·쿨다운·킬스위치·LLM 일시정지 (원자적 저장; testnet/live에서 손상되면 멈춤) |
| `bot.lock` | 단일 실행 잠금(PID·호스트·갱신 시각) |
| `control/` | `STOP`·`CLEAR` 요청 파일, `dashboard_token` |
| `logs/decisions.jsonl` | 사이클별 결정·게이트·비용 요약 |
| `logs/cycles/<날짜>/cycle_<n>.json/.md` | 사이클 보고서 전문 |
| `logs/journal.jsonl` | 청산 거래와 사후분석 |
| `logs/failed_notifications.jsonl` | 전송 실패한 알림 |
| `cost_ledger.jsonl` | LLM 호출별 토큰·비용 |
| `journal_state.json` | 논지·레드라인·진입 판정 스냅샷·게이트 통계 |
| `news_store.json` / `alert_state.json` / `equity_daily.json` | 뉴스 이벤트 / 알림 중복 방지 / 일별 자본 곡선 |
| `cache/` | 스캐너 이력, 공포탐욕 지수 |
| `backtest_cache/`, `backtest_results/` | 백테스트 시세 캐시·결과(JSON+Markdown) |

## 11. 백테스트(검증기)

`backtest`는 LLM 없이 규칙 전용 기준선을 돌립니다. 선행편향 금지(신호 t 종가, 체결 t+1 시가), 수수료 0.1%+슬리피지, 손절 우선, 워크포워드 3구간, 몬테카를로, 비용 2배 스트레스, 바이앤홀드 비교. 게이트를 통과하지 못하면 **NOT VALIDATED**로 보고하고 파라미터를 바꾸지 않습니다. LLM 파이프라인의 실제 성과는 paper 운용 기록(`logs/journal.jsonl`, 일일 리뷰)으로만 측정됩니다.

## 12. 알려진 한계 (정직하게)

- 이 저장소의 테스트(3,872개)는 전부 오프라인입니다: 가짜 거래소·mock 백엔드·가짜 텔레그램. **실제 Claude API 호출, 실제 텔레그램 전송, Binance testnet 주문은 개발 환경에서 키·네트워크가 없어 실행해 보지 못했습니다.** §5의 ②③⑤ 순서로 직접 확인하세요.
- 백테스트는 LLM 결정을 재현하지 못합니다. 이전 버전의 90일 규칙 기준선은 NOT VALIDATED(순 −1.87%, PF 0.71)였습니다.
- 청산 군집 등 일부 파생 지표는 **추정값**이며 경고·차단에만 쓰고 진입 근거로 쓰지 않습니다. 공포탐욕 지수는 외부 API(alternative.me)가 실패하면 빠진 채로 계산합니다.
- 게이트가 겹겹이라 거래가 거의 나지 않을 수 있습니다(의도된 보수성). 대시보드·일일 리뷰의 게이트 통계로 조정 여부를 사람이 판단합니다.
- 총괄(Opus 5.5)은 사고 기능을 끌 수 없어 응답이 길어질 수 있습니다. 역할별 타임아웃 합(270초)이 사이클 기한(300초) 안에 들어가도록 잡았고, 기한을 넘기면 남은 단계는 HOLD입니다.
- paper 체결은 호가+슬리피지 가정이며 실제 체결과 다를 수 있습니다.

## 13. 개발

```bash
python -m pytest -q
```

모듈 계약은 `SPEC.md`(마지막 "SPEC v2.1" 절이 우선)가 기준이며, 바꿀 때는 문서를 먼저 고칩니다. 역할 프롬프트는 `prompts/<역할>.md`, 스킬은 `skills/<이름>/SKILL.md`. 개발 과정에서 쓴 오케스트레이션 스크립트는 `docs/workflows/`에 있습니다.
