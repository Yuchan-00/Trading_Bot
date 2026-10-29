# 제3자 고지 (Third-Party Notices)

## 1. HKUDS/Vibe-Trading 에서 각색한 스킬

이 저장소의 `skills/` 디렉터리 아래에 있는 스킬 문서들은
[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) (MIT License) 의
스킬 문서를 바탕으로 **각색(재작성)** 한 것입니다.

원본은 범용 트레이딩 리서치 에이전트(도구 사용, 데이터 직접 조회, 지표 직접 계산,
롱/숏 양방향)를 전제로 작성되어 있으나, 본 프로젝트의 에이전트는
Binance 현물(롱 전용, USDT 페어, 시장가 주문, 15분 주기)에서 코드가 계산한
라벨링된 JSON 입력(`feature_pack` / `flow_pack` / `risk_pack` / `regime_pack` /
센티먼트 / 헤드라인 / 계정 상태 / 다른 역할의 출력)만을 받으며, 도구나 외부 데이터
접근 없이 입력 필드명을 인용하여 판단하도록 설계되어 있습니다. 따라서 원본의
휴리스틱과 체크리스트 중 본 프로젝트의 제약(현물 전용, 하드 리밋, 스키마 고정,
한국어 자유 서술)에 맞는 부분만 취해 다시 썼으며, 원문을 그대로 복제하지 않았습니다.
각색 과정의 상세 메모는 `docs/research/vibe_skill_survey.md` 와
`docs/research/agent_roster.md` 를 참고하십시오.

Vibe-Trading 에서 각색한 스킬 목록:

- `technical-basic`
- `ichimoku`
- `candlestick`
- `smc`
- `volatility`
- `minute-analysis`
- `perp-funding-basis`
- `crypto-derivatives`
- `liquidation-heatmap`
- `investor-lenses`
- `market-microstructure`
- `event-driven`
- `sentiment-analysis`
- `research-discipline`
- `behavioral-finance`
- `correlation-regime`
- `risk-analysis`
- `correlation-analysis`
- `asset-allocation`
- `execution-model`
- `thesis-tracker`
- `trade-journal`
- `strategy-dev-manager`
- `performance-attribution`
- `report-generate`

본 프로젝트는 Vibe-Trading 의 소스 코드, Microsoft Qlib 피처 정의, 알파 공식
(Alpha101 / GTJA191 / 학술 팩터) 및 번들 폰트는 **포함하지 않습니다**.
아래 라이선스 및 고지는 각색된 스킬 문서에 적용됩니다.

### 1.1 MIT License (원문)

```
MIT License

Copyright (c) 2026 Vibe-Trading Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 1.2 NOTICE (HKUDS 관련 부분 발췌)

```
Vibe-Trading
Copyright 2026 HKUDS contributors.

This product includes software developed at HKUDS (https://github.com/HKUDS).
```

(원본 NOTICE 의 Microsoft Qlib, 알파 공식, 폰트 관련 항목은 본 프로젝트가 해당
자료를 포함하지 않으므로 생략하였습니다.)

## 2. FinceptTerminal (AGPL-3.0)

[FinceptTerminal](https://github.com/Fincept-Corporation/FinceptTerminal) (AGPL-3.0) 은
설계 과정에서 **아이디어 참고 목적으로만 열람**하였습니다. 본 저장소에는
FinceptTerminal 의 코드, 문서, 프롬프트 등 어떠한 텍스트도 포함되어 있지 않으며,
따라서 AGPL-3.0 의 의무 사항은 본 프로젝트에 적용되지 않습니다.
