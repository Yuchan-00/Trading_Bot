# Vibe-Trading 스킬 전수 조사 (90개) — 암호화폐 현물 봇 관련성 평가

출처: https://github.com/HKUDS/Vibe-Trading (MIT). 조사일 2026-10-28. 관련성 high/medium은 역할 배정 후보, low/none은 제외.

| 스킬 | 관련성 | 제안 역할 | 목적 |
|---|---|---|---|
| `behavioral-finance` | high | bull / bear debate (with risk_manager for the debiasing checklist) | Turn behavioral-finance theory (under/overreaction, momentum vs reversal, disposition effect, herding, sentiment cycles) into quantifiable signals, a composite sentiment score that modulates exposure, contrarian extreme- |
| `candlestick` | high | technical | Detect 15 classic candlestick patterns (5 single, 6 double, 4 triple) with a pure pandas vectorized engine, score bullish +1 / bearish -1, and emit a composite long/short/flat signal from the sign of the total score. |
| `ccxt` | high | scanner | Reference card for the CCXT library: how to fetch public OHLCV, tickers, order books and trades from 100+ exchanges without an API key, plus timeframe/symbol conventions and pagination via `since`. |
| `correlation-regime` | high | risk_manager | Temporal correlation-regime machinery: edge-density of pairwise correlations + hysteresis state machine to flag a FUSED market, de-grossing risk context, first-mover crisis attribution with NAME/MACRO/AMBIGUOUS/ABSTAIN v |
| `event-driven` | high | news_sentiment | Defines an event-driven strategy where the LLM scores news/announcements from -1.0 to +1.0 with a fixed prompt, events are stored in a CSV (date,event_type,score,source,summary), an exponential time-decay aggregates them |
| `execution-model` | high | risk_manager | Backtest-only execution realism: fixed-bps, linear and square-root market-impact slippage models (implemented in src/quantlib/impact.py), VWAP/TWAP principles, execution delay to avoid look-ahead, all-in transaction-cost |
| `ichimoku` | high | technical | Defines a pure-pandas Ichimoku Kinko Hyo signal engine: compute the five lines, trigger only on Tenkan/Kijun crossovers, and require price-vs-cloud and cloud-direction agreement before emitting a long/short signal. |
| `liquidation-heatmap` | high | derivatives_flow | Teaches the agent to estimate where leveraged crypto positions get liquidated, read liquidation clusters as price magnets and post-wipe support/resistance, score cascade risk from cluster spacing, interpret 24h liquidati |
| `market-microstructure` | high | risk_manager | Explains price-formation micro-mechanics: spread measures, order-flow toxicity (VPIN, Kyle lambda), liquidity metrics (Amihud, Roll), price-impact models, limit-order-book depth/imbalance, flash-crash detection and China |
| `multi-factor` | high | scanner | Cross-sectional ranking: compute several factors per instrument on the same date (momentum, 5-day reversal, volatility, volume ratio, optional PE/PB/ROE), z-score them across the universe, sum with equal or custom weight |
| `perp-funding-basis` | high | derivatives_flow | Interpret perpetual funding rates, spot-futures basis, and open-interest changes as crowd-positioning / sentiment signals and as carry-trade opportunities. Gives regime classifiers, threshold tables, price-vs-funding div |
| `risk-analysis` | high | risk_manager | Risk measurement methodology: historical/parametric VaR, CVaR, max-drawdown analysis, Monte Carlo GBM, EVT (GPD) tail fitting, historical and hypothetical stress scenarios, with strict sign conventions and a report templ |
| `sentiment-analysis` | high | news_sentiment | Market sentiment quantification (Chinese): Crypto Fear & Greed bands, Put-Call Ratio, A-share margin balance, northbound flows, social-media heat metrics, a weighted composite 0-100 score and a sentiment-to-position-size |
| `smc` | high | technical | Smart Money Concepts (ICT) signal engine using the smartmoneyconcepts library: detect swing highs/lows, Break of Structure (BOS), Change of Character (ChoCH) and Fair Value Gaps (FVG), then emit long/short/flat per bar. |
| `technical-basic` | high | technical | Pure-pandas composite technical signal engine: trend (EMA 12/26 + ADX 14), mean reversion (Bollinger 20/2 + RSI 14), volume-price (OBV vs its 20 MA) combined by three-dimension voting into long/short/flat. |
| `trade-journal` | high | journal | Instructs the agent to call an analyze_trade_journal tool on a broker CSV export and present a trading profile (hold time, frequency, win rate, P/L ratio, drawdown) plus four behavioral-bias diagnostics (disposition effe |
| `volatility` | high | technical | Describes a volatility mean-reversion strategy: compute annualized historical volatility, rank it as a rolling percentile, go long in low-vol regimes (expecting expansion) and exit/short in high-vol regimes (expecting co |
| `asset-allocation` | medium | risk_manager | Explain portfolio allocation theory (MPT, Black-Litterman, risk budgeting, all-weather) and how to configure the project's five optimizers (equal_volatility, risk_parity, mean_variance, max_diversification, turnover_awar |
| `backtest-diagnose` | medium | backtest | Diagnose a failed or underperforming backtest: inspect metrics/equity/trades artifacts and the signal engine code, classify the failure (runtime, logic, data), apply a minimal fix, rerun, and emit concrete action items;  |
| `chanlun` | medium | technical | Chan theory (缠论) price-structure recognition using the czsc library: raw bars -> inclusion removal -> fractals (分型) -> strokes (笔) -> pivots/central zones (中枢) -> 1st/2nd/3rd class buy/sell points; outputs long/short/fla |
| `correlation-analysis` | medium | risk_manager | Full correlation and cointegration toolkit: co-movement scanning, bivariate Pearson/Spearman/Kendall + beta/R2, sector clustering, regime-conditional realized correlation, Engle-Granger/Johansen tests, half-life, Kalman  |
| `crypto-derivatives` | medium | derivatives_flow | Instructs the agent to analyze crypto derivatives markets (perpetual funding rates, futures term structure/basis, Deribit options IV/skew/Greeks) and recommend carry-arbitrage, calendar-spread, and volatility strategies, |
| `elliott-wave` | medium | technical | Describes a pure-pandas Elliott Wave signal engine: detects swing points with a rolling-window zigzag, matches 5-wave impulse and ABC corrective structures, validates them against the three iron rules and Fibonacci ratio |
| `factor-research` | medium | backtest | Cross-sectional factor validation framework: compute factor and forward-return panels, run IC/IR and quantile backtests via a factor_analysis tool, interpret thresholds, then combine surviving factors by equal, IC-weight |
| `harmonic` | medium | technical | Describes a signal engine that detects XABCD harmonic patterns (Gartley/Bat/Butterfly/Crab) from swing points using Fibonacci ratio windows and emits long/short/flat at the PRZ (potential reversal zone), with stop and ta |
| `investor-lenses` | medium | bear | Packages 12 named-investor reasoning procedures (Graham, Buffett, Munger inversion, Fisher, Lynch, Marks cycle, Dalio, Chanos, Bogle, Duan, Feng Liu, Qiu Guolu) as ordered signal walks with hard disqualifiers, a stacking |
| `minute-analysis` | medium | technical | Shows how to pull 1m-4H candles from OKX/Tushare/yfinance, compute intraday indicators (cumulative VWAP, TWAP, volume distribution by hour) and configure the project's backtest engine for minute-interval runs. |
| `ml-strategy` | medium | backtest | Provides a complete sklearn pipeline (data validation, OHLCV feature engineering, label = sign of future N-bar return, walk-forward retraining with no look-ahead, probability-to-signal mapping) to produce a continuous di |
| `okx-market` | medium | derivatives_flow | Catalog of OKX V5 public REST endpoints (tickers, candles, trades, instruments, order book, funding rate and history, mark price, open interest, price limits, index tickers/candles) with instrument-ID conventions and Pyt |
| `performance-attribution` | medium | journal | Framework for explaining why a portfolio made or lost money: Brinson-Fachler allocation/selection/interaction decomposition with Carino multi-period linking (imported from src/quantlib/attribution.py), alpha/beta and Fam |
| `quant-statistics` | medium | backtest | Reference for statistical tests used in quant research, all wrapping src.quantlib.timeseries: ADF stationarity, Engle-Granger cointegration with hedge ratio and half-life, Granger causality, GARCH(1,1) volatility fitting |
| `report-generate` | medium | journal | Instructs the agent how to write sell-side-style Markdown research reports (summary / core views / main body / data appendix / risks / recommendation), with a rating system, number/table formatting standards, terminology |
| `research-discipline` | medium | bear | A 60-second self-bias checklist to run before any research task: leader-bias, English-bias, narrative-bias, confirmation-bias, recency-bias, each with a correction, plus a 4-step apply procedure. |
| `sector-rotation` | medium | scanner | A-share sector rotation framework (Chinese): Shenwan industry taxonomy, a 100-point 'prosperity' score (earnings growth, trend, PMI, policy, valuation percentile), price/earnings/flow momentum ranking, supply-chain lead/ |
| `shadow-account` | medium | journal | Mines the user's own uploaded trade journal (A-share/HK/US/crypto) to extract 3-5 plain-language 'profit pattern' rules, backtests those rules across markets, attributes the P&L gap between the 'shadow' and the real acco |
| `strategy-dev-manager` | medium | backtest | Five-phase lifecycle (INGEST paper -> EXTRACT factor/strategy -> IMPLEMENT SignalEngine + backtest -> EVALUATE against thresholds -> MONITOR decay with a state machine) for turning research papers into registered, monito |
| `strategy-generate` | medium | backtest | Workflow for writing config.json + a SignalEngine (code/signal_engine.py), running the built-in backtest tool, and evaluating/fixing results; covers instrument code normalization across markets (A-share, US, HK, TSX, OKX |
| `thesis-tracker` | medium | risk_manager | Buy-side post-purchase discipline for equities: on buy, write a 5-sentence thesis, 3-7 falsifiable assumptions, red lines and valuation anchors to reports/{company}-thesis.md; each quarter re-check assumptions, score the |
| `alpha-zoo` | low | backtest | Orient the agent in using the project's built-in cross-sectional factor libraries (Kakushadze 101, GTJA 191, Qlib 158, Fama-French/Carhart) via the alpha_zoo / alpha_bench / factor_analysis tools to list alphas and run I |
| `bottleneck-hunter` | low | bear (reverse-check questions) / head (hard-gate principle) | Given a structural super-trend (AI infra, energy transition...), decompose its physical supply chain to Layer 2/3 choke points, score each on six scarcity criteria, screen listed small/mid-cap companies on them, enforce  |
| `commodity-analysis` | low | none | Fundamental framework for crude oil, gold and copper: supply-demand balance, gold 4-factor pricing, copper as macro barometer, inventory cycle stages, futures term structure (contango/backwardation) and seasonality, comb |
| `corporate-events` | low | news_sentiment | A-share event-driven playbook: merger-arb spread math, major-shareholder buy/sell signals with expected 20-day excess returns, equity-incentive plan reading, private placement/exchangeable bond effects, ST/delisting avoi |
| `cross-market-strategy` | low | risk_manager | How to write a signal_engine.py for a backtest universe spanning several markets (A-shares + crypto, equity + forex): detect market by symbol pattern, apply per-market indicator parameters, and scale signals by inverse v |
| `data-routing` | low | none | A router that tells the agent which Vibe-Trading data source/tool to use for each market and data need (A-shares, US, HK, crypto, forex), which env keys are required, which providers throttle/ban, and a cross-source veri |
| `defi-yield` | low | none | Instructs the agent to compare DeFi yields (lending, LP, staking, restaking, liquidity mining), compute impermanent loss and net LP yield, assess sustainability (real vs emission-funded yield), and rank opportunities on  |
| `earnings-revision` | low | news_sentiment | Instructs the agent to generate equity signals from analyst estimate revisions, earnings surprises (SUE/PEAD), management guidance changes and guidance language, plus earnings-quality red flags, for US and HK stocks, wit |
| `edgar-sec-filings` | low | news_sentiment | Tells the agent how to read US SEC filings (10-K, 10-Q, 8-K, DEF 14A, Form 4, 13F) to extract financial health, MD&A tone shifts, risk-factor changes, insider and institutional signals, and roll them into a -12..+12 comp |
| `etf-analysis` | low | scanner | Chinese-language framework for analysing and selecting ETFs in the China A-share market: product taxonomy, tracking error, premium/discount, liquidity and fee metrics, a 0-100 scoring model, core-satellite and sector-rot |
| `fund-analysis` | low | journal | Chinese-language mutual-fund/ETF selection framework: performance metrics (annualized return, alpha, IR, Sharpe, Sortino, Calmar, max drawdown), Sharpe style-box regression and style-drift detection, a five-step fund scr |
| `fundamental-filter` | low | scanner | Screens stocks on PE/PB/ROE (and optionally revenue, net income, net assets from point-in-time statements) to build a value or growth universe; passing names get equal weight 1/N long, others 0. Covers A-shares via Tusha |
| `geopolitical-risk` | low | news_sentiment | Instructs the agent to quantify geopolitical risk (GPR index z-scores, six hotspot watchlists with proxy indicators), classify crisis phase (pre-crisis early warning, during-crisis volatility trading, post-crisis mean re |
| `global-macro` | low | news_sentiment | Tells the agent to build a macro dashboard across central-bank policy transmission, FX forecasting (PPP/UIP/BEER), geopolitical proxies, capital flows (northbound/EPFR), and the dollar cycle, then score each factor -2..+ |
| `macro-analysis` | low | news_sentiment | Guides the agent to collect growth/inflation/rates/FX indicators for China, US and Europe, place the economy on the Merrill Lynch investment clock (recovery/overheat/stagflation/recession), read Fed/PBOC/ECB policy langu |
| `onchain-analysis` | low | news_sentiment | Interprets blockchain data for crypto investing: network activity (active/new addresses), whale and exchange-flow signals, DeFi/TVL and stablecoin liquidity, and valuation metrics (MVRV, NVT Signal, SOPR, Puell), combine |
| `pair-trading` | low | scanner | Mean-reversion pair strategy on two correlated instruments (e.g. BTC/ETH): compute price ratio, rolling mean/std, Z-score, long A/short B when Z < -entry_z and the reverse when Z > +entry_z, flat when /Z/ < exit_z. Equal |
| `private-company-research` | low | bull | Six-lens parallel deep-research framework for valuing unlisted companies (business model, financial forensics, competition, risk/governance, tech/IP, alternative data), with mandatory cross-validation, confidence labels  |
| `regulatory-knowledge` | low | none | Reference knowledge base (Chinese) of market microstructure/regulatory rules per market (A-share price limits, T+1, margin; HK T+0/shorting; US PDT/circuit breakers/RegSHO; crypto regulation by jurisdiction; taxes) so ba |
| `research-goal` | low | none | Workflow for Vibe-Trading's research-goal runtime: attach a research-only objective with 3-5 acceptance criteria, log evidence per criterion with provenance (tool_call_id, run_id, artifact sha256), and mark the goal comp |
| `seasonal` | low | scanner | Calendar-effect strategy: emit +1/-1/0 signals from bullish/bearish month lists and optional weekday overlay (both must agree in combined mode). Includes a pandas SignalEngine example that fetches OKX BTC/ETH daily candl |
| `social-media-intelligence` | low | news_sentiment | Playbook for collecting Twitter/X, Telegram, Discord and Reddit data, scoring sentiment (VADER/FinBERT/LLM), building buzz and fear/greed indices, classifying authors (institutional/KOL/retail/bot), and testing social se |
| `stablecoin-flow` | low | derivatives_flow | Market-timing framework based on stablecoin liquidity: total USDT/USDC supply growth, mint/burn events, exchange stablecoin reserves, stablecoin dominance (contrarian), on-chain velocity and chain distribution, combined  |
| `strategy-discovery` | low | backtest | Read-only facade over the Alpha Zoo registry and SDM store answering 'what strategies exist and in what state', with per-regime backtest evidence rows (never curated tags), evidence-quality flags, a sizing-corrected cost |
| `token-unlock-treasury` | low | news_sentiment | Instructs the agent to analyze a token's vesting/unlock calendar, emission rate and treasury composition to forecast supply-side sell pressure, then emit a short/medium/long-term signal (avoid/reduce/hold/accumulate) per |
| `us-etf-flow` | low | derivatives_flow | Instructs the agent to read US ETF creation/redemption flows (SPY/QQQ/IWM, sector SPDRs, factor and bond ETFs) to infer institutional risk appetite, sector rotation and rate expectations, producing a composite positionin |
| `vnpy-export` | low | backtest | Instructs the agent to translate a Vibe-Trading backtest strategy (config.json + signal_engine.py) into a runnable vnpy CtaTemplate subclass, mapping pandas/ta-lib indicators to ArrayManager calls and signals to buy/sell |
| `web-reader` | low | news_sentiment | Instructs the agent to fetch any URL as clean Markdown via a read_url tool backed by the third-party Jina Reader (r.jina.ai), with privacy and caching caveats. |
| `adr-hshare` | none | none | Analyze cross-listing price gaps between Chinese A-shares, HK H-shares and US ADRs (AH premium, ADR premium), assess HFCAA delisting risk, and emit a premium-mean-reversion arbitrage signal with a preferred listing venue |
| `akshare` | none | none | Reference card for the AKShare Python library: how to fetch A-share, US, HK OHLCV, macro, forex and futures data without an API key, including symbol/date formats and Chinese-to-English column mapping. |
| `ashare-pre-st-filter` | none | none | Predict whether a China A-share company will be flagged ST/*ST (risk warning / delisting) next fiscal year by forecasting revenue, net profit, net assets and dividends against board-specific regulatory thresholds, plus t |
| `convertible-bond` | none | none | China A-share convertible bond analysis: conversion value / bond floor / option value three-way valuation, clause games (downward revision, forced redemption, put-back), the 'double-low' screen and a monthly rotation fra |
| `credit-analysis` | none | none | Fixed-income and credit toolkit: rating scales, Altman Z-score, Merton/KMV distance-to-default, scorecard WOE/IV, bond pricing/YTM/duration/convexity/DV01/KRD, yield-curve fitting (Nelson-Siegel/Svensson), credit spread  |
| `deep-company-series` | none | none | Guides writing and strictly revising an 8-part, ~120k-word long-form research series on a single listed company (moat, profit engine, hidden assets, financials, management, valuation), with a fact-check checklist against |
| `dividend-analysis` | none | none | Instructs the agent to evaluate dividend stocks: normalize the dividend, check earnings/FCF/CFO coverage, diagnose dividend-growth quality, balance-sheet flexibility, separate yield from total return, and run a yield-tra |
| `doc-reader` | none | none | Describes the read_document tool that extracts text from PDF, DOCX, XLSX, PPTX, images (OCR), CSV, JSON/YAML and source files into a unified JSON envelope, with OCR engine configuration and truncation rules. |
| `earnings-forecast` | none | none | (Chinese) Instructs the agent to build EPS forecasts top-down (GDP -> industry -> company) or bottom-up (volume x price, segments, stores x ARPU), compare against analyst consensus to compute SUE, trade post-earnings dri |
| `eastmoney` | none | none | (Chinese) Index page for Eastmoney's free no-auth data endpoints wrapped as Vibe-Trading tools: fund flow, dragon-tiger board, margin trading, block trades, shareholder count, lockup expiry, sector membership, broker rep |
| `financial-statement` | none | none | Chinese-language guide to reading the three financial statements: articulation checks between income statement, balance sheet and cash-flow statement, an earnings-quality scorecard (accruals vs cash), a 12-item accountin |
| `hedging-strategy` | none | none | Walks the agent through designing a hedge for an existing portfolio: beta hedge via index futures/ETFs, option overlays (protective put, collar, put spread, far-OTM tail puts, VIX calls), cross-asset hedges, three hedge- |
| `hk-connect-flow` | none | none | Analyzes Shanghai/Shenzhen-Hong Kong Stock Connect northbound and southbound daily flows, sector rotation, AH premium index and quota utilization, combining them into a -10..+10 cross-border composite signal for A-share/ |
| `management-deep-dive` | none | none | Equity fundamental research procedure that scores a company's management team on integrity (promise-vs-delivery tracking, crisis behavior, stakeholder treatment), ability (strategic foresight, execution, capital allocati |
| `mootdx` | none | none | Documents a retired China A-share data source (mootdx, TDX binary protocol over TCP): installation, frequency codes, symbol format, column renames and loader fallback behavior. |
| `options-advanced` | none | none | Advanced options trading: volatility-surface modeling (SABR / local vol), dynamic Greeks management and hedge-frequency rules, calendar spreads, long/short gamma volatility arbitrage, skew trades (risk reversal, butterfl |
| `options-payoff` | none | none | Methodology for option P&L analysis: catalogue of single/multi-leg option structures, Black-Scholes pricing and Greeks, implied-vol inversion, expiry/theoretical payoff curves, breakeven and max-profit/loss computation,  |
| `options-strategy` | none | none | Describes the project's synthetic options backtest engine: a SignalEngine.generate() interface emitting multi-leg open/close instructions, config.json fields for the options engine (risk-free rate, historical-vol IV sour |
| `pine-script` | none | none | Export a backtested Python strategy to TradingView Pine Script v6, TDX formula (Tongdaxin/THS/Eastmoney) and MQL5 in one combined artifacts/strategy.pine file, with Python-to-platform mapping tables, syntax rules and a q |
| `qveris` | none | none | How to use the paid QVeris data marketplace: search (free) -> inspect (free) -> execute (paid) workflow, how to choose a capability by success_rate and expected_cost, billing modes (free/paid with per-session credit budg |
| `sec-edgar` | none | none | Data-fetch skill for US SEC EDGAR: resolve ticker to CIK, list filings (10-K/10-Q/8-K/Form 4/13F) with document URLs, pull XBRL us-gaap metric series; documents the throttled client, contact User-Agent requirement and en |
| `tushare` | none | none | Data-source reference for the Tushare Pro API (China A-share, HK, US equities, funds, bonds, futures, options, macro, news). Tells the agent how to authenticate with a token and lists ~230 endpoints with links to per-end |
| `valuation-model` | none | none | Instructs the agent to value a listed company with DCF/DDM/SOTP and PE-band/PB-ROE/EV-EBITDA, run WACC-vs-growth sensitivity tables, cross-validate at least two methods, and screen a 10-item valuation-trap checklist befo |
| `yfinance` | none | none | Data-source reference for Yahoo Finance via the yfinance package and a project-internal Yahoo public-API client: OHLCV, company info, financial statements, dividends, holders, indices, FX, and options chains for US/HK/Ca |

## 상세 (high / medium)

### `behavioral-finance` — high → bull / bear debate (with risk_manager for the debiasing checklist)
- 목적: Turn behavioral-finance theory (under/overreaction, momentum vs reversal, disposition effect, herding, sentiment cycles) into quantifiable signals, a composite sentiment score that modulates exposure, contrarian extreme-fear/greed checklists, and a debiasing checklist for the decision maker.
- 필요 입력: Price returns at several horizons (5d, 20d, 60d, 250d), RSI(5), Volume / turnover relative to average, 60-day VWAP (as avg cost proxy), China A-share sentiment series (margin growth, closed-end fund discount, new accounts, limit-up counts) - not available for crypto
- 산출: Market sentiment diagnosis (score 0-100, phase), Behavioral-bias signals (overreaction, disposition effect, herding), Strategy recommendations (holding period, exposure, contrarian trigger), Debiasing checklist
- 핵심 휴리스틱:
  - Underreaction -> momentum on 1-12 month / clear-event horizons; overreaction -> reversal on < 1 week or > 12 month horizons
  - RSI(5) < 10 -> short-term rebound signal (5-10 bars)
  - capital_gain_overhang = (price - 60-bar VWAP) / VWAP; CGO > 0.2 -> watch for disposition-effect selling, CGO < -0.3 -> selling pressure may ease
  - Attention-weighted momentum: if abnormal volume > 3x average, cut the momentum holding period by 50%
  - Combine cross-sectional strength (top 20% ranked return) with time-series trend (price > MA60): both -> full signal, one -> half position
  - Sentiment score > 80 extreme greed -> cut exposure below 60%; < 20 extreme fear -> raise exposure; 40-60 neutral
  - Contrarian triggers need at least 3 of 5 checklist items, not one
  - Debiasing: loss aversion -> pre-set stop-loss executed mechanically; overconfidence -> cap trades per period; recency bias -> evaluate over >= 60 bars; confirmation bias -> force reading the opposing view
- 관련성 근거: Crypto is retail-driven and highly sentiment-sensitive; the momentum/reversal horizon logic, CGO via VWAP, abnormal-volume attention rule and the 3-of-5 contrarian checklist are computable from OHLCV and funding/long-short data. The debiasing checklist is a natural guardrail for the head and bull/bear debate.
- 적용 메모: Replace A-share sentiment inputs with crypto proxies: funding rate percentile, long/short ratio, open-interest change, 24h volume vs 20-day average, RSI(5) on 1h/4h, BTC dominance if cheap to compute; build a 0-100 composite sentiment from those. Scale horizons to 15m cycles (e.g. 20 bars of 1h for 'short-term', MA60 on 4h). Give the bear agent the overreaction/greed checklist and the bull agent the underreaction/fear checklist; give the head the debiasing questions as required JSON fields (anchored_to_entry?, ignored_bearish_news?, overtrading?). Exposure modulation must stay inside the code-enforced caps.
- 참조 파일: 없음 (약 2100 토큰)

### `candlestick` — high → technical
- 목적: Detect 15 classic candlestick patterns (5 single, 6 double, 4 triple) with a pure pandas vectorized engine, score bullish +1 / bearish -1, and emit a composite long/short/flat signal from the sign of the total score.
- 필요 입력: OHLC DataFrame per symbol (open/high/low/close, datetime index), Parameters body_pct (0.1) and shadow_ratio (2.0), Example fetches OKX candles; any ccxt OHLCV works
- 산출: Per-symbol signal Series: 1 long, -1 short, 0 stand aside, Per-pattern score columns (hammer, engulfing, morning_star, ...) that can be exposed as features
- 핵심 휴리스틱:
  - Doji: body / (high-low) < 0.1 (body_pct); neutral, contributes 0
  - Hammer (bullish): lower shadow >= 2.0 x body, upper shadow < body, body > 0; inverted hammer mirrors it
  - Shooting star = inverted hammer shape after an uptrend (close[-1] > close[-2]) -> bearish
  - Bullish engulfing: prev bearish, current bullish, close >= prev open and open <= prev close (bearish mirrored)
  - Piercing line: prev bearish, open < prev low, close > prev body midpoint; dark cloud cover mirrored (open > prev high, close < midpoint)
  - Morning star: day1 bearish, day2 body/range < 0.3 with gap down (high2 < low1), day3 bullish closing above day1 midpoint; evening star mirrored
  - Three white soldiers: 3 bullish closes each higher, each open inside the prior body; three black crows mirrored
  - Composite signal = sign(sum of pattern scores); 0 -> stand aside
- 관련성 근거: Pure OHLCV, asset-agnostic, ships with a working vectorized implementation (example_signal_engine.py already fetches BTC-USDT/ETH-USDT/SOL-USDT). Pattern scores are cheap deterministic features that can be computed in code and handed to the technical agent, saving LLM tokens and avoiding hallucinated pattern reads.
- 적용 메모: Rewrite the detector in our own code (MIT attribution if any logic is copied) as a pure-pandas module run on 15m/1h/4h candles; feed the per-pattern flags and composite score as a JSON block to the technical agent rather than asking the LLM to spot patterns. Note crypto trades 24/7 so true gaps (morning/evening star gap conditions) are rare on 15m bars - relax the gap condition or use a body-based star definition. Treat -1 as 'exit / no entry', not short (spot-only). Fully offline-testable with synthetic candles.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/candlestick/example_signal_engine.py (591-line SignalEngine with all 15 detectors, generate(data_map) entry, and an OKX candle fetcher demo) (약 500 토큰)

### `ccxt` — high → scanner
- 목적: Reference card for the CCXT library: how to fetch public OHLCV, tickers, order books and trades from 100+ exchanges without an API key, plus timeframe/symbol conventions and pagination via `since`.
- 필요 입력: pip install ccxt, exchange name (env CCXT_EXCHANGE, default binance), symbol in slash format BTC/USDT, timeframe string (1m..1M)
- 산출: OHLCV arrays [[ts,o,h,l,c,v],...], ticker dict {last,bid,ask,volume}, order book {bids,asks,timestamp}, recent trades list
- 핵심 휴리스틱:
  - `exchange = ccxt.binance({"enableRateLimit": True})` - always enable the built-in rate limiter
  - `fetch_ohlcv(symbol, timeframe, since, limit)` returns `[[ts, o, h, l, c, v], ...]` (ms timestamps)
  - `fetch_order_book(symbol, limit)` -> `{bids, asks, timestamp}`; `fetch_tickers(symbols)` for batch quotes
  - Timeframes: `1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d, 1w, 1M`; check `exchange.timeframes` before use
  - Symbol format is slash style `BTC/USDT`; convert hyphen `BTC-USDT` -> `BTC/USDT`
  - Long history: paginate with the `since` ms timestamp (loader caps at 200 pages)
- 관련성 근거: Directly describes the exact data layer the target bot already uses (ccxt public Binance endpoints). Pure plumbing, not an analysis skill.
- 적용 메모: Not an LLM-agent skill; use as the spec for the bot's data adapter / mock backend. Add Binance testnet (`exchange.set_sandbox_mode(True)`), `fetch_funding_rate`/`fetch_open_interest` via ccxt.binanceusdm for the derivatives_flow role, and cache candles per 15-min cycle to avoid rate limits. No attribution needed beyond ccxt itself.
- 참조 파일: backtest/loaders/ccxt_loader.py (project loader, not in skill dir) (약 450 토큰)

### `correlation-regime` — high → risk_manager
- 목적: Temporal correlation-regime machinery: edge-density of pairwise correlations + hysteresis state machine to flag a FUSED market, de-grossing risk context, first-mover crisis attribution with NAME/MACRO/AMBIGUOUS/ABSTAIN verdicts, and a correlation-rewiring leaderboard for slow bleed-outs. Explicitly not a trade-timing signal.
- 필요 입력: multi-asset return matrix (crypto 1m-1d bars from OHLCV), pandas, numpy only, calm/event masks (from mode 1) for leaderboard
- 산출: per-bar density, smoothed density, fused 0/1 + transition timestamps, reference gross exposure (1.0 defused / 0.5 fused), attribution verdict, named asset, watchlist, z-score matrix, rewiring_score ranking per asset
- 핵심 휴리스틱:
  - Edge density = fraction of asset pairs with rolling |rho| >= 0.5 (edge_threshold for raw-return correlations)
  - Hysteresis: enter FUSED when smoothed density >= 0.65, exit only when <= 0.45; anchor enter near 90-95th calm percentile, exit near calm median; keep a wide dead band
  - Smoothing must be trailing (causal); `rolling(center=True)` leaks up to half a window of future data
  - FUSED => 'de-gross, don't liquidate': halve gross exposure; going fully to cash destroyed results because regime onset lags the top
  - First-mover: intensity = rolling 3-bar sum of |returns|, robust z vs own trailing baseline (median/MAD, shifted by 1, 1.4826*MAD, guard MAD=0); alarm_z 8, watch_z 3; NAME only if lead >= 2 bars over the second crosser; >= 60% crossing within 1 bar -> MACRO; else AMBIGUOUS; no crossing -> ABSTAIN
  - corr_window 30-90 bars for daily data, hundreds for intraday; rewiring score = row mean of |delta rho| event vs calm, needs >= 40 bars each
  - Validated on crypto (17 symbols, 1-minute bars, COVID/LUNA/FTX/SVB...) but regime exits lost to a plain price stop
- 관련성 근거: Validated on crypto with only return data; the bot's universe of USDT pairs gives the return matrix for free. Gives a principled 'market is one bloc' flag and an abstention-style crisis detector that maps cleanly onto the bot's drawdown ladder, cooldowns and position caps. The skill itself warns it is risk context, not a buy signal, which matches the risk_manager role.
- 적용 메모: Compute in code each cycle on 15m or 1h returns of the watchlist (e.g. 10-20 USDT pairs, corr_window ~96-200 bars): edge density, fused state with hysteresis, and the first-mover z-scores. Pass {fused, density, verdict, named, watchlist} to the risk_manager/head prompt with rules: fused -> halve max position size and raise confidence floor (e.g. 0.75); MACRO/NAME alarm -> no new entries this cycle and tighten stops; a NAME'd symbol is blocked from entry and flagged for exit. Log regime transitions to the journal and Telegram. Keep thresholds as config, recalibrate on paper-mode history. All formulas are simple pandas; rewrite in own words, cite MIT source.
- 참조 파일: https://github.com/tarvyn-analytics/corrcalc-graphs-pipeline (external reference) (약 4500 토큰)

### `event-driven` — high → news_sentiment
- 목적: Defines an event-driven strategy where the LLM scores news/announcements from -1.0 to +1.0 with a fixed prompt, events are stored in a CSV (date,event_type,score,source,summary), an exponential time-decay aggregates them into an event signal, and that signal is blended with a technical signal (alpha = 0.6 technical / 0.4 event).
- 필요 입력: News/announcement text (read_url, or RSS/RSSHub feeds via the companion loader), Event CSV with knowable-date, event_type, score, source, summary, Technical signal series in [-1, 1] to blend with
- 산출: Per-event LLM sentiment score in [-1.0, 1.0] (single number, no explanation), Decayed event signal series in [-1, 1], Combined signal = alpha x tech + (1 - alpha) x event, clipped to [-1, 1], Example engine: equal-weight long names whose decayed event_score >= 0.2 (optionally top_n)
- 핵심 휴리스틱:
  - Scoring scale: '1.0 extremely bullish; 0.5 moderately bullish; 0.2 mildly bullish; 0.0 neutral; -0.2 mildly bearish; -0.5 moderately bearish; -1.0 extremely bearish (fraud, major violation, black swan). Output one number only.'
  - Decay: 'decayed = score x exp(-lambda x days_since); lambda = 0.1 decays to ~37% in ~10 days; sum multiple events and clip to [-1, 1]'
  - Ignore weak events: 'events with |score| < 0.2 are ignored'; lookback window 30 days
  - Combination: 'combined = 0.6 x tech_signal + 0.4 x event_signal, clipped; when no event exists the combined signal collapses to the technical signal'
  - Event type durations: earnings 1-5d, macro 5-20d, policy/regulatory 20-60d, sentiment 1-3d, insider 5-10d, technical_break 1-5d
  - Look-ahead rule: 'date must be the knowable date (publication, not occurrence); released after close -> next trading day; enforce event_date <= trade_date'
  - Dedup: 'the same event from multiple sources -> deduplicate by (date, event_type) or average the scores'
  - 'Event sparsity is normal; do not fabricate data just to fill gaps'; 'fix the prompt template and recalibrate regularly' against LLM scoring drift
- 관련성 근거: Directly matches the bot's RSS/news input and multi-agent design: a cheap haiku call produces a single bounded number per headline, decay and blending are deterministic Python, and the skill explicitly handles no-event days, dedup and look-ahead. Crypto news is 24/7 so the 'next trading day' rule simplifies to publish timestamp.
- 적용 메모: Adopt the scoring prompt almost verbatim but return JSON {score, event_type, summary, symbols[]} via structured outputs, one haiku call per batch of headlines (not per headline) to save cost. Keep the event store in SQLite/JSON instead of CSV. Switch decay to hours for 15-min cycles (e.g. lambda per hour so sentiment halves in ~6-12h; policy/regulatory events get a slower lambda). Use per-event-type lambdas rather than the single 0.1 default. Pass the decayed event_signal and the latest top-3 summaries to the bull/bear/head agents rather than hard-blending with 0.6/0.4; the head can still see the suggested combined number. Enforce |score| >= 0.2 filter and (symbol, event_type, 1h bucket) dedup. Mock backend: feed canned RSS items with fixed timestamps.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/event-driven/example_signal_engine.py (약 1700 토큰)

### `execution-model` — high → risk_manager
- 목적: Backtest-only execution realism: fixed-bps, linear and square-root market-impact slippage models (implemented in src/quantlib/impact.py), VWAP/TWAP principles, execution delay to avoid look-ahead, all-in transaction-cost assumptions by market, and a cost-drag sensitivity analysis template.
- 필요 입력: Fill price, direction (+1/-1), order size, average daily volume (ADV), daily volatility, Backtest config: commission, engine, interval, Strategy turnover statistics for annual cost estimation
- 산출: Adjusted fill prices from fixed_slippage / linear_impact / sqrt_impact, Delayed signal series via delayed_execution(raw_signal, delay_bars), Markdown 'Execution Cost Analysis' report: trade characteristics, per-trade and annualized cost table, gross vs net return, optimization suggestions, Slippage sensitivity table (return/Sharpe/MDD at 0/3/5/10/20 bps)
- 핵심 휴리스틱:
  - Crypto fixed slippage reference: 'BTC spot 2-5 bps; ETH spot 3-8 bps; small altcoins 10-50 bps (liquidity varies widely)'
  - Linear impact = impact_coeff x volume_traded / adv; crypto impact_coeff 0.05-0.15
  - Square-root impact = eta x sigma x sqrt(volume_traded / adv), eta ~0.5 (0.3-0.8 calibrated range), sigma = daily return volatility; 'strongest empirical support'
  - Model choice: 'capital < 0.5% of ADV -> fixed 5 bps; 0.5-5% -> linear; > 5% -> square-root (required)'
  - 'Order size should not exceed 5-10% of the day's traded volume, otherwise the impact model becomes invalid'; max_participation = 0.05
  - Volume filter: 'do not trade when volume < 0.3 x 20-bar rolling mean volume' (set signal to 0)
  - Crypto all-in cost: commission 0.02-0.1%, spread 0.01-0.05%, 'round-trip ~0.2%'; backtest commission = 0.001 (0.1% one-way) is a reasonable all-in estimate
  - Cost drag: 'annual cost = annual turnover x one-way cost; 12 turns x 0.1% = 1.2%/yr, which eats 24% of a 5% return'; 'better to overestimate transaction costs than underestimate'
- 관련성 근거: The bot uses market orders on Binance spot, so the slippage models, the crypto-specific bps table, the 0.3x rolling-volume liquidity gate and the participation cap map directly onto both the paper/mock fill simulator and the risk manager's pre-trade checks. Formulas are simple and need only OHLCV volume and the order book.
- 적용 메모: Implement deterministically in Python, no LLM: (1) paper/mock fill = mid or last price +- fixed bps (BTC/ETH 5, alts 15-30) or sqrt impact using 24h quote volume as ADV and 1d realized vol; (2) risk_manager code check: reject or shrink an order whose notional > 5% of the symbol's 24h volume / 96 (per-15-min share) or whose expected slippage + 2x fee exceeds a fraction of the 2% stop distance; (3) skip a symbol when current 15m volume < 0.3 x 20-bar mean; (4) backtest/journal uses 0.1% one-way all-in cost and reports a bps sensitivity table. Replace the A-share T+1 delay with delay_bars=1 at 15m (signal at bar close, fill at next bar open). Drop VWAP/TWAP, as market orders only.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/quantlib/impact.py (약 2400 토큰)

### `ichimoku` — high → technical
- 목적: Defines a pure-pandas Ichimoku Kinko Hyo signal engine: compute the five lines, trigger only on Tenkan/Kijun crossovers, and require price-vs-cloud and cloud-direction agreement before emitting a long/short signal.
- 필요 입력: OHLC dataframe with at least 78 candles per symbol (52 + 26 displacement), Parameters tenkan 9 / kijun 26 / senkou_b 52 / displacement 26
- 산출: Per-bar signal: 1 long, -1 short, 0 stand aside, Line values (tenkan, kijun, senkou A/B, chikou) for context
- 핵심 휴리스틱:
  - Tenkan = (9H+9L)/2; Kijun = (26H+26L)/2; Senkou A = (Tenkan+Kijun)/2 shifted +26; Senkou B = (52H+52L)/2 shifted +26; Chikou = close shifted -26
  - Strong buy = bullish TK cross AND price above cloud AND bullish cloud (A > B); strong sell = bearish TK cross AND price below cloud AND A < B; all other cases stand aside
  - Cross strength grading: cross above the cloud = strong, inside = medium, below = weak (bullish); mirrored for bearish
  - Triple filter: all three conditions (cross, price-cloud position, cloud direction) must agree or no signal
  - Warm-up: at least 78 candles before any signal is valid
  - Signals fire only on crossover events, not on every bar where the condition holds
- 관련성 근거: Needs only OHLC, is widely used on BTC/ETH, deterministic, and gives a trend-regime filter (above/below cloud) that cleanly gates long-only spot entries. The 'all three must agree' rule is a natural confidence contributor and cheap to compute every 15m.
- 적용 메모: Compute in Python per cycle on 15m (entry timing) and 4h (regime) candles; pass {tk_cross, price_vs_cloud, cloud_bullish, cross_strength} as booleans/enums into the Haiku technical agent's input rather than asking the LLM to compute it. Long-only: treat bearish strong signal as 'exit/avoid', not short. Suggest head requires 4h price above cloud for any new long. Rewrite formulas in our own code; cite MIT source for the triple-filter rule.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/ichimoku/references/信号系统.md, /home/user/hkuds/vibe-trading/agent/src/skills/ichimoku/references/五线计算.md, /home/user/hkuds/vibe-trading/agent/src/skills/ichimoku/example_signal_engine.py (약 400 토큰)

### `liquidation-heatmap` — high → derivatives_flow
- 목적: Teaches the agent to estimate where leveraged crypto positions get liquidated, read liquidation clusters as price magnets and post-wipe support/resistance, score cascade risk from cluster spacing, interpret 24h liquidation volume and long/short liquidation ratio, and output a liquidation analysis with trading bias.
- 필요 입력: Open interest (by price if available) and leverage distribution estimate, 24h liquidation totals, long vs short split, largest single liquidation (CoinGlass free tier / OKX API), Funding rate and OI change after events, Current price and recent price history
- 산출: 24h liquidation overview and market state (extreme/elevated/normal/calm), Key liquidation levels table with type, est. volume, distance %, priority, Strongest upside/downside magnet and asymmetry, Cascade risk up/down (high/moderate/low), Post-liquidation S/R levels, Trading implications: bias, risk note, key level
- 핵심 휴리스틱:
  - long_liq_price = entry * (1 - 1/leverage + maintenance_margin); short_liq_price = entry * (1 + 1/leverage - maintenance_margin); e.g. 10x long at 65,000 liquidates at ~58,825 (9.5% away)
  - Leverage distance table: 5x ~20%, 10x ~10%, 20x ~5%, 50x ~2%, 100x ~1%
  - Magnet signal: if nearest cluster above > 2x nearest cluster below => upward magnet; reverse => downward; else balanced
  - Cascade risk: clusters stacked with min gap < 2% of price = high, < 5% = moderate, else low
  - 24h liquidations: > $1B extreme (potential V-reversal), $500M-1B high vol, $200-500M elevated, $50-200M normal, < $50M calm (leverage building)
  - Long/short liquidation ratio > 2 = longs squeezed, < 0.5 = shorts squeezed; largest single > $10M = whale
  - After cascade: funding deeply negative + OI dropped 20%+ => contrarian long with tight risk; during cascade do not catch the knife, wait for OI to stabilize
  - Post-liquidation S/R: a level where > $100M was liquidated often becomes support (long wipe) or resistance (short wipe)
- 관련성 근거: Crypto-native and directly actionable for spot entries: Binance futures public endpoints give funding, OI, long/short ratio, and (optionally) recent forced-order stream, which is enough to approximate cluster levels from the leverage table and apply the cascade / washout rules. Spot price follows perp liquidation dynamics closely on 15m horizons.
- 적용 메모: No paid heatmap: approximate clusters in code by projecting liquidation prices from recent swing highs/lows at 10x/20x/50x using the formula, weighted by OI change; feed 'nearest est. long-liq cluster below (%)', 'cascade_risk', '24h liq total (if forced-order stream enabled)', funding, OI delta to the Sonnet derivatives_flow agent. Spot adaptation: 'reduce size near heavy long-liq zone' becomes 'do not enter if a dense cluster is within 2% below (stop would sit inside the cascade)'; 'contrarian long after washout' is the one bullish setup the agent may flag. Scale USD thresholds by symbol (BTC values given; use OI-relative % for alts). Liquidation data is estimated and shifting; mark as low-confidence when the forced-order stream is disabled.
- 참조 파일: 없음 (약 2200 토큰)

### `market-microstructure` — high → risk_manager
- 목적: Explains price-formation micro-mechanics: spread measures, order-flow toxicity (VPIN, Kyle lambda), liquidity metrics (Amihud, Roll), price-impact models, limit-order-book depth/imbalance, flash-crash detection and China A-share auction/block-trade quirks, producing a liquidity diagnosis, cost estimate and execution recommendation.
- 필요 입력: Order book snapshot (bid/ask levels with sizes), Recent trades / tick data (for VPIN, effective spread), OHLCV with volume (Amihud, Roll, impact models), Daily volatility and average daily volume, pandas, numpy, scipy
- 산출: Liquidity diagnosis block (ADV, Amihud, effective spread, Kyle lambda), Order-flow block (VPIN, order-book imbalance OIR, net large-order flow), Trading-cost estimate (impact + fees, total one-way cost), Execution suggestion (TWAP/VWAP/IS, window, slices, urgency)
- 핵심 휴리스틱:
  - Quoted spread = Ask - Bid; Effective spread = 2 x |trade price - mid|; widen to >5x normal -> pause orders
  - Depth asymmetry / OIR = (Bid_vol - Ask_vol) / (Bid_vol + Ask_vol); rolling 5-min OIR > 0.3 -> short-term bullish (accuracy ~55-60%), filter for spoofing/icebergs
  - VPIN = sum(|buy_vol - sell_vol|) / (n x V) over 50 volume buckets; <0.3 normal, 0.3-0.5 caution, >0.5 stop trading
  - Kyle lambda: regress dP = a + lambda x (buy_vol - sell_vol) over 5-min windows; large lambda = poor liquidity
  - Amihud ILLIQ = mean(|R_d| / VOL_d) x 1e6; sudden rise -> liquidity drying up; exclude Amihud > 10
  - Power-law impact = eta x sigma_daily x (Q / ADV)^0.6 (eta ~0.5-1.5); square-root: sigma x sqrt(Q / (ADV x T)) for Q/ADV > 5%
  - Flash-crash signature: >5% drop in minutes, volume spikes then collapses, spread blows out, V-shaped rebound; use limit orders / specify max acceptable price
  - Do not execute large orders in the first 15 minutes after open or the last 5 before close (session-based; map to funding settlement / low-liquidity hours in crypto)
- 관련성 근거: The bot already has an order-book snapshot, 24h tickers and OHLCV with volume, which is enough for spread, depth asymmetry/OIR, Amihud and a rough impact estimate; these directly gate whether a market order is safe and give a pre-trade cost check. VPIN/Kyle lambda need trade-level data (optional via ccxt fetch_trades). A-share auction/block-trade sections are irrelevant.
- 적용 메모: Implement the formulas in code (not in the LLM prompt): compute spread_bps, top-5/top-20 depth in USDT, OIR, Amihud(1h bars), and estimated impact of the planned 10%-of-equity market order; pass these numbers to the risk_manager (sonnet) and let it veto when spread > N x 24h median, depth on the taker side < k x order notional, or impact estimate > 0.1-0.2%. Drop VPIN unless fetch_trades is wired; drop TWAP/VWAP/IS because orders are small market orders. Replace the session-time avoidance rule with 'avoid the minute around funding settlement (00/08/16 UTC) and a spread spike check'. Also useful for the scanner as a liquidity filter (minimum 24h quote volume, max spread).
- 참조 파일: 없음 (약 2600 토큰)

### `multi-factor` — high → scanner
- 목적: Cross-sectional ranking: compute several factors per instrument on the same date (momentum, 5-day reversal, volatility, volume ratio, optional PE/PB/ROE), z-score them across the universe, sum with equal or custom weights, and go long the TopN equal-weighted, rebalancing every N days. Also points to a ZooSignalEngine for composing 450+ library alphas.
- 필요 입력: OHLCV DataFrames for >= 3 instruments (dict code -> DataFrame), Optional extra_fields (PE/PB/ROE for A-shares), pandas, numpy; src.factors.registry for the zoo engine
- 산출: Per-symbol signal Series: 1/N if in TopN else 0, ZooSignalEngine signal panel (long-only / short-only / long-short)
- 핵심 휴리스틱:
  - Factors: momentum = return over past N (20) bars, positive; reversal = return over past 5 bars, negative; volatility = std of returns over N bars, negative; volume_ratio = today's volume / N-day avg, positive
  - Align factor directions before z-scoring (flip sign of negative factors), then Z = (x - mean) / std across the cross-section
  - Cross-sectional standardization needs at least 3 instruments, else z-scores are meaningless
  - Composite = equal-weight (or custom-weight) sum of z-scores; pick TopN (default 3), weight 1/N
  - Keep the previous selection unchanged between rebalance dates (default every 20 bars) - do not rerank every bar
  - Guard: if std < 1e-12 or fewer than 2 valid values, set all z-scores to 0
- 관련성 근거: This is the natural algorithm for the bot's scanner: rank the USDT universe on momentum / volume ratio / volatility / reversal from Binance 24h tickers and candles, take the top 3-5 candidates, and only spend LLM calls on those. TopN=3 even matches the max-3-positions limit. Equity-only factors (PE/PB/ROE) and the Alpha Zoo dependency are dropped.
- 적용 메모: Implement in code as a pre-LLM filter: for all USDT pairs passing liquidity thresholds (min 24h quote volume, max spread), compute 4h/1d momentum, 1h reversal, 24h realized volatility, volume ratio vs 20-period average, plus optional funding-rate/OI change as crypto factors; z-score, weight, and hand the TopN with their factor table to the LLM roles. Keep the 'do not rerank every cycle' rule as a cooldown on the candidate list (e.g. re-rank hourly, not every 15 minutes) to limit churn and API cost. Do not use long-short mode (spot only).
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/multi-factor/example_signal_engine.py, /home/user/hkuds/vibe-trading/agent/src/skills/multi-factor/zoo_signal_engine.py (약 800 토큰)

### `perp-funding-basis` — high → derivatives_flow
- 목적: Interpret perpetual funding rates, spot-futures basis, and open-interest changes as crowd-positioning / sentiment signals and as carry-trade opportunities. Gives regime classifiers, threshold tables, price-vs-funding divergence rules, an OI x funding matrix, data endpoints, and an output template.
- 필요 입력: Current and 7-day funding rate history (per-8h decimal) from Binance/OKX futures public API, Perp price vs spot price (premium), quarterly futures price for basis, Open interest and 24h OI change, Optionally cross-exchange funding rates
- 산출: Funding Rate & Basis Analysis report: current rates/regime, basis structure, 7-day funding history, OI x funding signal, carry opportunity, directional signal with divergence and confidence (high/medium/low)
- 핵심 휴리스틱:
  - annualized = funding_rate_8h * 3 * 365 (rate is a per-8h decimal, 0.0001 = 0.01%)
  - Funding > +0.05%/8h = extreme long crowding -> contrarian short / reduce longs; < -0.02%/8h = short-squeeze territory -> contrarian long
  - Regime: avg7d > 0.0003 and last 3 positive -> overheated_long; avg > 0.0001 and last 3 positive -> bullish_carry; avg < -0.0002 and last 3 negative -> overheated_short; else neutral
  - OI +5% in 24h and funding > +0.03% -> leveraged_long_buildup (squeeze risk); OI -5% and funding > 0 -> long_liquidation; |OI change| < 2% and |funding| < 0.005% -> quiet_market
  - Divergence: price new highs + funding declining = bearish; price new lows + funding rising = bullish; consolidation + deeply negative funding = short squeeze imminent
  - Funding > +0.05% for 3+ consecutive periods -> ~70% chance of a 5-10% correction within 7 days; < -0.03% for 3+ periods -> ~65% chance of a 5-15% bounce
  - Annualized basis > 30% = euphoric leverage / top warning; backwardation (< 0%) = extreme pessimism, contrarian long
  - Extremely high funding is a cost for longs, not a bullish signal
- 관련성 근거: Funding, OI, premium index and long/short ratio are exactly the free Binance futures public data the bot already has. The threshold tables and OI x funding matrix convert directly into a crowding/sentiment input for long-only spot entries (avoid buying into overheated_long; favor entries during overheated_short with price support). Carry-trade and cross-exchange arbitrage sections are irrelevant (no futures trading).
- 적용 메모: Compute regime, annualized funding, OI change, premium and divergence flags in code; pass the labels to a haiku derivatives_flow agent that returns JSON {regime, crowding_risk, squeeze_direction, confidence}. Rewrite thresholds in our own words; treat the 70%/65% historical stats as the skill author's claims, not verified facts. Hard rule candidate for risk_manager: block new longs when regime == overheated_long or leveraged_long_buildup.
- 참조 파일: okx-market skill (referenced via load_skill, not in dir) (약 2200 토큰)

### `risk-analysis` — high → risk_manager
- 목적: Risk measurement methodology: historical/parametric VaR, CVaR, max-drawdown analysis, Monte Carlo GBM, EVT (GPD) tail fitting, historical and hypothetical stress scenarios, with strict sign conventions and a report template. Tells the agent to call src/quantlib/risk.py rather than retyping formulas.
- 필요 입력: Return series (daily or higher frequency) or equity/net-value series, Portfolio weights (optional benchmark, risk budget), Vibe-Trading module src/quantlib/risk.py (historical_var, parametric_var, historical_cvar, max_drawdown_analysis, monte_carlo_gbm, analyze_mc_results, fit_gpd_tail)
- 산출: Risk Analysis Report: core metrics table (vol, MDD with dates, VaR/CVaR 95% 1D, skew, kurtosis), stress-test table with 'stop triggered' column, Monte Carlo summary, risk-control recommendations
- 핵심 휴리스틱:
  - Loss is a positive number in every function; flip sign once at presentation layer only; cvar >= var must hold for same sample/confidence, else the tail mask is wrong
  - Historical VaR = element ceil((1-confidence)*n)-1 of ascending-sorted returns, negated; parametric VaR = -(mu + z*sigma), z = norm.ppf(1-confidence)
  - Do not assume parametric < historical: at 95% parametric usually reads higher; quote both at 99% to expose the tail
  - CVaR is typically 1.3-1.8x VaR; VaR is not the maximum loss
  - Stress test: portfolio_loss = sum(weight_i * shock_i); compare to risk budget and whether stop-loss thresholds are triggered; BTC reference shocks: 2020 COVID -50%, 2022 hiking -65%, liquidity dry-up -40%, rate shock -15%
  - EVT: tail is 'fat' only if xi > 2*shape_stderr; check xi stability across nearby thresholds before quoting
  - Tail metrics: kurtosis >3 fat tails; skew <0 left-skewed; tail ratio = worst5%/best5% > 1 means larger downside
  - Square-root-of-time horizon scaling only valid under i.i.d. returns; correlations go to 1 in crises; always pass a seed to Monte Carlo and use >=10,000 paths
- 관련성 근거: Market-agnostic and directly usable with OHLCV-derived returns and the bot's own equity curve. VaR/CVaR/MDD/kurtosis are the right language for the risk_manager and backtest roles, and the stress-shock idea maps onto the drawdown ladder. Only the quantlib import path and equity/bond scenario rows need replacing.
- 적용 메모: Implement the formulas in our own risk module (numpy/pandas, tests, loss-positive convention) and compute them in code, not in the LLM: per-symbol 1h/4h-return historical VaR95/99, CVaR95, realized vol, kurtosis, and the account equity MDD. Pass these numbers to the risk_manager (sonnet) with rules such as: if 1-bar CVaR95 * position_size would breach the 2% stop or the daily -3% halt, veto or shrink; size = min(10% cap, risk_budget / CVaR95). Drop Monte Carlo and EVT from the per-cycle path (too slow/noisy on 15-min data); keep them for the backtest/journal nightly report. Replace scenario table with crypto shocks: -10%/-20%/-40% flash crash over 1h/1d applied to open positions.
- 참조 파일: src/quantlib/risk.py (referenced module, not under the skill directory) (약 3900 토큰)

### `sentiment-analysis` — high → news_sentiment
- 목적: Market sentiment quantification (Chinese): Crypto Fear & Greed bands, Put-Call Ratio, A-share margin balance, northbound flows, social-media heat metrics, a weighted composite 0-100 score and a sentiment-to-position-size mapping; sentiment is treated as a contrarian indicator with a dashboard output format.
- 필요 입력: Crypto Fear & Greed index (alternative.me, free), Option put/call volume (Deribit for BTC), A-share margin balance and northbound flows (not applicable), Social media mention counts/polarity (Twitter/Reddit/Telegram)
- 산출: Sentiment dashboard table (indicator, value, percentile, signal), Composite sentiment score 0-100 with band label, Position suggestion and risk notes
- 핵심 휴리스틱:
  - Crypto F&G bands: 0-20 extreme fear = bottom zone (contrarian buy, in 3-5 tranches, only with fundamental support); 80-100 extreme greed = top zone (trim, do not short); 40-60 neutral = sentiment uninformative
  - Composite = 0.25*fear/greed + 0.20*PCR + 0.20*margin + 0.20*northbound + 0.15*social, each normalized 0-100
  - Position mapping: 0-20 -> 80-100% invested; 20-40 -> 60-80%; 40-60 -> 40-60%; 60-80 -> 20-40%; 80-100 -> 0-20%
  - BTC (Deribit) PCR normal 0.3-0.8; extreme <0.2 or >1.2; PCR high + vol high = capitulation (strong reversal long signal)
  - Heat index = search/discussion volume vs MA(30): spike = overheated; bullish post ratio >80% = extreme optimism; KOL unanimity = danger
  - Sentiment plus trend: greed in an uptrend is normal, fear in a downtrend is normal; extremes can persist, never short on sentiment alone
  - Social feeds are noisy (bots/marketing accounts); filter before counting
- 관련성 근거: The Crypto Fear & Greed section is written for crypto and the index is free. The contrarian banding, 'sentiment + trend' rule and heat-vs-baseline measure map directly onto RSS headline analysis and onto Binance public futures data (funding rate and long/short ratio play the role margin balance/PCR play here). PCR and A-share flow sections are unusable.
- 적용 메모: Rebuild the composite from free crypto inputs only: 0.35*Fear&Greed (alternative.me, cache 1h, mock in offline mode), 0.25*funding-rate z-score (positive extreme = greed), 0.20*long/short ratio percentile, 0.20*headline polarity from RSS scored by haiku (title-only, last 24h, dedup by hash). Compute the score in code; the news_sentiment agent (haiku) only labels headlines and flags event risk (hack, delisting, regulation, ETF) with a 'risk_event: true' field the head must respect. Replace the position mapping with a confidence modifier: composite >80 -> cap new longs / reduce confidence 0.1; <20 -> allow entries only with technical confirmation. Keep 'never act on sentiment alone' in the head prompt. Drop PCR, margin, northbound, social scraping.
- 참조 파일: 없음 (약 3200 토큰)

### `smc` — high → technical
- 목적: Smart Money Concepts (ICT) signal engine using the smartmoneyconcepts library: detect swing highs/lows, Break of Structure (BOS), Change of Character (ChoCH) and Fair Value Gaps (FVG), then emit long/short/flat per bar.
- 필요 입력: OHLCV DataFrame (open/high/low/close/volume, datetime index), pip extra: vibe-trading-ai[smc] (smartmoneyconcepts library), Parameters swing_length (10) and close_break (True)
- 산출: Per-symbol signal Series: 1 = long, -1 = short, 0 = stand aside, Example SignalEngine (example_signal_engine.py) that fetches OKX daily candles
- 핵심 휴리스틱:
  - Long = bullish ChoCH/BOS + bullish FVG present; Short = bearish ChoCH/BOS + bearish FVG; else stand aside
  - ChoCH has the highest weight (trend reversal), BOS is confirmation (continuation), FVG is a same-direction filter
  - structure = ChoCH where non-zero, else BOS; buy = structure==1 & FVG>=0; sell = structure==-1 & FVG<=0
  - Bullish BOS = close breaks the previous swing high in an uptrend; bearish ChoCH = close breaks the previous swing low in an uptrend
  - Bullish FVG: low of bar 3 > high of bar 1 (gap left in the middle); bearish FVG: high of bar 3 < low of bar 1; price tends to return to fill it
  - Bullish order block = the last bearish candle before a strong rally; bearish OB = last bullish candle before a strong drop; expect a reaction on retest
  - Need at least swing_length * 2 bars, otherwise skip the symbol
  - Larger swing_length -> fewer but more reliable structures; close_break=True requires a closing-price break
- 관련성 근거: Pure price-structure logic, works on any Binance OHLCV timeframe and is popular in crypto. Only long signals are usable for spot; the short side becomes 'exit/avoid'. The library dependency is optional since BOS/ChoCH/FVG are easy to compute in pandas.
- 적용 메모: Compute BOS/ChoCH/FVG/OB in-house on 15m and 1h/4h candles (no smartmoneyconcepts dependency, keeps the mock backend offline-testable) and pass the booleans plus nearest FVG/OB zones to the technical agent as structured context rather than asking the LLM to find them. Map -1 to 'no entry / consider exit of an open long'. Use FVG/OB zone edges as candidate stop-loss levels, clamped to the hard 2% cap.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/smc/example_signal_engine.py, /home/user/hkuds/vibe-trading/agent/src/skills/smc/references/结构突破.md (BOS/ChoCH definitions and weights), /home/user/hkuds/vibe-trading/agent/src/skills/smc/references/订单块与缺口.md (order block and FVG definitions) (약 350 토큰)

### `technical-basic` — high → technical
- 목적: Pure-pandas composite technical signal engine: trend (EMA 12/26 + ADX 14), mean reversion (Bollinger 20/2 + RSI 14), volume-price (OBV vs its 20 MA) combined by three-dimension voting into long/short/flat.
- 필요 입력: OHLCV DataFrame with datetime index, pandas, numpy (requests only for the demo fetch), Optional parameter overrides
- 산출: Per-symbol signal Series: 1 long, -1 short, 0 stand aside, Reference implementations of RSI, ADX chain, Bollinger, OBV (example_signal_engine.py)
- 핵심 휴리스틱:
  - Long = trend bullish + RSI not overbought + OBV rising; Short = trend bearish + RSI not oversold + OBV falling; mixed = stand aside
  - trend_bull = EMA12 > EMA26 and ADX14 > 25; trend_bear = EMA12 < EMA26 and ADX14 > 25
  - mr_oversold = close < lower BB(20,2) and RSI14 < 30; mr_overbought = close > upper BB and RSI14 > 70
  - buy = (trend_bull or mr_oversold) and OBV > OBV_MA20 and not mr_overbought; sell = (trend_bear or mr_overbought) and OBV < OBV_MA20 and not mr_oversold
  - RSI and ADX use Wilder smoothing ewm(alpha=1/period), not a rolling mean
  - ADX chain: +DM/-DM -> TR -> Wilder smooth -> +DI/-DI -> DX = 100*|+DI - -DI|/(+DI + -DI) -> ADX
  - OBV = (volume * sign(close.diff())).cumsum()
  - Volume ratio uses a 20-period volume MA
- 관련성 근거: Works directly on Binance OHLCV at any timeframe, no dependencies, deterministic and offline-testable. Gives the technical agent a consistent, pre-computed indicator pack and a baseline vote so the LLM reasons over numbers instead of recomputing them.
- 적용 메모: Compute all indicators in code (pandas) for 15m, 1h and 4h and hand the technical agent a compact JSON (ema_state, adx, rsi, bb_position, obv_trend, vote) plus the voting rule as the prompt's decision rubric; the haiku agent then outputs direction/confidence. Treat -1 as 'no entry / exit long' for spot. Consider multi-timeframe agreement (4h trend + 15m trigger) as a confidence booster. Unit-test the indicator math against known values.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/technical-basic/example_signal_engine.py (약 500 토큰)

### `trade-journal` — high → journal
- 목적: Instructs the agent to call an analyze_trade_journal tool on a broker CSV export and present a trading profile (hold time, frequency, win rate, P/L ratio, drawdown) plus four behavioral-bias diagnostics (disposition effect, overtrading, chasing, anchoring) with severity and evidence.
- 필요 입력: Trade fill CSV with datetime/symbol/side/quantity/price/amount/fee (generic format; also Tonghuashun/Eastmoney/Futu formats), The analyze_trade_journal tool (Python implementation elsewhere in the repo, not in this directory), Optional filter_expr: date range, symbol, market
- 산출: Single dense markdown report: total trades, roundtrips, avg holding days, trades/week, win rate, profit/loss ratio, cumulative PnL, max drawdown, top symbols, hourly distribution, one-sentence observation, Behavior diagnostics table: 4 biases with severity low/medium/high + numeric evidence + improvement suggestions
- 핵심 휴리스틱:
  - PnL via FIFO lot matching; only closed round-trips count toward win rate / P-L ratio; drawdown measured from zero realized PnL
  - Disposition effect = avg_loser_hold / avg_winner_hold: medium >= 1.2, high >= 1.5
  - Overtrading = (quiet-day avg PnL - busy-day avg PnL) / |quiet| where busy = >=3 trades/day, quiet = <=1: medium >= 0.3, high >= 1.0
  - Chasing = fraction of buys placed after a 3-trade rolling +3% run-up in the same symbol: medium >= 40%, high >= 60%
  - Anchoring = fraction of symbols (>=5 trades) whose trade prices have CV < 5%: medium >= 33%, high >= 66%
  - If win_rate < 0.4 AND profit_loss_ratio < 1.0 -> explicit warning: losing on both dimensions, recommend cooling-off
  - If avg_holding_days < 1 AND trade_frequency_per_week > 15 -> flag intraday-heavy pattern
  - Fixes: disposition high -> hard stop-loss; overtrading high -> hard cap on trades/day; chasing high -> buy pullbacks not breakouts, 'no chase above +X% intraday' rule
- 관련성 근거: Market-agnostic. The bot already keeps its own trade journal (fills, P&L), so the four bias metrics can be computed in pure Python from local state with no external data, and surfaced to the journal/risk roles and the dashboard.
- 적용 메모: Skip the CSV parser and the analyze_trade_journal tool; implement the 4 metrics directly over the bot's journal table (per 15-min cycle data, use hours instead of days for hold time, and 'busy cycle' instead of 'busy day'). Run it as code, not an LLM call, once per day or on demand; pass the resulting severities into the risk_manager prompt (e.g. chasing=high -> raise required confidence, overtrading=high -> extend cooldown). Expose on the dashboard. Rewrite report template in English/Korean.
- 참조 파일: 없음 (약 1500 토큰)

### `volatility` — high → technical
- 목적: Describes a volatility mean-reversion strategy: compute annualized historical volatility, rank it as a rolling percentile, go long in low-vol regimes (expecting expansion) and exit/short in high-vol regimes (expecting contraction).
- 필요 입력: OHLCV close series (any timeframe; daily in the example), Parameters hv_window=20, lookback=120, low_pct=20, high_pct=80, annualize=252 (365 for crypto), pandas, numpy
- 산출: Signal series per symbol: 1 long (low-vol), -1 short/exit (high-vol), 0 stand aside; example_signal_engine.py provides a SignalEngine class and an OKX fetch demo
- 핵심 휴리스틱:
  - HV = returns.rolling(hv_window).std() * sqrt(annualize); crypto uses 365 (7x24 trading)
  - HV percentile = hv.rolling(lookback).rank(pct=True) * 100
  - Percentile < low_pct (20) -> long; > high_pct (80) -> exit/short; middle -> hold current position
  - Signal must be 0 until the lookback window is filled (fillna(0))
  - Volatility is not direction: low vol only means expansion is statistically more likely, not that price will rise
  - returns computed with pct_change(fill_method=None)
- 관련성 근거: Pure OHLCV, explicitly supports crypto (365 annualization, OKX example trivially swapped for ccxt/Binance). The HV percentile is a cheap regime feature for position sizing and for gating entries.
- 적용 메모: Compute in code, not by LLM: on 1h or 4h candles use hv_window=20 bars and lookback=120 bars (adjust annualize to sqrt(bars_per_year), e.g. 8760 for 1h). Since the bot is spot-only, map -1 to 'no new entries / tighten stop' instead of short. Feed hv_pct to the technical agent and to the risk_manager: e.g. hv_pct > 80 -> reduce position size or skip; hv_pct < 20 -> breakout-watch flag. Also use ATR-based HV to sanity-check that the 2% stop is not inside normal noise.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/volatility/example_signal_engine.py (약 450 토큰)

### `asset-allocation` — medium → risk_manager
- 목적: Explain portfolio allocation theory (MPT, Black-Litterman, risk budgeting, all-weather) and how to configure the project's five optimizers (equal_volatility, risk_parity, mean_variance, max_diversification, turnover_aware) plus rebalancing triggers and frequencies, outputting a weights plan written to config.json.
- 필요 입력: Return series for >= 3 instruments, Lookback window (default 60 bars), Optional return forecasts / views, Previous weights (for turnover-aware), Project config.json and signal_engine.py (for backtests)
- 산출: Allocation table (asset, weight, risk contribution, expected return), Optimizer JSON config block, Expected return / vol / Sharpe / max drawdown, Rebalancing rules (frequency, threshold, est. cost)
- 핵심 휴리스틱:
  - Inverse-volatility weight: w_i = (1/sigma_i) / sum_j(1/sigma_j)
  - Risk contribution: RC_i = w_i x (Sigma w)_i / sigma_p; risk parity sets all RC_i equal
  - Do not use raw mean-variance without bounds; lookback < 30 overfits, out-of-sample Sharpe often halves; 60 is a reasonable default (<20 noisy, >120 slow)
  - Optimizer decision tree: no forecasts + correlation matters -> risk_parity; large vol differences -> equal_volatility; costs matter -> turnover_aware (penalty gamma on ||w - w_prev||_1)
  - Crypto rebalance suggestion: weekly / biweekly with +-15% deviation threshold (high volatility)
  - Sum of weights <= 1.0, no leverage unless explicit
  - BTC correlation with traditional assets is unstable and tends to become positive in crises
  - At least 3 instruments for meaningful optimization; with 2, equal_volatility is enough
- 관련성 근거: The bot holds at most 3 spot positions capped at 10% each, so full optimizers are overkill, but inverse-volatility sizing (ATR/realized-vol scaled position size within the 10% cap), the correlation warning (BTC/ETH/alts often ~0.8+ correlated, so 3 positions may be one bet), and threshold-based rebalancing are directly useful for the risk manager.
- 적용 메모: Rewrite as a short sizing checklist: compute 20-60 candle realized vol per candidate, scale position size by inverse vol within the 10% symbol cap; compute pairwise 1h/4h return correlation of open positions and treat correlation > 0.8 as effectively one position (reduce new-entry size or refuse a 3rd correlated position); never exceed total exposure 30% (3 x 10%). Drop MPT/BL/all-weather, the config.json optimizers and the China examples. Must be computable offline from mock OHLCV.
- 참조 파일: config.json (project file, not in skill dir), signal_engine.py (project file) (약 2500 토큰)

### `backtest-diagnose` — medium → backtest
- 목적: Diagnose a failed or underperforming backtest: inspect metrics/equity/trades artifacts and the signal engine code, classify the failure (runtime, logic, data), apply a minimal fix, rerun, and emit concrete action items; also defines the hard-gate checklist that decides whether a run counts as evidence.
- 필요 입력: artifacts/metrics.csv, equity.csv, trades.csv, code/signal_engine.py and config.json, Backtest exit code and stderr, read_file / edit_file / bash tools
- 산출: Root-cause classification, Precise code edits and a rerun, Post-fix validation (AST parse, class SignalEngine, def generate), At least 2 action_items in the form 'Change X from A to B' / 'Add X logic in file'
- 핵심 휴리스틱:
  - Hard-gate checklist: metrics.csv non-empty, equity.csv non-empty, trade_count > 0, no NaN in equity, exit_code == 0; failing any gate yields no evidence rows
  - Zero trades = signal logic too strict; first trade > 2 years after start = lookback/dropna too aggressive; capital utilization < 50% = sizing bug; open position at end = missing forced liquidation
  - Do not touch code when the error is provider-side: 'no data available', 'rate limit', 'API limit', 'daily limit'
  - Fix one issue at a time, rerun immediately, max 3 repair iterations
  - Fix the bug only; do not change strategy logic unless asked
  - Action items must name parameter values, filenames and function names
- 관련성 근거: Asset-agnostic engineering discipline. The bot needs an offline backtest/replay harness over mock OHLCV, and this gate list (non-empty equity, trade_count > 0, no NaN, exit 0) plus the logic-bug taxonomy translate directly to validating that harness and to the journal agent's post-mortems.
- 적용 메모: Rewrite as a validation checklist for the replay/backtest module: run must produce metrics + equity + trades; trade_count > 0 over the window; no NaN in equity; no positions open at end unless intended; capital utilization reported; separate 'exchange/API error' from 'logic error' so cooldown/retry handles the former. Replace years-based 'late trade' rule with '> N cycles without a signal'. Keep the 'max 3 repair iterations' rule for any self-healing loop. Pure code/tests, not an LLM prompt.
- 참조 파일: strategy-discovery skill (referenced for manifest format), refresh_strategy_evidence tool / vibe-trading strategy-evidence refresh CLI (약 1100 토큰)

### `chanlun` — medium → technical
- 목적: Chan theory (缠论) price-structure recognition using the czsc library: raw bars -> inclusion removal -> fractals (分型) -> strokes (笔) -> pivots/central zones (中枢) -> 1st/2nd/3rd class buy/sell points; outputs long/short/flat signals and multi-stroke pattern classes.
- 필요 입력: OHLCV bars as czsc RawBar list (symbol,id,dt,freq,open,close,high,low,vol,amount) in chronological order, pip install czsc (v0.9.68+), pandas, at least ~30 bars before incremental updates
- 산출: signal 1 = long, -1 = short, 0 = wait per bar, completed stroke list (bi_list), detected pivots (ZS with zg/zd/zz/gg/dd), named signals e.g. 一买/一卖/三买/三卖, 3/5/7/9/11-stroke pattern labels
- 핵심 휴리스틱:
  - Pipeline: `原始K线 → 去包含处理 → 分型识别 → 笔检测 → 中枢构建 → 买卖点判定`
  - Pivot (中枢) = overlap of >=3 strokes: `zg = min(high of first 3 strokes)`, `zd = max(low of first 3 strokes)`, `zz = zd + (zg-zd)/2`, valid iff `zg > zd`
  - Trend rule: new pivot's `zd > previous zg` -> uptrend (中枢上移); new pivot's `zg < previous zd` -> downtrend
  - 1st buy (一买): downtrend with >=2 pivots, last leg's momentum weaker than the previous leg (divergence/背驰 via MACD area or price range) + bottom fractal confirmation
  - 3rd buy (三买, 'most reliable'): price breaks above zg, pullback low stays > zg, bottom fractal confirms; stop-loss just below zg. 3rd sell is the mirror with zd
  - Long if: 一买 OR 三笔向上盘背 OR 五笔类一买; short if the mirrors. Near pivot lower edge (zd) favor longs, near upper edge (zg) favor shorts
  - Engine detail: look back up to 10 strokes for the most recent valid ZS; signals evaluated on `di=1` (last completed stroke)
- 관련성 근거: Pure price-structure method that works on any OHLCV including BTC/USDT (the example even fetches BTC daily candles). Gives an objective support/resistance and trend-structure vocabulary (pivot zg/zd, 3rd-buy) useful for entries and stop placement, but it is a long-ish-horizon technique, docs are Chinese, and it needs the czsc dependency; spot-only bot can only act on buy points.
- 적용 메모: Do not ask the LLM to detect fractals; compute pivots/strokes in code (czsc or a lightweight own implementation of fractal->stroke->pivot) on 1h/4h candles and feed the technical agent a compact JSON: current ZS zg/zd/zz, whether price is above/inside/below, last signal type (1st/2nd/3rd buy). Use only long-side signals (spot); use 一卖/三卖 as exit/avoid flags. Use zg as a stop reference only if it keeps the stop within the 2% cap; otherwise skip the trade. Keep czsc optional behind a feature flag so the mock backend stays dependency-light.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/chanlun/example_signal_engine.py, /home/user/hkuds/vibe-trading/agent/src/skills/chanlun/references/核心概念/分型.md, /home/user/hkuds/vibe-trading/agent/src/skills/chanlun/references/核心概念/笔.md, /home/user/hkuds/vibe-trading/agent/src/skills/chanlun/references/核心概念/中枢.md, /home/user/hkuds/vibe-trading/agent/src/skills/chanlun/references/买卖点/一买一卖.md, /home/user/hkuds/vibe-trading/agent/src/skills/chanlun/references/买卖点/二买二卖.md, /home/user/hkuds/vibe-trading/agent/src/skills/chanlun/references/买卖点/三买三卖.md (약 1500 토큰)

### `correlation-analysis` — medium → risk_manager
- 목적: Full correlation and cointegration toolkit: co-movement scanning, bivariate Pearson/Spearman/Kendall + beta/R2, sector clustering, regime-conditional realized correlation, Engle-Granger/Johansen tests, half-life, Kalman hedge ratio, cross-market lead-lag, crisis correlation breakdown, and Z-score pair-trading signal generation with health monitoring.
- 필요 입력: daily (or intraday) return series for 2..N assets; price series (non-stationary) for cointegration, benchmark return series for regime labels (BTC for crypto), pandas, numpy, scipy, statsmodels (plus src.quantlib.timeseries helpers in the repo)
- 산출: correlation stats table, cointegration test table, half-life, hedge ratio, regime-conditional correlation table, pair signals: z_score, position (long/short spread/flat), health_score + action (hold/reduce/exit_now)
- 핵심 휴리스틱:
  - Screen: Pearson > 0.8 -> cointegration queue; 0.6-0.8 check sector/factor; < 0.6 unsuitable for pairs
  - Report Pearson, Spearman, Kendall; if Pearson and Spearman differ by > 0.1 the relation is nonlinear/heavy-tailed, weight Spearman
  - Regime labels: bull if benchmark return > rolling mean (N=365 bars for crypto), high_vol if rolling vol > 1.5x its annual mean; short-data fallback: cumulative return > +10% bull, < -10% bear
  - Typical regime correlation: bear 0.7-0.9, high-vol 0.8+ ('converges toward 1'), sideways 0.2-0.4; BTC-ETH 0.7-0.9 with < 1h lag; US equities -> BTC 0.1-0.4, US leads within 1 day
  - Half-life = -ln(2)/lambda from regressing dSpread on lagged spread; 5-20 days ideal for short-term pairs, > 180 near random walk
  - Pair signal thresholds: entry_z 2.0, exit_z 0.5, stop_z 3.5, lookback 2-3x half-life (conservative 2.5/0.3/3.0/90)
  - Spread health: ADF p > 0.10 -> -40, half-life > 2x original -> -30, ADF p > 0.20 -> -20; score >= 70 healthy/hold, >= 40 warning/reduce, else exit_now
  - Use prices for cointegration and returns for correlation; inner-join dates, never forward-fill; crisis stops tighter; apply Benjamini-Hochberg when testing many pairs
- 관련성 근거: Pair trading needs a short leg, impossible spot-only, and cointegration tests are overkill for 15-min cycles. But the regime-conditional correlation, BTC-beta, crisis correlation breakdown and 'everything correlates to 1 in a selloff' rules are directly usable with free Binance OHLCV for portfolio-level risk and for avoiding three near-identical positions.
- 적용 메모: Implement in code (not LLM): rolling 1h-return correlation of each candidate to BTC and to current open positions, beta to BTC, average pairwise correlation. Feed as JSON to risk_manager with rules: block a new position if |corr| to an existing position > 0.8 (max 3 positions must diversify); if BTC 4h-return regime is 'bear' or 'high_vol', require higher confidence or reduce size. Drop cointegration, Johansen, Kalman, plotting and A-share sections. Z-score mean-reversion of an alt vs BTC ratio could be a technical-agent feature (long only when alt/BTC z < -2 while BTC regime is bull).
- 참조 파일: src/quantlib/timeseries (adf_test, cointegration_test, find_hedge_ratio, compute_half_life - repo library, not in skill dir) (약 7500 토큰)

### `crypto-derivatives` — medium → derivatives_flow
- 목적: Instructs the agent to analyze crypto derivatives markets (perpetual funding rates, futures term structure/basis, Deribit options IV/skew/Greeks) and recommend carry-arbitrage, calendar-spread, and volatility strategies, producing a market snapshot table plus strategy suggestions and risk warnings.
- 필요 입력: Perpetual funding rate (8h) per symbol, Spot price, Quarterly/dated futures prices (for basis/term structure), ATM implied volatility and 25-delta risk reversal (Deribit options data), Option put/call ratio and option open interest, OKX / Deribit market data access
- 산출: Markdown 'Crypto-Derivatives Analysis' with Market Snapshot table (spot, funding 8h, annualized funding, quarterly basis, ATM IV, 25d RR), Strategy Suggestions table (strategy, direction, expected annualized return, risk level), Risk Warnings list
- 핵심 휴리스틱:
  - Funding rate > 0: longs pay shorts (bullish sentiment); < 0: shorts pay longs. Annualized = funding_8h x 3 x 365
  - Funding 8h > 0.1% (>109% ann.) = extreme greed -> short/contrarian signal (unsustainable); < -0.1% = extreme panic -> long signal
  - Funding 0.01-0.03% = normal bullish; -0.01~0.01% = neutral; 0.03-0.1% = bullish bias
  - basis = (futures - spot)/spot; annualized_basis = basis / days_to_expiry * 365; contango (far > near > spot) = bull/normal, backwardation = bear or post-crash
  - Bull market: contango annualized 15-40%; bear: backwardation or contango <5%; extreme crashes produce brief backwardation
  - Decision tree: high funding (>0.05%) + high IV (>80%) -> carry/short vol; backwardation -> reduce exposure / hedge
  - BTC IV regime: <40% extremely low, 60-80% normal, 80-120% elevated, >120% extreme
  - Risk params: leverage <= 3x, single-coin allocation < 30%, stop when floating loss exceeds expected 3-month return
- 관련성 근거: The bot does no derivatives trading, so the arbitrage/options strategy content is unusable; but the funding-rate sentiment table and basis/contango interpretation map directly onto Binance futures public data (funding rate, premium index, OI) and give a clean contrarian/regime signal for spot entries.
- 적용 메모: Keep only the funding-rate signal table (convert to Binance 8h funding from fapi /premiumIndex and /fundingRate), the basis formula applied to Binance quarterly futures or the perp premium index, and the 'extreme funding = contrarian' rule. Drop OKX/Deribit, options, carry arbitrage, calendar spreads, leverage params. Add open interest change and long/short ratio (Binance public) alongside funding. Output as JSON: {funding_regime, basis_regime, oi_trend, crowding_score, bias: long/short/neutral}. Haiku tier is fine; one call per candidate or batched per cycle.
- 참조 파일: 없음 (약 2600 토큰)

### `elliott-wave` — medium → technical
- 목적: Describes a pure-pandas Elliott Wave signal engine: detects swing points with a rolling-window zigzag, matches 5-wave impulse and ABC corrective structures, validates them against the three iron rules and Fibonacci ratios, and emits long/short/flat signals on the confirmation bar.
- 필요 입력: OHLCV DataFrame with high/low/close and datetime index (example fetches OKX daily candles for BTC/ETH/SOL), Parameters: swing_window (10), fib_tolerance (0.15), min_wave_bars (5)
- 산출: Per-symbol integer signal Series: 1 = long, -1 = short, 0 = stand aside, Signal stamped on the confirmation bar (swing_window bars after the swing), not the swing bar itself
- 핵심 휴리스틱:
  - Three iron rules: 'Wave 2 cannot retrace beyond the start of wave 1; Wave 3 cannot be the shortest impulse wave; Wave 4 cannot enter the price territory of wave 1'
  - Fibonacci checks used by the engine: wave2/wave1 in 0.5-0.618 (+-tol), wave3/wave1 in 1.0-2.618, wave4/wave3 in 0.236-0.5, wave5 ~= wave1; max wave-2 retrace 0.786 'beyond which it may not be wave 2'
  - ABC checks: 'B retraces 0.382-0.618 of A (zigzag) or 0.786-1.0 (flat); C in 0.618-1.618 x A, most often C = A'
  - Signal logic: '5-wave advance completed -> sell (trend top); ABC pullback completed -> buy (correction finished); Wave 3 in progress -> stay with the trend, no reversal signal'
  - Swing detection: a bar is a swing high if high == rolling(2*w+1, center=True).max(); zigzag keeps strictly alternating H/L, retaining the more extreme of consecutive same-type points
  - 'A swing point is only known swing_window bars after it forms, so the signal is stamped on that confirmation bar' (anti look-ahead)
  - Philosophy: 'simplest effective single interpretation... would rather miss signals than misclassify them'
  - Wave character notes: wave 3 is the strongest and usually has rising volume; wave 5 may show divergence; wave 4 is usually the shallowest pullback and alternates in form with wave 2
- 관련성 근거: Pure price-structure technique that works on any OHLCV series; the reference engine was even demoed on BTC/ETH/SOL-USDT. But wave counting is subjective, the engine is deliberately sparse (few signals), and on 15-minute cycles with swing_window=10 confirmation lag it mostly adds context rather than entries. Useful as an optional structural input to the technical agent, not as a standalone decision maker.
- 적용 메모: Implement the swing/zigzag + iron-rule checker as deterministic Python (no LLM cost) on 1h and 4h candles, and feed the result to the technical agent as a compact JSON field, e.g. {"wave_state": "impulse_complete|abc_complete|wave3_in_progress|none", "direction": 1|-1|0, "confirmed_at": ts}. Spot bot cannot short, so map -1 to 'avoid new longs / tighten TP' instead of a sell signal. Lower fib_tolerance or raise min_wave_bars on crypto's noisy intraday data; note _check_min_bars uses .days for datetime indexes, which must be changed to bar counts for intraday frames. Keep the 'prefer missing a signal to misclassifying' rule in the prompt.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/elliott-wave/example_signal_engine.py, /home/user/hkuds/vibe-trading/agent/src/skills/elliott-wave/references/Fibonacci浪间关系.md, /home/user/hkuds/vibe-trading/agent/src/skills/elliott-wave/references/波浪结构.md (약 480 토큰)

### `factor-research` — medium → backtest
- 목적: Cross-sectional factor validation framework: compute factor and forward-return panels, run IC/IR and quantile backtests via a factor_analysis tool, interpret thresholds, then combine surviving factors by equal, IC-weighted or orthogonalized weights (with walk-forward weight timing enforced by quantlib.combination and optional neutralization/PCA).
- 필요 입력: Factor CSV (index=date, columns=codes) and forward-return CSV with identical shape, factor_analysis tool (n_groups default 5); optional Alpha Zoo registry (450+ alphas), quantlib.combination.combine_factors and quantlib.neutralization for combination/cleaning
- 산출: ic_series.csv, ic_summary.json (IC mean, std, IR, share IC > 0), group_equity.csv, Verdict on factor validity and direction; composite factor panel with weights and diagnostics, Artifacts placed at <run_dir>/artifacts/factor/<factor_name>/
- 핵심 휴리스틱:
  - IC thresholds: '|IC mean| > 0.03 basic predictive power; > 0.05 strong; > 0.10 suspiciously high, check for look-ahead bias'
  - 'IR = IC mean / IC std; > 0.5 stably effective; > 1.0 extremely strong, very rare'; 'proportion of IC > 0 above 55% = stable direction; below 50% = unusable'
  - Quantile backtest: 'Group_1..Group_N final equity should be monotonic; large long-short spread = strong selection; V-shape means nonlinear; only tails differing means factor works only in extremes'
  - Look-ahead: 'factor at day T from data <= T; return from T close to T+1 close and beyond; never correlate with same-day return'
  - Standardize before IC: 'apply cross-sectional rank or Z-score; skewed factors (market cap, turnover) are otherwise dominated by outliers'
  - Combination: 'composite = sum(w_i x Z(factor_i)), w_i = |IC_i| / sum|IC_j|' or equal weight; orthogonalize (regress later factors on earlier, keep residual) when factors are highly correlated
  - 'A weight applied at row t may only use information from rows < t'; full-sample IC weights are 'the most common way to turn a real factor into a fake backtest'
  - Sample-size floor: 'each cross-section needs at least 5 valid instruments (and >= n_groups) for a meaningful IC'; watch factor IC decay over time (crowding)
- 관련성 근거: Not used in the live 15-minute decision loop and the Alpha Zoo/quantlib tooling will not be ported, but the IC/IR/quantile methodology is exactly how the offline backtest/journal module should validate the scanner's ranking features (momentum, volume surge, funding rate, OI change) across the USDT universe. Needs a universe of at least 5-10 symbols per cross-section, which Binance easily provides.
- 적용 메모: Write a small Python module (pandas only) that, given a wide panel of a candidate feature (e.g. 24h momentum, funding rate, OI delta, long/short ratio) and forward 15m/1h/4h returns over the top-30 USDT pairs, computes Spearman rank IC per bar, IC mean/std/IR, share IC > 0, and 5-quantile equity curves. Use the stated thresholds (0.03/0.05/0.5/55%) to decide which features the scanner and head prompts are allowed to cite. Enforce the factor-at-t / return-after-t rule and walk-forward weights. Run only offline against cached candles or the mock backend; no LLM involved. Skip neutralization/PCA and the Alpha Zoo.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/factor-research/references/combination.md, /home/user/hkuds/vibe-trading/agent/src/skills/factor-research/references/neutralization.md, /home/user/hkuds/vibe-trading/agent/src/quantlib/combination.py, /home/user/hkuds/vibe-trading/agent/src/quantlib/neutralization.py (약 2500 토큰)

### `harmonic` — medium → technical
- 목적: Describes a signal engine that detects XABCD harmonic patterns (Gartley/Bat/Butterfly/Crab) from swing points using Fibonacci ratio windows and emits long/short/flat at the PRZ (potential reversal zone), with stop and target rules in the reference notes.
- 필요 입력: OHLC dataframe per symbol (bundled detector uses swing_window to find pivots), Optional pyharmonics library (vibe-trading-ai[harmonic] extra)
- 산출: Per-bar signal series: 1 long, -1 short, 0 stand aside, stamped on the swing-confirmation bar, Pattern name and PRZ price band (in engine), stop/target levels per references
- 핵심 휴리스틱:
  - Gartley: B = 0.618 XA, D = 0.786 XA, BC = 0.382-0.886 AB, CD = 1.27-1.618 BC
  - Bat: B = 0.382-0.5 XA, D = 0.886 XA, CD = 1.618-2.618 BC
  - Butterfly: B = 0.786 XA, D = 1.27 XA (only pattern where D exceeds X)
  - Crab: B = 0.382-0.618 XA, D = 1.618 XA, CD = 2.24-3.618 BC
  - Engine tolerance windows: Gartley b_retrace (0.55,0.68) d_retrace (0.72,0.84); Bat (0.33,0.55)/(0.82,0.94); Butterfly (0.72,0.84)/(1.20,1.38); Crab (0.33,0.68)/(1.52,1.72)
  - PRZ entry: wait for a reversal candle inside the PRZ; do not enter before D is confirmed
  - Stop: below D for bullish / above D for bearish, typically 1-1.5x the PRZ width; targets: 0.382 AD, 0.618 AD, then point A
  - A swing point is only known swing_window bars after it forms, so the signal is stamped on the confirmation bar, not the swing bar (no look-ahead)
- 관련성 근거: Pure OHLC-based, works on Binance candles, and gives explicit stop/target geometry that maps onto our 2%/4% caps. But patterns are rare on 15m cycles, detection is parameter-sensitive, and the Crab/Butterfly stops often exceed 2%, so it is a secondary confirmation input rather than a primary strategy.
- 적용 메모: Implement the ratio windows as deterministic Python (no LLM) on 1h/4h candles, output {pattern, direction, prz_low, prz_high, stop, t1, t2} into the feature pack the technical agent reads. Only surface bullish patterns (spot, long-only). Reject any pattern whose implied stop (D - 1.5x PRZ width) is more than 2% below entry, since the code hard-limit would truncate it anyway. Keep the no-look-ahead confirmation rule for the backtest role. Rewrite in our own words; keep only the Fibonacci tables with MIT attribution.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/harmonic/references/PRZ交易.md, /home/user/hkuds/vibe-trading/agent/src/skills/harmonic/references/XABCD形态.md, /home/user/hkuds/vibe-trading/agent/src/skills/harmonic/example_signal_engine.py (약 400 토큰)

### `investor-lenses` — medium → bear
- 목적: Packages 12 named-investor reasoning procedures (Graham, Buffett, Munger inversion, Fisher, Lynch, Marks cycle, Dalio, Chanos, Bogle, Duan, Feng Liu, Qiu Guolu) as ordered signal walks with hard disqualifiers, a stacking protocol for 2-3 lenses that can disagree, and a binding 8-part output contract.
- 필요 입력: Pre-gathered evidence (financials, prices, disclosures) frozen before lens selection, For Marks lens in crypto: funding rates, perp open interest, liquidation data, Reasoning only for Munger inversion; no data feed required
- 산출: Lens name and what it optimizes for, Framework-conditional verdict, Ordered signal walk with pass/fail/missing, Disqualifier check (triggered / not / unknown), Misuse-regime check, 2-3 concrete falsifiers, Confidence with limiting factor, Standing disclaimer
- 핵심 휴리스틱:
  - Freeze the evidence before choosing the lens; evidence gathered after choosing is circular
  - Hit a hard disqualifier -> stop and report the veto; disqualifiers are not weighted against positives
  - Stack two or three lenses, never more; choose lenses that can actually disagree; run each independently before comparing
  - Never average the verdicts; classify every disagreement as Horizon, Caliber, Risk preference, or Information, and report the losing lens's strongest point verbatim
  - Munger inversion: enumerate at least five death paths from different families, each with probability band, time-to-impact, and an observable trigger; 'unknown is not low'
  - Munger veto: a failure path with permanent total loss and no observable trigger -> decline
  - Marks: take the temperature with observables (leverage, flows, issuance), place on pendulum euphoria->optimism->neutral->pessimism->capitulation, output posture (more aggressive/neutral/defensive), never a level or date
  - Marks crypto proxies named explicitly: funding rates, perpetual open interest, leverage liquidations; 'prefer what people did with money over what they said'
- 관련성 근거: Ten of twelve lenses are equity-fundamental and useless without financials. But the skill-level protocol (freeze evidence, hard vetoes, independent runs, classify disagreement, never average) is exactly the bull/bear/head debate structure we want, and the Munger inversion and Marks cycle lenses are data-light and explicitly crypto-portable.
- 적용 메모: Use three pieces: (1) bear agent = Munger inversion rewritten for a 15m spot trade: list >=3 death paths (cascade/liquidity, news shock, regime flip, exchange incident), each with an observable trigger from our data, verdict survivable / survivable-if-monitored / decline. (2) Head agent adopts the stacking rules: read bull and bear independently, classify disagreement (horizon/caliber/risk/information), never average confidence, quote the losing side's strongest point in the journal. (3) derivatives_flow agent gets the Marks pendulum (euphoria..capitulation) scored from funding rate, OI change, long/short ratio, liquidations, output as posture. Drop the other ten lenses entirely; do not reference investor names in prompts.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/munger-inversion.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/marks-cycle-position.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/dalio-debt-cycle.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/graham-deep-value.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/buffett-quality-franchise.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/fisher-scuttlebutt.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/lynch-garp.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/chanos-forensic-short.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/bogle-cost-drag.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/duan-right-business.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/fengliu-weak-system.md, /home/user/hkuds/vibe-trading/agent/src/skills/investor-lenses/references/qiuguolu-three-questions.md (약 2000 토큰)

### `minute-analysis` — medium → technical
- 목적: Shows how to pull 1m-4H candles from OKX/Tushare/yfinance, compute intraday indicators (cumulative VWAP, TWAP, volume distribution by hour) and configure the project's backtest engine for minute-interval runs.
- 필요 입력: Minute OHLCV candles (OKX /market/candles, 300 rows per request), config.json with source/codes/interval/initial_cash/commission for the backtest tool, pandas, numpy, requests
- 산출: DataFrame with vwap, twap, vol_pct, hourly volume columns, Minute-level backtest run via the backtest tool with the interval field set
- 핵심 휴리스틱:
  - VWAP = cumsum(typical_price x vol) / cumsum(vol), typical_price = (high+low+close)/3
  - TWAP = expanding mean of close
  - Volume distribution: vol_pct = vol / sum(vol) x 100; resample to 1h sums to find the active hours
  - Annualization factor = bars per year inferred from source + interval (crypto 5m = 365 x 288 = 105120)
  - Minute backtest length limits: <= 7 days for 1m, <= 30 days for 5m, <= 1 year for 1H
  - Set lower commission for intraday backtests (0.05% instead of 0.1%) since turnover is frequent
  - Timestamps are ms epoch; convert with unit='ms'
- 관련성 근거: Crypto-native and uses exactly the kind of data the bot has (ccxt candles 1m..1d). Content is thin, but the VWAP/volume-profile formulas and the backtest-window/annualization conventions are directly reusable for the technical agent's context and the backtest harness.
- 적용 메모: Replace OKX requests with ccxt fetch_ohlcv on Binance (1000-row limit, pagination by since). Compute session/rolling VWAP (e.g. anchored to UTC day) and 'price vs VWAP' + 'volume vs hour-of-day average' in code and feed them as fields to the technical (haiku) agent. Use the annualization rule (365 x bars/day) and the backtest length guidance in the backtest role; set commission to Binance taker 0.1% (or 0.075% with BNB) rather than 0.05%.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/minute-analysis/example_signal_engine.py (약 700 토큰)

### `ml-strategy` — medium → backtest
- 목적: Provides a complete sklearn pipeline (data validation, OHLCV feature engineering, label = sign of future N-bar return, walk-forward retraining with no look-ahead, probability-to-signal mapping) to produce a continuous directional signal in [-1, 1] for any OHLCV series.
- 필요 입력: OHLCV DataFrame with DatetimeIndex (>= 300 rows), scikit-learn, pandas, numpy
- 산출: Per-symbol signal Series in [-1.0, 1.0] (prob x 2 - 1), no NaN/inf, Optional discrete {-1, 0, 1} signal from predict()
- 핵심 휴리스틱:
  - Validate data first: required OHLCV columns, >= 300 rows, close NaN ratio <= 20%, else skip the symbol
  - Feature set (<15 features): ret_5, ret_20, vol_20 = rolling std of returns, ma_ratio = close / MA20, volume_ratio = vol / MA20(vol), RSI14 (guard loss=0), bb_position = (close - lower) / (upper - lower), (high-low)/close, (close-open)/open, rolling skew 20
  - Label: future N-bar return > 0 -> 1 else 0; a label at row t is usable only once t + horizon <= i (train_stop = i - horizon + 1)
  - Walk-forward: min_train_size 252, retrain every 20 bars, expanding or sliding (504) window; fit the StandardScaler on the training slice only
  - Skip retrain if < 50 valid rows or a single class; keep serving the previous model
  - Overfitting guards: max_depth 3-5, n_estimators 100, feature count < 15; use class_weight='balanced' when up/down ratio is skewed
  - Signal mapping: predict_proba[:,1] x 2 - 1; positive = bullish, |value| = confidence; clip to [-1, 1]
  - Features must use only data available at T-1 and earlier
- 관련성 근거: Works on any OHLCV, so it runs offline on Binance candles with no extra data, and its walk-forward discipline is exactly what a backtest role needs. But it is a code-side model, not an LLM reasoning procedure; its value is as an optional extra numeric input (ML probability) to the agents, and a 5-day equity horizon must be re-tuned to 15m-4h bars.
- 적용 메모: Do not have an LLM run this. Optionally implement a small ml_signal module: train walk-forward on 15m/1h Binance candles with horizon ~4-16 bars, retrain every ~100 bars, and expose ml_prob_up as one field in the technical agent's input and in the backtest harness as a baseline strategy. Reuse the feature list and the validation/leakage rules verbatim. If kept out of the first version, still borrow the data-validation checklist for the data layer and the mock backend tests.
- 참조 파일: 없음 (약 2200 토큰)

### `okx-market` — medium → derivatives_flow
- 목적: Catalog of OKX V5 public REST endpoints (tickers, candles, trades, instruments, order book, funding rate and history, mark price, open interest, price limits, index tickers/candles) with instrument-ID conventions and Python request examples.
- 필요 입력: HTTP access to https://www.okx.com/api/v5 (no auth), requests, pandas
- 산출: Raw JSON / DataFrames for ticker, candles, order book, funding rate, open interest, etc., Example scripts printing last price, 24h change, top tickers, candle DataFrames
- 핵심 휴리스틱:
  - 24h change = last / open24h x 100 - 100
  - Candle rows come newest-first; reverse before building a DataFrame; max 300 rows per request
  - Order book levels are [price, size, deprecated, order_count]; sz up to 400 levels
  - Funding rate fields: fundingRate (current), nextFundingRate (predicted), settFundingRate (last settled), fundingTime
  - Instrument naming: spot BTC-USDT, perp BTC-USDT-SWAP, index BTC-USD
  - Rate limits: funding-rate 20 req/2s, books 40 req/2s - cache snapshots per cycle
  - Check code == '0' on every response before reading data
- 관련성 근거: Crypto data source documentation, but for OKX rather than Binance. The bot already gets the equivalent fields (24h ticker, candles, order book, funding rate, OI) via ccxt/Binance public endpoints, so this is mainly a checklist of which fields to fetch, not something to adopt. Could serve as a secondary venue for cross-exchange sanity checks.
- 적용 메모: Use as a field checklist for the data layer: ticker (last, open24h, vol24h, bid/ask), candles, books (top N levels), funding-rate (current + predicted + last settled), open-interest, mark vs index price. Map each to the Binance equivalent (ccxt fetch_ticker, fetch_ohlcv, fetch_order_book, fetch_funding_rate, fapi openInterest). Optionally add OKX as a read-only cross-check of price divergence; otherwise ignore the OKX-specific scripts.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/scripts/market_data_example.py, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/scripts/candle_data_example.py, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/现货行情/单个行情.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/现货行情/批量行情.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/现货行情/K线数据.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/现货行情/最近成交.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/现货行情/交易产品列表.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/现货行情/深度数据.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/合约行情/资金费率.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/合约行情/历史资金费率.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/合约行情/标记价格.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/合约行情/持仓量.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/合约行情/限价.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/指数行情/指数行情.md, /home/user/hkuds/vibe-trading/agent/src/skills/okx-market/references/指数行情/指数K线.md (약 1000 토큰)

### `performance-attribution` — medium → journal
- 목적: Framework for explaining why a portfolio made or lost money: Brinson-Fachler allocation/selection/interaction decomposition with Carino multi-period linking (imported from src/quantlib/attribution.py), alpha/beta and Fama-French factor regression, Treynor-Mazuy / Henriksson-Merton market-timing tests, benchmark selection, risk-adjusted metrics grading, rolling analysis, and a report template.
- 필요 입력: Portfolio and benchmark sector weights and returns per period, Portfolio return series, benchmark (BTC for crypto) return series, risk-free rate, Factor return series for factor attribution (not available for crypto here), src/quantlib/attribution.py (brinson_fachler, carino_link)
- 산출: Performance Attribution Report: overview table (cum/annual return, vol, Sharpe, IR), attribution breakdown, factor exposure table, conclusions, Rolling Sharpe/alpha/beta analysis
- 핵심 휴리스틱:
  - Allocation_i = (w_p,i - w_b,i) x (r_b,i - R_b); Selection_i = w_b,i x (r_p,i - r_b,i); Interaction_i = (w_p,i - w_b,i) x (r_p,i - r_b,i); sum == R_p - R_b with no residual
  - Carino linking: k_t = (ln(1+R_p,t) - ln(1+R_b,t)) / (R_p,t - R_b,t); linked effect = sum_t (k_t / k) x effect_t
  - Alpha-beta: R_p = alpha + beta x R_m + eps via OLS with at least 60 data points
  - Treynor-Mazuy: gamma > 0 and significant on (R_m - R_f)^2 -> timing ability
  - Sharpe grading: >1.5 excellent, 1.0-1.5 good, 0.5-1.0 average; Sortino >2.0 excellent; Calmar >1.0 excellent
  - Timing hit rate > 55% = shows skill; bear capture ratio < 100% = better downside defense
  - Crypto benchmark = BTC-USDT; daily computation, monthly reporting
  - Multiple testing: test 100 strategies and ~5 look significant by chance; attribution explains the past, not the future
- 관련성 근거: No sectors or factor data, so Brinson and Fama-French do not apply. But the simplified pieces (alpha/beta vs BTC, Sharpe/Sortino/Calmar grading thresholds, bull/bear capture, timing hit rate, position-change vs future-return correlation) are a good spec for the journal's periodic self-review of the bot's own P&L.
- 적용 메모: Implement metrics in code (not LLM): per-trade and daily P&L vs a buy-and-hold BTC benchmark, Sharpe/Sortino/Calmar/max DD, hit rate, average win/loss, per-symbol and per-agent-signal contribution. Feed the computed table to a journal (sonnet or haiku) agent weekly to write a plain-language 'why we made/lost money' note; drop sector/factor sections entirely.
- 참조 파일: src/quantlib/attribution.py (project module), tests/quantlib/test_attribution.py (약 2600 토큰)

### `quant-statistics` — medium → backtest
- 목적: Reference for statistical tests used in quant research, all wrapping src.quantlib.timeseries: ADF stationarity, Engle-Granger cointegration with hedge ratio and half-life, Granger causality, GARCH(1,1) volatility fitting/forecasting, regression diagnostics (heteroskedasticity, autocorrelation, VIF), bootstrap confidence intervals (incl. Sharpe), hypothesis-test quick reference, multiple-testing correction, and Sharpe significance rules of thumb.
- 필요 입력: Price or return series (pandas), fitted statsmodels OLS results, Optional packages statsmodels>=0.14 and arch>=6.0, src.quantlib.timeseries functions
- 산출: Statistical Testing Report: stationarity table, cointegration table, GARCH parameters and long-run vol, bootstrap Sharpe/alpha CI with significance
- 핵심 휴리스틱:
  - ADF p < 0.05 stationary; 0.05-0.10 difference and retest; > 0.10 non-stationary. Prices are non-stationary, use log returns
  - Pair z-score: > 2.0 short spread, > 1.5 small short, < -1.5 small long, < -2.0 long spread, near 0 close; half-life longer than holding horizon means do not trade
  - GARCH(1,1): sigma^2_t = omega + alpha*eps^2_{t-1} + beta*sigma^2_{t-1}; long-run vol = sqrt(omega/(1-alpha-beta)); forecast decays toward long-run vol; BTC alpha ~0.05-0.20, beta ~0.75-0.90, long-run vol ~60-80% annualized
  - GARCH forecast accuracy declines rapidly beyond 5-10 days
  - Financial data is almost always heteroskedastic -> use HAC (Newey-West) standard errors by default
  - Sharpe t-stat = Sharpe x sqrt(n) / sqrt(1 + 0.5 x Sharpe^2); Sharpe > 2.0 in backtest = overfitting warning; realised Sharpe of a zero-edge strategy over 1000 daily bars has std ~0.50
  - Multiple testing: use Benjamini-Hochberg FDR (multipletests(p, method='fdr_bh')); predefine the testing plan, no p-hacking
  - Always pass a seed to bootstrap so reported intervals are reproducible; bootstrap_sharpe takes returns (fractions), not an equity curve
- 관련성 근거: Not for per-cycle LLM decisions, but valuable for the offline backtest/validation module: bootstrap Sharpe CI, Sharpe significance, overfitting warnings and multiple-testing correction guard against trusting a lucky paper run. GARCH/realized-vol forecast could size stops within the 2% cap. Cointegration is only relevant if the ratio feature from pair-trading is adopted.
- 적용 메모: Implement in Python (numpy/pandas, optional statsmodels/arch) with no LLM: bootstrap_sharpe(returns, seed) and a 'significance gate' before promoting a strategy config from paper to testnet/live; simple EWMA or GARCH vol forecast feeding ATR-style stop placement bounded by the 2%/4% hard caps. Rewrite thresholds in our own words; do not copy quantlib code.
- 참조 파일: src/quantlib/timeseries.py (project module), backtest/validation.py bootstrap_sharpe_ci (약 3000 토큰)

### `report-generate` — medium → journal
- 목적: Instructs the agent how to write sell-side-style Markdown research reports (summary / core views / main body / data appendix / risks / recommendation), with a rating system, number/table formatting standards, terminology guide, a backtest report template, and a PDF-export procedure.
- 필요 입력: Analysis results (backtest metrics.csv / equity.csv, fundamentals, risk-analysis output), Report type selection (stock / industry / strategy / backtest / flash note), write_file tool for PDF (Vibe-Trading specific)
- 산출: Complete Markdown report with rating, data tables, >=3 risk warnings, concrete action (direction, size, horizon), disclaimer, Backtest report (performance summary table, annual table, risk section, improvement suggestions, strategy rating)
- 핵심 휴리스틱:
  - Put the conclusion first: summary = 3-5 bullets, explicit bullish/bearish/neutral, never vague
  - Every view must have at least one supporting data point; chain 'because A (data) -> therefore B (judgment) -> action C'
  - Argue both sides: bullish reports discuss risks, bearish reports mention catalysts
  - Be actionable: recommendation specifies instrument, position size, time horizon
  - Use ranges not points ('target 25-30' not '27.5'); 'likely' over 'certain', 'may' over 'will'
  - Strategy rating thresholds: High priority = Sharpe>1.5 & MDD<20%; Allocatable = Sharpe>1.0 & MDD<30%; Watch = Sharpe 0.5-1.0; Not recommended = Sharpe<0.5 or MDD>40%
  - Only cite metrics from the engine-written metrics.csv/equity.csv of the run; a self-written metrics file is not evidence
  - Mark data cut-off date and generation date; AI reports must carry a not-investment-advice disclaimer; crypto-specific risks: regulation / exploits / liquidity / rug pulls
- 관련성 근거: Not needed for the 15-min trade decision loop (we use structured JSON, not prose). But the writing discipline (conclusion first, data-backed, both sides, ranges, concrete action, risk list) is directly useful for the journal agent's daily/weekly review and for the Telegram summary text, and the strategy-rating thresholds are a ready scoring rule for backtest results.
- 적용 메모: Strip equity/PDF/A-share content. Keep: (1) a compact 'daily review' schema for the journal role (summary bullets, what worked / what failed with numbers, >=3 risks, next-day adjustments) emitted as JSON then rendered to Markdown for Telegram/dashboard; (2) Sharpe/MDD rating thresholds for the backtest role to grade paper runs; (3) the 'cite only engine-produced metrics' rule to stop the LLM from fabricating P&L. Run on sonnet once per day, not per cycle.
- 참조 파일: 없음 (약 2800 토큰)

### `research-discipline` — medium → bear
- 목적: A 60-second self-bias checklist to run before any research task: leader-bias, English-bias, narrative-bias, confirmation-bias, recency-bias, each with a correction, plus a 4-step apply procedure.
- 필요 입력: The one-sentence thesis being researched, Web/search access (assumed) for broadening queries
- 산출: No artifact; an adjusted query plan and a conclusion that cites at least one disconfirming data point and checks data freshness
- 핵심 휴리스틱:
  - Write the thesis in one sentence, then ask for each bias: 'am I about to fall into this?'
  - Confirmation-bias: for every bull point, deliberately search the bear case; cite at least one disconfirming data point per conclusion
  - Recency-bias: for any material number check its date; prefer last 30 days; mark anything older than a year 'possibly stale'
  - Narrative-bias: ignore the concept label, look at the actual product/economics (an 'AI stock' may have no AI revenue)
  - Leader-bias: ask 'who is NOT in the top-10 that should be here?'
  - Post-research re-check: did I cite disconfirming evidence? is any key figure stale?
- 관련성 근거: Market-agnostic reasoning hygiene. Confirmation-bias and recency-bias corrections map directly onto a bull/bear debate and onto news-headline handling (stale headlines re-surfaced by RSS). Leader/English-bias items are about equity screening and do not apply.
- 적용 메모: Fold into prompts rather than a separate call: bear agent prompt gets 'for every bull point produce the disconfirming evidence from the provided data'; news_sentiment prompt gets 'check headline timestamp; ignore items older than 24h for intraday decisions, flag >7d as stale'; scanner prompt gets 'do not favor BTC/ETH just because they are large; rank by the metrics'. Add a 'disconfirming_evidence' required field to the bull/bear JSON schema. Costs zero extra LLM calls.
- 참조 파일: 없음 (약 700 토큰)

### `sector-rotation` — medium → scanner
- 목적: A-share sector rotation framework (Chinese): Shenwan industry taxonomy, a 100-point 'prosperity' score (earnings growth, trend, PMI, policy, valuation percentile), price/earnings/flow momentum ranking, supply-chain lead/lag transmission, and overweight/underweight output table.
- 필요 입력: Sector daily return matrix, Sector earnings (ROE, net profit growth), PMI/capacity data, policy news, Northbound flows, margin balance, ETF flows (A-share specific)
- 산출: Prosperity ranking Top 10 with score and change, Allocation table: overweight / neutral / underweight sectors with weight ranges and logic, Supply-chain opportunities and risk notes
- 핵심 휴리스틱:
  - Cross-sectional price momentum: cumulative return over lookback=60 bars skipping the most recent 5 (avoid short-term reversal), rank descending
  - Composite = 0.4*price momentum rank + 0.3*earnings momentum rank + 0.3*flow rank; overweight top 5, underweight bottom 5
  - Demand transmits bottom-up (downstream -> midstream -> upstream); cost transmits top-down; upstream leads midstream by 1-2 quarters
  - Passive destocking phase is the buy point in the inventory cycle
  - Short-term theme hype distorts momentum signals; distinguish theme vs real prosperity
  - Rotation should not be too frequent: monthly/quarterly rebalancing
  - Low PE on cyclicals can be the earnings top; use PB instead
- 관련성 근거: The industry taxonomy, earnings and flow inputs are A-share only. But the cross-sectional momentum-ranking skeleton (lookback minus skip, rank, take top N) transfers cleanly to ranking Binance USDT pairs or crypto 'sectors' (L1, L2, DeFi, meme, AI), and the 'theme hype vs real strength' warning is relevant for alt rotations.
- 적용 메모: Implement a code-side candidate ranker, no LLM: for a universe of ~30-50 liquid USDT pairs compute 4h/1d return over 24-72 bars skipping the last 2-4 bars, 24h quote volume rank, and relative strength vs BTC; composite = 0.5*momentum rank + 0.3*volume/liquidity rank + 0.2*relative-strength rank; hand the top 5-8 to the LLM scanner (haiku) with a sector tag map maintained in config. Keep 'do not chase a spike already >15% in 24h without volume confirmation' as a prompt rule. Use static sector tags (crypto has no Shenwan equivalent).
- 참조 파일: 없음 (약 2800 토큰)

### `shadow-account` — medium → journal
- 목적: Mines the user's own uploaded trade journal (A-share/HK/US/crypto) to extract 3-5 plain-language 'profit pattern' rules, backtests those rules across markets, attributes the P&L gap between the 'shadow' and the real account (noise trades, early exits, late exits, overtrading, missed signals), and renders an 8-section PDF report. Research only, never routes orders.
- 필요 입력: User trade journal (delivery statement) already processed by analyze_trade_journal, Tools: extract_shadow_strategy, run_shadow_backtest, render_shadow_report, scan_shadow_signals, Multi-market daily price history for backtests, Optional llm_translator callable for rule wording
- 산출: Rule cards: rule_id, human_text (<=30 chars), support_count, coverage_rate, holding_days_range, Backtest matrix: per-market Sharpe/annual return/max DD, combined equity curve, Signed delta attribution: noise_trades_pnl, early_exit_pnl, late_exit_pnl, overtrading_pnl, missed_signals_pnl, Counterfactual top-5 trades by |impact|, HTML + PDF report (HTML-only fallback), optional today's signal scan with disclaimer
- 핵심 휴리스틱:
  - Rules are 'the common profile of the user's winning round-trips', not 'guaranteed-profit formulas'
  - profitable roundtrips < 5 -> raise an error, never fabricate rules
  - If user says the rules 'don't look like me', raise min_support and re-run
  - noise_trades_pnl = cumulative PnL of real trades that match no rule (the 'emotional orders')
  - early_exit_pnl = winners held shorter than the rule's lower bound, opportunity cost prorated by the shortfall
  - late_exit_pnl = losers held longer than the rule's upper bound, loss prorated by the excess
  - missed_signals_pnl = shadow_pnl - real_pnl - sum(other four buckets) (residual)
  - Every signal scan output must carry 'research only, not a buy recommendation'
- 관련성 근거: The tooling (journal upload, multi-market backtest, PDF) is irrelevant, but the P&L attribution taxonomy (noise/early-exit/late-exit/overtrading/missed) is a clean, cheap way for the bot to audit its own journal and feed a self-review back to the head agent. Needs only the bot's own trade log.
- 적용 메모: Drop the user-journal, PDF and multi-market pieces. Implement the five-bucket attribution in pure Python over the bot's own trade journal (per-symbol, per-rule tag); run it daily or every N trades, not per 15-minute cycle. Emit a short JSON summary (noise_trade_share, avg_hold_vs_target, overtrading flag) that the risk_manager/head reads as a 'behavioral' input. The '< 5 samples -> abstain' rule maps directly to 'do not adjust parameters until >= N closed trades'.
- 참조 파일: (none; mentions extractor._translate_rule and the analyze_trade_journal Phase 4a tool in code) (약 1500 토큰)

### `strategy-dev-manager` — medium → backtest
- 목적: Five-phase lifecycle (INGEST paper -> EXTRACT factor/strategy -> IMPLEMENT SignalEngine + backtest -> EVALUATE against thresholds -> MONITOR decay with a state machine) for turning research papers into registered, monitored factors/strategies in the Vibe-Trading store.
- 필요 입력: PDF/report via read_document (with OCR quality field), Tools: sdm_register, sdm_status, alpha_bench, create_hypothesis, generate_backtest_config, scaffold_signal_engine, backtest, link_autopilot_backtest, factor_analysis, sdm_decay_scan, OHLCV data_map for the SignalEngine contract, Templates factor_signal_engine.py / strategy_signal_engine.py
- 산출: Registered artifact with status created/benching/active/monitoring/decayed/disabled, signal_engine.py returning symbol -> Series in [-1,1], Backtest bench record; factor IC/IR stats, Decay scan report (templates/decay_report.md)
- 핵심 휴리스틱:
  - Factor alive if IC mean > 0.03, IR > 0.5, IC positive ratio > 55%; strategy alive if Sharpe > 0.5 AND max drawdown < 30%
  - Decay bands (worst metric wins): Sharpe healthy > 1.0, warning 0.5-1.0, decayed 0-0.5, critical < 0; MaxDD healthy < 15%, warning 15-25%, decayed 25-40%, critical > 40%; win rate healthy > 55%, profit factor healthy > 1.5
  - State machine: active -> monitoring after 3 consecutive warning benches; monitoring -> decayed after 2 more decayed checks; decayed -> disabled after 3 critical periods; monitoring -> active on recovery (Sharpe > 1.0 and MaxDD < 15% for 2 periods)
  - Extra strategy decay signals: drawdown deepening 3+ windows; win rate down > 10 pts from baseline; trade count up > 50% with flat Sharpe; profit factor < 1.0
  - Minimum sample: Sharpe needs 30 trades, win rate 50, profit factor 30; below that report but exclude from transitions
  - Rolling window by holding period: < 5 days -> 60 bars; 5-20 days -> 120; > 20 days -> 252
  - Look-ahead rule: factor values use data up to T, returns from T+1; never negative shifts
  - Dedup: IC vs existing alpha > 0.99 = duplicate, 0.90-0.99 = variant; always cross-check extracted formulas against source to avoid hallucination
- 관련성 근거: The paper-ingestion/registry machinery is out of scope, but the evaluation thresholds, decay state machine and minimum-sample rules are directly reusable for monitoring the bot's own strategy health (paper and live) and for a backtest harness. No external data needed beyond the bot's journal.
- 적용 메모: Reuse only references/strategy_metrics.md and decay_thresholds.md: implement rolling Sharpe/MaxDD/win-rate/profit-factor over the bot's closed trades and a simplified healthy/warning/decayed/disabled state that the risk_manager can read (e.g. 'decayed' -> raise confidence threshold or pause new entries). Use the 60-bar window since holds are intraday. Scale drawdown bands down to the bot's -6% ladder. Drop papers, OCR, SDM tools and the SignalEngine contract.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/strategy-dev-manager/references/decay_thresholds.md, /home/user/hkuds/vibe-trading/agent/src/skills/strategy-dev-manager/references/strategy_metrics.md, /home/user/hkuds/vibe-trading/agent/src/skills/strategy-dev-manager/references/strategy_extraction_guide.md, /home/user/hkuds/vibe-trading/agent/src/skills/strategy-dev-manager/references/scheduled_decay_scan.md, /home/user/hkuds/vibe-trading/agent/src/skills/strategy-dev-manager/templates/factor_signal_engine.py, /home/user/hkuds/vibe-trading/agent/src/skills/strategy-dev-manager/templates/strategy_signal_engine.py, /home/user/hkuds/vibe-trading/agent/src/skills/strategy-dev-manager/templates/decay_report.md, /home/user/hkuds/vibe-trading/agent/src/skills/strategy-dev-manager/examples.md (약 2600 토큰)

### `strategy-generate` — medium → backtest
- 목적: Workflow for writing config.json + a SignalEngine (code/signal_engine.py), running the built-in backtest tool, and evaluating/fixing results; covers instrument code normalization across markets (A-share, US, HK, TSX, OKX crypto), warm-up bars, position_adjustment modes, optimizers, validation (Monte Carlo/bootstrap/walk-forward) and review criteria.
- 필요 입력: User strategy description with instrument codes and date range, Backtest tool, read_run_artifact, write_file, bash for AST check, Data loaders: tushare, yfinance, okx, ccxt, akshare, config.json schema
- 산출: config.json in run_dir root, code/signal_engine.py returning symbol -> Series in [-1,1], Backtest metrics/validation summary, passed/score, action_items (>= 2, parameter-specific)
- 핵심 휴리스틱:
  - Five design questions before coding: data fields, signal logic (entry/exit/direction/filters), position management (equal weight 1/N, stop-loss, max position), backtest params (default commission 0.1%), validation checklist (no NaN signals, no leverage, artifacts complete)
  - Declare warmup_bars (>= longest lookback, e.g. MA200 needs 200 bars) instead of silently widening start_date, otherwise the extra period gets graded
  - Hard gates: metrics.csv and equity.csv non-empty, exit_code 0, no NaN equity, trade_count > 0; pass = score >= 60; poor returns alone do not fail a correctly implemented strategy
  - Bug categories: zero trades (too-strict conditions), first trade > 2 years in, capital utilization < 50%, open position at end
  - position_adjustment: 'rebalance' re-fills every bar on a constant target (40 fills instead of 1 measured); use 'hold' when entries/exits carry the strategy; rebalance_tolerance 0.05 cut 60 fills to 5
  - Minute backtests: no more than 30 days of 1m or 1 year of 1H; crypto annualization uses 365 days
  - Validation: monte_carlo permutation p-value, bootstrap Sharpe 95% CI, walk_forward N windows consistency
  - Poor returns in a correct strategy are a valid result: report and stop; optimize only when asked
- 관련성 근거: The Vibe-Trading-specific config/engine is not reusable, but the pre-trade design checklist, hard gates, bug categories and the over-trading/rebalance warning are good guardrails for the bot's own backtest harness (ccxt OHLCV at 15m) and for how the head agent should phrase action_items.
- 적용 메모: Write our own minimal vectorized 15m backtester with the same five hard gates and bug checks, 10 bps commission, 'hold' semantics (enter on signal, exit on SL/TP/signal flip) to avoid per-bar churn, warm-up exclusion, and optional bootstrap/walk-forward validation. Keep the action_items format ('Change X from A to B', parameter-specific, >= 2) as the schema for the journal/head agent's improvement notes. Ignore instrument normalization, OKX source, fundamentals.
- 참조 파일: /home/user/hkuds/vibe-trading/agent/src/skills/strategy-generate/examples.md, ../cross-market-strategy/SKILL.md (cross-reference) (약 4000 토큰)

### `thesis-tracker` — medium → risk_manager
- 목적: Buy-side post-purchase discipline for equities: on buy, write a 5-sentence thesis, 3-7 falsifiable assumptions, red lines and valuation anchors to reports/{company}-thesis.md; each quarter re-check assumptions, score thesis health 1-10 and recommend add/hold/trim/exit.
- 필요 입력: web_search, get_stock_news, get_financial_statements, financial_rigor (verify_valuation, verify_market_cap), read_file / write_file on reports/{company}-thesis.md, Quarterly earnings, PE/PB, insider transactions
- 산출: Thesis file: core thesis (5 sentences), assumptions table, red-line table, valuation anchors, tracking table, Quarterly tracking report: health score /10, assumption statuses, red-line check, conclusion (intact/weakening/impaired/broken), action (add/hold/trim/exit), next-check timing
- 핵심 휴리스틱:
  - Write sell conditions before buying; if you cannot state the thesis in 5 sentences the buy decision is not clear enough
  - Assumptions must be verifiable ('ROE > 25% and stable' not 'a great company'), 3-7 of them
  - Status ladder per assumption: holding / weakening / impaired / broken
  - Health = 10 - 3*broken - 2*impaired - 1*weakening - 5*(red lines triggered), clamped to [1,10]
  - 9-10 consider adding; 7-8 hold; 5-6 hold with alertness; 3-4 consider trimming; 1-2 strongly consider exit
  - Act when a red line triggers; 'let's wait and see' is how big losses happen
  - Thesis breaking != price falling: a 30% drop does not force a sell, a broken thesis does
  - Only three reasons to sell: the buy was wrong, fundamentals changed, something better was found
- 관련성 근거: Equity fundamentals, earnings and valuation anchors do not apply, but the pattern of writing a falsifiable entry thesis with explicit invalidation conditions at entry time, then re-scoring it each cycle, maps well onto the bot's open positions (horizon of hours, not quarters) and gives the head/risk agents a disciplined hold/exit rubric.
- 적용 메모: At every BUY, have the head agent emit a structured 'thesis' JSON: 2-3 one-line reasons, 2-4 falsifiable assumptions in market terms (e.g. 'price holds above 4h EMA26', 'funding stays < 0.05%', 'no negative headline on symbol'), and red lines (beyond the hard SL). Each 15m cycle the risk_manager (sonnet) re-scores held positions with the 10 - 3/2/1/5 formula and outputs hold/trim/exit; code enforces the SL/TP regardless. Store theses in the journal and include them in the Telegram trade message.
- 참조 파일: (none; writes/reads reports/{company}-thesis.md at runtime) (약 2000 토큰)
