# Trading_Bot — Claude API 멀티에이전트 Binance 현물 봇

Claude 에이전트들이 역할을 나눠(기술분석 · 파생/자금흐름 · 뉴스 · 강세/약세 변론 · 리스크 관리 · 총괄 · 저널) 거래를 판단하고, **돈을 지키는 규칙은 전부 코드가 강제**하는 Binance 현물(USDT 페어, 롱 전용) 자동매매 봇입니다. 기본은 paper(가상 체결)이고, 모든 거래 이벤트는 텔레그램으로 알립니다. 로컬 웹 대시보드로 상태를 봅니다.

> ⚠️ 이 소프트웨어는 투자 조언이 아니며 수익을 보장하지 않습니다. 규칙 전용 백테스트는 **NOT VALIDATED**가 나올 수 있고, LLM 결정은 백테스트로 재현되지 않습니다. 실자금(`MODE=live`)은 사용자 본인 책임이며 봇은 권하지 않습니다.

설계: [DESIGN.md](DESIGN.md) · 모듈 계약: [SPEC.md](SPEC.md) · 작업 기록: [docs/WORKLOG.md](docs/WORKLOG.md) · 조사 자료: [docs/research/](docs/research/)

## 1. 무엇이 돈을 지키나 (코드 강제, LLM·설정으로 못 바꿈)

| 한도 | 값 |
|---|---|
| 종목당 최대 비중 | 자본의 10% |
| 동시 포지션 | 3개 |
| 손절 거리 | ≤ 2% (체결가 기준 재고정) |
| 익절 거리 | ≤ 4% |
| 당일 손실 | −3% → 당일 신규 진입 중단 |
| 연속 손실 | 3회 → 당일 신규 진입 중단 |
| 확신도 하한 | 0.65 (레짐 FUSED·전략 쇠퇴·편향 심각 시 0.75) |
| 재진입 쿨다운 | 청산 후 4사이클, 손실 청산 후 8사이클 |
| 낙폭 사다리 | −3% 사이즈 50% / −4.5% 신규 금지 / −6% 자동 STOP_ALL |
| 거래당 위험 | 자본의 0.15%(수수료 포함), 비용 반영 손익비 ≥ 1.5 |
| 시간 손절 | 96사이클(15분 기준 24시간) |
| 주문 | 현물 시장가만, 롱 전용 |
| LLM 비용 | 일일/월간 상한 초과 시 LLM 중단 → HOLD (손절/익절은 계속) |

LLM 출력은 `risk.py`와 게이트(G0~G4)가 **조이기만** 합니다. 총괄이 BUY를 내도 리스크 관리자의 거부권(veto), 확신도 하한, 사이즈·손절 상한, 유동성·슬리피지 검사, 최소 주문액을 통과해야 주문이 나갑니다.

## 2. 설치

```bash
git clone https://github.com/Yuchan-00/Trading_Bot.git
cd Trading_Bot
python -m venv .venv
```

```bash
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
copy .env.example .env   # Windows (macOS/Linux: cp .env.example .env)
```

Python 3.11 이상. 패키지: `anthropic`, `ccxt`, `pydantic`, `fastapi`, `feedparser` 등(`requirements.txt`).

## 3. `.env` 설정

```dotenv
MODE=paper                      # paper | testnet | live
ANTHROPIC_API_KEY=sk-ant-...    # https://console.anthropic.com
AGENT_BACKEND=anthropic         # mock 이면 LLM 없이 오프라인 실행(보수적 HOLD)
MODEL_ANALYSIS=claude-haiku-5-5  # 기술·파생·뉴스·저널
MODEL_RESEARCH=claude-sonnet-5-5 # 강세·약세·리스크
MODEL_HEAD=claude-opus-5-5       # 총괄
LLM_DAILY_BUDGET_USD=10
LLM_MONTHLY_BUDGET_USD=200
TELEGRAM_BOT_TOKEN=...          # @BotFather 에서 발급
TELEGRAM_CHAT_ID=...            # 봇과 대화 후 getUpdates 로 확인
SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT
CYCLE_MINUTES=15
PAPER_START_BALANCE_USDT=10000
```

- `MODE=testnet`: https://testnet.binance.vision 에서 키를 만들어 `BINANCE_TESTNET_API_KEY/_SECRET` 입력.
- `MODE=live`: `BINANCE_LIVE_API_KEY/_SECRET` + `CONFIRM_LIVE=YES` + 시작할 때 콘솔에 `LIVE` 입력. 셋 중 하나라도 없으면 시작하지 않습니다. 실계정 키는 **출금 권한 없이**, 가능하면 IP 제한을 걸어 만드세요.
- 비밀값은 로그·텔레그램·LLM 입력·예외 메시지에 들어가지 않습니다(`config.mask_secret`).

## 4. 실행

```bash
python -m tradingbot.main run            # 15분 주기로 계속 실행
```

```bash
python -m tradingbot.main run --once     # 1사이클만
```

```bash
python -m tradingbot.main dashboard      # http://127.0.0.1:8787
```

```bash
python -m tradingbot.main backtest --days 90   # 규칙 전용 기준선 백테스트 (LLM 없음)
```

```bash
python -m tradingbot.main status         # 상태 요약
```

```bash
python -m tradingbot.main stop --reason "수동 정지"   # 다음 사이클에 킬스위치
```

키 없이 동작을 확인하려면: `MODE=paper AGENT_BACKEND=mock` 으로 `run --once` (보수적 HOLD). 합성 시세로 돌리는 `--offline-fixture` 옵션은 구현 후 이 문서를 갱신합니다.

### 한 사이클에서 일어나는 일
1. **G0 사전점검(코드)**: 킬스위치, 낙폭 사다리, 당일 손실, 연속 손실, LLM 일시정지, 예산, 데이터 품질, 펀딩 정산 분(00/08/16 UTC ±1분 신규 주문 금지).
2. **코드 청산**: 보유 포지션의 손절/익절/시간손절을 먼저 집행(LLM을 기다리지 않음) → 텔레그램.
3. **스캐너(코드)**: 유동성 게이트, 후보 랭킹, 피처팩(가격 구조·파생/자금흐름·리스크·레짐·종합심리).
4. **분석가 3(Haiku, 병렬·서로 분리)**: 기술 / 파생·자금흐름 / 뉴스(새 헤드라인 있을 때만).
5. **G1** → **강세·약세 변론(Sonnet, 독립)** → **G2(약세 decline이면 탈락)** → **리스크 관리자(Sonnet, 거부권)** → **G3** → **총괄(Opus, 결정할 게 있을 때만)** → **G4** → 주문 → 상태 저장 → 텔레그램.
6. 결정 로그(`logs/decisions.jsonl`), 사이클 보고서(`logs/cycles/`), 비용 원장(`cost_ledger.jsonl`).

호출 수: 후보 2개·보유 1개 기준 최악 11회(Haiku 5, Sonnet 5, Opus 1), 보통 7~8회. 셋업이 없으면 Sonnet/Opus는 호출되지 않습니다.

## 5. 텔레그램 알림

진입 · 청산(모든 사유와 손익) · 부분체결/거부/거래소 오류 · 게이트에 의한 강등(총괄 BUY→HOLD, 리스크 거부, 약세 킬) · 중단/해제(일일 한도, 연속 손실, 낙폭 사다리, 킬스위치) · 레짐 전환 · 뉴스 risk_event · 포지션 건강도 하락 · 일일 리뷰(00:05 UTC) · 시작/종료/오류/LLM 일시정지 · 하트비트(4시간).

알림은 큐에 넣고 별도 스레드가 보내므로 텔레그램 장애가 거래를 막지 않습니다. 실패한 메시지는 `logs/notify_failed.jsonl`에 남습니다.

## 6. 대시보드

`python -m tradingbot.main dashboard` 후 브라우저에서 `http://127.0.0.1:8787`. 외부 CDN 없이 동작하며 파일만 읽습니다(상태·포지션·결정·거래·비용·백테스트·자본 스파크라인). **STOP** 버튼은 `control/dashboard_token` 파일의 토큰을 입력해야 동작하고, `control/STOP` 파일을 만들어 다음 사이클에 킬스위치를 켭니다.

## 7. 스킬과 역할

`skills/<이름>/SKILL.md`는 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)(MIT)의 스킬을 현물 롱 전용 봇에 맞게 **우리 말로 다시 쓴** 것입니다(출처는 각 파일 frontmatter, 라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)). 역할별 배정은 [DESIGN.md §2](DESIGN.md)를 보세요. 역할 프롬프트는 `prompts/<역할>.md`이며, 프롬프트+스킬 본문은 바이트 단위로 고정되어 Claude 프롬프트 캐시(1시간 TTL)에 올라갑니다.

## 8. 비용

역할별 모델: 분석 Haiku 5.5($0.10/$0.50 per MTok), 리서치 Sonnet 5.5($2/$10), 총괄 Opus 5.5($4/$20). 호출당 입력 6K·출력 0.8K 가정 시 사이클당 보통 $0.08, 최악 $0.25 → 하루 대략 $6~10(캐시 적용 전). `LLM_DAILY_BUDGET_USD`를 넘으면 그날은 LLM 호출을 멈추고 HOLD합니다. 실제 사용량은 `cost_ledger.jsonl`과 대시보드 "비용" 패널에서 확인합니다.

## 9. 백테스트(검증기)

`backtest` 명령은 LLM 없이 규칙 전용 기준선을 돌립니다: 선행편향 금지(신호 t 종가, 체결 t+1 시가), 수수료 0.1%+슬리피지, 손절 우선, 워크포워드 3구간, 몬테카를로, 비용 2배 스트레스, 바이앤홀드 비교. 게이트를 통과하지 못하면 **NOT VALIDATED**로 보고하고 파라미터를 바꾸지 않습니다. 결과는 `backtest_results/`에 JSON+Markdown으로 저장됩니다.

## 10. 알려진 한계

- 백테스트는 LLM 결정을 재현하지 못합니다. 실제 성과는 paper 운용의 `logs/journal.jsonl`로만 측정됩니다.
- 청산 군집 등 일부 파생 지표는 **추정값**이며 차단(진입 금지)에만 쓰고 진입 근거로는 쓰지 않습니다.
- 게이트가 겹겹이라 거래가 거의 나지 않을 수 있습니다(의도된 보수성). 대시보드의 게이트 통계로 조정 여부를 사람이 판단합니다.
- 테스트넷/실거래소 주문 경로는 키가 있어야 검증됩니다. paper 체결은 호가+슬리피지 가정입니다.

## 11. 개발

```bash
python -m pytest -q
```

모든 테스트는 오프라인(가짜 거래소·mock 백엔드·가짜 텔레그램)입니다. 모듈 계약은 `SPEC.md`가 기준이며, 바꿀 때는 문서를 먼저 고칩니다.
