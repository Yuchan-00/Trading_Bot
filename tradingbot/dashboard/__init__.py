"""읽기 전용 파일 기반 대시보드 (SPEC §14).

``create_app(settings, data_dir)``가 FastAPI 앱을 만든다. 앱은 봇 프로세스와 파일(state.json,
logs/*.jsonl, cost_ledger.jsonl, backtest_results/, control/)로만 소통하며 거래소·LLM에 접근하지 않는다.
"""
from .app import create_app

__all__ = ["create_app"]
