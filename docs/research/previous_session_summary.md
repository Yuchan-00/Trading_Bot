# 이전 세션 요약: "Binance 멀티에이전트 트레이딩 봇" (2026-10-20 ~ 21)

출처: 세션 `session_01HkzE7YjmjQ5hy4Ck6Q72UM`의 마지막 100개 이벤트(2026-10-20 22:40~22:42Z)와 세션 말미에 저장된 프로젝트 메모리(`tradingbot-project.md`). 앞부분 기록은 읽는 도중 세션이 접근 불가(not found)로 바뀌어 읽지 못함. 비밀값(키·토큰)은 포함하지 않음.

## 1. 결과물 위치와 상태
- 프로젝트 폴더: 사용자 PC `B:\TradingBot` (전용 venv `B:\TradingBot\.venv`). **2026-10-21 기준 git 저장소가 아니었고**, GitHub `Yuchan-00/Trading_Bot`에는 push되지 않음.
- 납품 시점: 2026-10-21. 테스트 830개 전부 통과(`pytest -q -p no:cacheprovider`, 약 18초).
- 모델 호출 방식: `claude -p --output-format json --json-schema … --tools "" --no-session-persistence --strict-mcp-config --disable-slash-commands --permission-prompts none --model <model> --system-prompt-file <임시파일>` 서브프로세스. 사용자 프롬프트(JSON)는 stdin. `ANTHROPIC_*`, `BINANCE_*`, `TELEGRAM_*` 환경변수는 서브프로세스에서 제거(구독만 사용, 키는 LLM 쪽으로 가지 않음). 타임아웃 시 Windows는 `taskkill /F /T`로 프로세스 트리 종료. CLI v2.1.299에는 `--max-turns`가 없어 도구를 꺼서 단일 턴으로 만듦.
- 이유(메모리 기록): 사용자는 "코드가 강제하는 안전장치가 있는 자율 결정"과 "API 키 지출 없음"을 원했음. → 이번 세션에서 사용자가 **Claude API 사용으로 변경**을 요청함.

## 2. 파일 구성 (세션 말미 `ls B:\TradingBot`)
`DESIGN.md`, `README.md`(한국어), `SPEC.md`(모듈 계약), `agents.py`, `backtest.py`, `backtest_cache/`, `backtest_results/`, `claude_cli.py`, `config.py`, `cycle.py`, `docs/`(SKILL_REVIEW.md, ADOPTION_PLAN.md), `exchange.py`, `indicators.py`, `logs/`, `main.py`, `models.py`, `news.py`, `notify.py`, `portfolio.py`, `prompts/`(7개), `pytest.ini`, `quality.py`, `reports.py`, `requirements.txt`, `risk.py`, `state.json`, `tests/`, `vendor/`(gitignore, 참조 스킬 팩 원본+LICENSE), `.env.example`, `.env`(사용자 편집, 비밀 포함).

## 3. 아키텍처
- 에이전트 7개: `scanner`, `technical`, `news`, `bull`, `bear`, `risk_manager`, `head`(총괄). 프롬프트는 `prompts/<agent>.md`. 사이클당 LLM 호출 12회(mock 실행 기준, 후보 2종목).
- 사이클 흐름(로그 기준): 후보 선정 → 결정(BUY/SELL/HOLD + 확신도) → 게이트(`gate=GO`, `binding=confidence_tier`) → 손절 기본값 2.0×ATR14(1h), 익절 = min(2.0×손절, 4%) → 비용 0.30% 반영 순 R:R ≥ 1.5가 되도록 **손절만 조임** → 확신도 티어로 사이즈 상한(0.72 → 허용 비중의 40% = 4%) → 체결 → 체결가 기준 손절/익절 재고정 → 체결 직후 즉시 상태 저장.
- 하드 리밋(`risk.py` 상수, 메모리 기록): 종목당 10%, 포지션 3개, SL ≤ 2%, TP ≤ 4%, 일일 −3%, 연속 3패, 확신도 ≥ 0.65, 4사이클 쿨다운, STOP/STOP_ALL 킬스위치, 현물 시장가만. 조임 전용 오버레이: 수수료 반영 위험, 순 R:R 1.5(손절만 조임), 노이즈/과확장 필터, 주의(caution) 상태, 복수매매 차단 8사이클, 낙폭 사다리(−6% 자동 STOP), 시간손절 96사이클. 총괄의 SELL(조기 청산)도 확신도 0.65 규칙 적용; 코드 손절/익절/시간손절은 확신도와 무관하게 집행.
- 모드: `MODE=paper` 기본. live는 `MODE=live` + `CONFIRM_LIVE=YES` + 콘솔에 "LIVE" 입력. `bot.lock` 단일 인스턴스 잠금(PID, 죽은 PID면 자동 교체).
- 상태: `state.json` 원자적 저장(포지션·진입가·당일 손익·쿨다운·LLM 일시정지·최고자본). testnet/live에서 손상된 상태 파일이면 새로 시작하지 않고 멈춤(`CorruptStateError`); paper는 격리 후 새로 시작.
- 데이터 품질(`quality.py`): 오래된 봉·NaN·비정상 값이면 에이전트 호출 없이 건너뜀.
- 알림: Telegram(`notify.py`), 토큰은 로그/repr에서 마스킹(`mask_secret`, 2자만 노출).
- 보고서: `logs/cycles/…` 사이클 보고서 전문, `logs/decisions.jsonl`, 일일 보고서.
- 백테스트(`backtest.py`): 선행편향 차단, 비용·스트레스 테스트, 워크포워드 3구간, 몬테카를로, 게이트 판정. LLM 결정은 재현 못 함(규칙 전용 기준선).

## 4. 스킬 팩에서 채택한 것 (코드로 재구현, 문구 복사 없음)
| 출처 | 반영 위치 | 내용 |
|---|---|---|
| Position Sizer | `risk.py` | 거래당 고정 위험 0.15%(수수료 포함), 2×ATR 손절 제안, 포트폴리오 heat, 확신도 티어 |
| Backtest Expert | `backtest.py` | 선행편향 차단, 비용·스트레스, 워크포워드 3구간, 몬테카를로, 게이트 |
| Drawdown Circuit Breaker / Pre-trade Gate / trader | `risk.py`, `cycle.py` | 주의 상태, 복수매매 8사이클 차단, 낙폭 사다리(−6% 자동 STOP), R:R ≥ 2, 물타기 금지 |
| Data Quality Checker | `quality.py` | 오래된 봉·NaN·비정상 값 건너뜀 |
| Market News / Breadth | `prompts/news.md`, scanner | 빈 피드 ≠ 조용한 시장, 레드플래그 키워드, 출처 등급, RISK_OFF 토큰 |

참조 팩(`vendor/`, 읽기 전용): tradermonty/claude-trading-skills(MIT), claudskills `trader`(MIT), 웹 팩 6개(MIT/Apache). 평가표 81개 스킬: `docs/SKILL_REVIEW.md`, 채택 계획: `docs/ADOPTION_PLAN.md`.

## 5. 검증 결과
| 항목 | 결과 |
|---|---|
| 실제 CLI paper 1사이클 | "Not logged in" 감지 → LLM 60분 일시정지 → HOLD, 크래시 없음 |
| mock 백엔드 paper 1사이클 | 호출 12회, BTC·ETH 각 400 USDT 매수, 체결가 기준 손절 재고정, 보고서 저장. 자본 9,998.28 |
| 백테스트 90일 15분봉(수수료 0.1%+슬리피지) | **NOT VALIDATED**: 순 −1.87%, 승률 31.2%, PF 0.71, 최대낙폭 2.28%, 3종목 모두 바이앤홀드(+26~39%)보다 나쁨. 파라미터 미조정 |
| 적대적 리뷰 8개 관점 | 확정 결함 34건 수정 |

## 6. 마지막 리뷰 수정 라운드(워크플로, 파일별 수정자 6개, 830 통과)
- `exchange.py`: BUY 체결 시 기초자산 수수료를 뺀 **순 수량** 기록(`_fee_breakdown`), fee/fees 이중 계산 제거(`_fee_entries` 단일 출처), 화이트리스트에서 빠진 보유 종목도 **매도는 가능**(매수만 화이트리스트), NaN/inf/음수 수량 거부(`_positive_finite`), 예외 체인에서 비밀 제거(`_scrub_exception`), `adjustForTimeDifference=True`, 보유 수량이 1% 이내로 부족하면 가용 수량만 매도(`SELL_DUST_TOLERANCE`), 1% 넘게 부족하면 그대로 보내 거래소가 거부하게 둠(경고 로그).
- `portfolio.py`: 손상 상태 정책(모드별), `os.replace` 재시도 5회, 부분 체결은 체결분만 `ClosedTrade`로 기록하고 잔여분은 포지션 유지(`PARTIAL_EXIT_DUST_PCT=0.5`), 일 전환 처리.
- `claude_cli.py`: `--system-prompt-file` 사용(바이너리가 지원하면), 프로세스 트리 종료.
- `tests/test_review_*.py`: 회귀 테스트 추가(exchange 57개 등).
- `tests/test_notify.py`: `mask_secret` 2자 마스킹에 맞게 단언 완화.

## 7. 미해결·사용자 조치 필요 (세션 종료 시점)
1. `claude auth login` 미실행 상태(모든 에이전트 HOLD). → API 전환 시 불필요해짐.
2. 테스트넷 키(`BINANCE_TESTNET_API_KEY/_SECRET`, https://testnet.binance.vision) 미입력 → 테스트넷 주문 경로·OCO(`EXCHANGE_OCO`, 기본 꺼짐) 실거래소 미검증.
3. **주의**: `.env`에 `MODE=live`, `CONFIRM_LIVE=YES`와 실계정 키가 들어 있음(−2015로 거부됨, IP 제한 추정). 평소 `MODE=paper` 권장. 데모 실행은 `MODE=paper AGENT_BACKEND=mock TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID=`로.
4. 실자금 매매는 수행하지 않는다는 원칙(이전 세션에서 요청을 거절한 기록 있음).

## 8. 이번 세션에서 바뀌는 전제
- 사용자 요청: LLM 호출을 `claude -p` 서브프로세스에서 **Claude API(Anthropic SDK)**로 변경.
- 추가 요청: FinceptTerminal·Vibe-Trading 같은 터미널/스킬 기능 추가, 모든 과정 git 커밋.
