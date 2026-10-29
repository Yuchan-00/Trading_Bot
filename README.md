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

macOS·Linux:

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Windows(cmd·PowerShell 공통) — venv 를 활성화하지 않고 venv 의 python 을 직접 씁니다. PowerShell 의 `Activate.ps1` 은 기본 실행 정책에서 막히므로("이 시스템에서 스크립트를 실행할 수 없으므로…") 이 방법이 가장 간단합니다. 활성화하려면 cmd 에서 `.venv\Scripts\activate.bat`, 또는 PowerShell 에서 한 번 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 뒤 `.venv\Scripts\Activate.ps1`. 아래 명령의 `python` 은 활성화하지 않았다면 `.venv\Scripts\python.exe` 로 바꿔 쓰세요.

```bash
.venv\Scripts\python.exe -m pip install --upgrade pip
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
copy .env.example .env
```

`.env` 는 메모장으로 열어 고친 뒤 **UTF-8** 로 저장합니다(Windows 10 1903 이후 메모장 기본값). PowerShell 의 `>`·`>>` 로 만들거나 덧붙이지 마세요 — Windows PowerShell 5.1 의 `>` 는 UTF-16 으로 저장합니다(봇은 경고와 함께 읽지만 UTF-8 로 다시 저장하세요). `>>` 나 cmd·Git Bash 로 줄을 덧붙여 인코딩이 섞이면 봇은 문제 줄 번호를 알려 주고 시작하지 않습니다. 메모장이 이름을 `.env.txt` 로 저장하는 경우가 있으니 확장자를 확인하세요.

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
| `DATA_DIR` | 상태·로그 저장 폴더(기본: 봇을 실행한 폴더). Windows 경로는 따옴표 없이(`DATA_DIR=B:\TradingBot\data`) 또는 `/` 로(`B:/TradingBot/data`). 큰따옴표 안의 `\t \b \n` 은 특수문자로 바뀌고 `%USERPROFILE%` 은 펼쳐지지 않아 봇이 거부합니다 |
| `LOG_FILE` | 로그 파일(예: `logs/bot.log` — `DATA_DIR` 기준, UTF-8, `.env.example` 기본값). 비우거나 `none` 이면 콘솔만 |
| `NEWS_FEEDS` | RSS URL 쉼표 목록(기본 CoinDesk·CoinTelegraph·Decrypt·Bitcoin Magazine) |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | 기본 `127.0.0.1` / `8787` |
| `SCANNER_MIN_QUOTE_VOLUME_24H` 등 | 후보 선별 임계값 덮어쓰기(`SCANNER_MAX_SPREAD_BPS`, `SCANNER_MIN_DEPTH_1PCT_USDT`, `SCANNER_MIN_VOL_RATIO_15M`, `SCANNER_MAX_SPIKE_24H_PCT`, `SCANNER_SPIKE_VOLUME_MULT`, `SCANNER_FUNDING_WINDOW_S`) |

비밀값은 로그·텔레그램·대시보드·LLM 입력·예외 메시지에 들어가지 않습니다(마스킹 테스트 포함).

같은 이름의 **환경변수가 `.env` 보다 우선**합니다. 콘솔 창에서 `set`·`$env:` 로 넣은 값은 그 창을 닫을 때까지 남고, `setx` 로 넣은 값은 영구적입니다. 봇은 환경변수가 `.env` 값을 덮어쓰면 그 이름을 경고합니다(값은 쓰지 않음, `.env` 의 빈 값은 제외). 확인: PowerShell `Get-ChildItem Env:AGENT_BACKEND`(지금 창), `[Environment]::GetEnvironmentVariable('AGENT_BACKEND','User')`(`setx` 로 넣은 영구 값). 지금 창에서만 지우기: PowerShell `Remove-Item Env:AGENT_BACKEND`, cmd `set AGENT_BACKEND=`. `setx` 로 넣은 값까지 지우기: PowerShell `[Environment]::SetEnvironmentVariable('AGENT_BACKEND', $null, 'User')`(관리자로 `setx /M` 했다면 관리자 PowerShell 에서 `'Machine'`) 또는 시작 > '시스템 환경 변수 편집' > 환경 변수 > 사용자 변수에서 삭제한 뒤, **시작 메뉴에서 새 창**을 여세요. `reg delete HKCU\Environment /v AGENT_BACKEND /f` 로 지웠다면 로그아웃 후 다시 로그인해야 새 창에 반영됩니다.

## 5. 처음 실행하는 순서 (권장)

**① 키 없이 체험** — 합성 시세 + mock 백엔드(자동), 네트워크·API 키 없음, paper 전용. 데이터는 `<DATA_DIR>/offline_fixture/`에 따로 저장됩니다. cmd·PowerShell·bash 어디서나 같은 명령입니다.

```bash
python -m tradingbot.main run --once --offline-fixture --mock-scenario approve
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

로그는 콘솔(표준에러)에 나오고, `.env` 에 `LOG_FILE=logs/bot.log` 를 두면 `<DATA_DIR>/logs/bot.log` 에도 UTF-8 로 남습니다(`.env.example` 기본값). PowerShell 에서 실시간으로 보려면 `Get-Content <DATA_DIR>\logs\bot.log -Wait -Tail 50`. **PowerShell 에서 `>`·`>>`·`*>>`·`| Tee-Object` 로 봇 출력을 돌리지 마세요** — 그러면 Ctrl+C 가 봇을 즉시 강제 종료하고(종료 알림·잠금 해제 없음), live 의 `LIVE` 입력 안내가 보이지 않으며, Windows PowerShell 5.1 에서는 한글이 깨집니다.

**Windows 에서 오래 돌릴 때**

- **종료는 Ctrl+C**(또는 Ctrl+Break) **한 번 누르고 기다리세요**. 사이클 사이 대기 중이거나 Claude 판단 단계(분석가·변론·리스크·총괄)면 1초 안에 "종료 신호 수신" 로그가 나옵니다. 사후분석·일일 리뷰 호출이나 거래소 호출 중이면 그 호출이 끝난 뒤(최대 1분쯤) 나옵니다 — 다시 눌러도 괜찮지만 창은 닫지 마세요. 그 사이클을 마친 뒤 ⏹ 알림을 보내고 잠금을 풉니다. 창 닫기(X)·작업 관리자 "작업 끝내기"·로그오프·재부팅은 즉시 강제 종료입니다(⏹ 없음). 다음 실행이 남은 잠금을 넘겨받고 ▶️ 시작 알림에 "이전 실행이 정상 종료되지 않았습니다"를 붙입니다.
- **절전**: `run` 이 도는 동안 자동 절전(유휴 타이머)은 봇이 막습니다(화면 꺼짐은 허용). 덮개 닫기·전원 버튼·시작 > 절전, 배터리 사용 중 모던 스탠바이는 막을 수 없습니다. **PC 가 잠든 동안에는 손절·익절이 집행되지 않으므로** 전원을 연결하고 "덮개를 닫을 때: 아무것도 안 함"으로 두세요(설정 > 시스템 > 전원). 깨어나면 바로 사이클을 돌리고, 공백이 길었으면 🔔 사이클 공백 경보를 보냅니다.
- **콘솔 창 클릭·Pause 키**: 기본 콘솔(conhost)에서 창 안을 클릭·드래그하면 "선택" 모드가 되고, **Pause** 키(Windows Terminal 은 Ctrl+S 도)를 누르면 출력이 일시정지되어 화면이 멈춥니다. 봇은 로그를 별도 스레드로 내보내므로 거래(손절·익절)와 `LOG_FILE` 기록은 계속되지만, 화면을 다시 갱신하려면 선택은 **Esc**, 일시정지는 아무 글자·Space·Enter 로 푸세요.
- **시계**: Binance 는 서명 요청의 시각이 서버보다 1초 이상 빠르면 거부합니다(-1021). 봇이 자동으로 시계 차이를 다시 재지만 "설정 > 시간 및 언어 > 자동으로 시간 설정"을 켜 두세요.
- **실행 폴더**: `.env` 와 상대 경로 `DATA_DIR` 은 **현재 작업 폴더** 기준입니다. 바로가기·`.bat`·작업 스케줄러는 시작 위치를 봇 폴더로 두세요(`.bat` 이면 `cd /d B:\TradingBot`). 시작 로그에 실제로 읽은 `.env` 와 데이터 폴더의 절대 경로가 나오고, `.env` 를 못 찾으면 경고합니다. `stop`/`clear-stop` 은 쓴 파일의 절대 경로와 대상 봇 PID 를 보여 주고, 그 폴더에서 실행 중인 봇이 없으면 경고합니다.
- **다른 프로그램이 봇 파일을 열고 있을 때**(백신 검사·동기화 프로그램·Excel 로 연 `cost_ledger.jsonl` 등): Windows 는 그동안 파일 교체·삭제를 거부합니다. 봇은 잠시 기다렸다가 다시 시도하고(`state.json` 은 최대 10초), 비용 원장이 잠겨 있으면 `cost_ledger.pending.jsonl` 에 따로 기록합니다(합계는 두 파일을 더함). 봇 폴더는 OneDrive 같은 동기화 폴더 밖에 두는 것을 권합니다.
- **이 PC 에 다른 사용자 계정이 있으면**: Windows 는 봇 파일의 권한을 따로 좁히지 않고 봇 폴더의 권한을 물려받습니다. `C:\Users\<이름>` 아래면 본인만 읽을 수 있지만, `B:\TradingBot` 처럼 드라이브 바로 아래 만든 폴더는 다른 계정도 `.env`(API 키)·대시보드 토큰을 읽고 `control` 파일(STOP·CLEAR)을 쓸 수 있습니다. 봇 폴더를 사용자 폴더 아래로 옮기거나, cmd 에서 한 번 `icacls B:\TradingBot /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"` 로 본인·SYSTEM·관리자만 남기세요(FAT/exFAT 드라이브는 권한 기능이 없어 옮기는 수밖에 없습니다).

## 6. 명령어

| 명령 | 설명 |
|---|---|
| `run` | 사이클 경계 + 5초마다 실행(기본 15분). `--once` 1사이클만, `--cycle-minutes N` 주기 변경, `--offline-fixture` 합성 시세 체험(API 키 불필요), `--mock-scenario conservative\|approve` mock 응답 선택 |
| `dashboard` | 로컬 대시보드(읽기 전용 + STOP/해제) |
| `backtest --days 90 [--symbols BTC/USDT ETH/USDT]` | 규칙 전용 기준선 백테스트(LLM 없음, Binance 공개 시세 필요) |
| `status` | 상태 요약(파일만 읽음, API 키 없어도 동작) |
| `stop --reason "..."` | `control/STOP` 작성 → 다음 사이클에 킬스위치(절대 경로·대상 봇 PID 표시, 실행 중인 봇이 없으면 경고) |
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

`dashboard` 실행 후 http://127.0.0.1:8787. 외부 CDN 없이 동작하고 파일만 읽습니다(상태·포지션·결정·거래·비용·백테스트·자본 스파크라인). **STOP/해제** 버튼은 `<DATA_DIR>/control/dashboard_token`(macOS·Linux 는 권한 0600. Windows 는 따로 좁히지 않고 봇 폴더의 권한을 물려받습니다 — §5 'Windows 에서 오래 돌릴 때'의 '다른 사용자 계정' 참고)의 토큰을 입력해야 동작하며, `control/STOP`·`control/CLEAR` 파일만 씁니다. Host 헤더를 검사해 외부 사이트가 대시보드를 읽지 못하게 합니다.

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
| `logs/bot.log` | 실행 로그(`LOG_FILE` 설정 시, UTF-8) |
| `cost_ledger.jsonl` | LLM 호출별 토큰·비용(원장이 다른 프로그램에 잠겼을 때는 `cost_ledger.pending.jsonl`) |
| `journal_state.json` | 논지·레드라인·진입 판정 스냅샷·게이트 통계 |
| `news_store.json` / `alert_state.json` / `equity_daily.json` | 뉴스 이벤트 / 알림 중복 방지 / 일별 자본 곡선 |
| `cache/` | 스캐너 이력, 공포탐욕 지수 |
| `backtest_cache/`, `backtest_results/` | 백테스트 시세 캐시·결과(JSON+Markdown) |

## 11. 백테스트(검증기)

`backtest`는 LLM 없이 규칙 전용 기준선을 돌립니다. 선행편향 금지(신호 t 종가, 체결 t+1 시가), 수수료 0.1%+슬리피지, 손절 우선, 워크포워드 3구간, 몬테카를로, 비용 2배 스트레스, 바이앤홀드 비교. 게이트를 통과하지 못하면 **NOT VALIDATED**로 보고하고 파라미터를 바꾸지 않습니다. LLM 파이프라인의 실제 성과는 paper 운용 기록(`logs/journal.jsonl`, 일일 리뷰)으로만 측정됩니다.

## 12. 알려진 한계 (정직하게)

- 이 저장소의 테스트(4,011개)는 전부 오프라인입니다: 가짜 거래소·mock 백엔드·가짜 텔레그램. **실제 Claude API 호출, 실제 텔레그램 전송, Binance testnet 주문은 개발 환경에서 키·네트워크가 없어 실행해 보지 못했습니다.** §5의 ②③⑤ 순서로 직접 확인하세요.
- Windows 동작(파일 공유 위반 재시도, Ctrl+C·Ctrl+Break, 절전 막기, 콘솔 선택 모드, 시계 보정)은 CI 의 windows-latest 와 리눅스에서의 흉내로 확인했습니다. 실제 PC 의 콘솔 클릭·절전 복귀·백신/동기화 프로그램의 파일 잠금은 직접 겪어 보며 로그(`LOG_FILE`)로 확인해 주세요.
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
