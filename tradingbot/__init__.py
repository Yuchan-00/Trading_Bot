"""Claude API 멀티에이전트 Binance 현물 트레이딩 봇.

모든 리스크 한도는 코드(`risk.py`, `config.HardLimits`)가 강제하고, LLM은 한도 안에서만 결정한다.
기본 모드는 paper(가상 체결)이며 실자금 매매는 명시적 확인 없이는 켜지지 않는다.
"""

__version__ = "0.1.0"
