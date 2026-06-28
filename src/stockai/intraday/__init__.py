"""Intraday trading module for StockAI.

Generates daily intraday trading plans for IDX (Indonesia Stock Exchange) names.

This module is designed to run in <1 minute on a personal machine using only
free data (yfinance daily OHLCV) so the user can plug it into the
"15 minutes/day" passive-trader workflow.

The scoring methodology intentionally mirrors the May 2026 intraday report
(see `E:\project\stockai\reports\idx_intraday_202605.md`) so that the
auto-generated rankings stay interpretable and consistent with the manual
research the user already trusts.

Public surface:
    IntradayProfile  - per-name snapshot used by the screen / plan / report
    score_universe   - rank a list of tickers by intraday suitability
    generate_plan    - build next-day plan for a single ticker
    render_report    - render markdown report for the top-N names
"""

from stockai.intraday.scoring import IntradayProfile, score_universe
from stockai.intraday.planner import generate_plan, IntradayPlan
from stockai.intraday.reporter import render_report
from stockai.intraday.models import IntradayPlanRow, IntradayOutcomeRow
from stockai.intraday.storage import (
    save_plan,
    list_plans,
    evaluate_pending_plans,
    plan_stats,
)
from stockai.intraday.backtest import (
    BacktestTrade,
    run_backtest,
    aggregate,
    render_backtest_report,
    load_persisted_trades,
)
from stockai.intraday.regime import RegimeVerdict, check_regime

__all__ = [
    "IntradayProfile",
    "IntradayPlan",
    "IntradayPlanRow",
    "IntradayOutcomeRow",
    "BacktestTrade",
    "RegimeVerdict",
    "score_universe",
    "generate_plan",
    "render_report",
    "save_plan",
    "list_plans",
    "evaluate_pending_plans",
    "plan_stats",
    "run_backtest",
    "aggregate",
    "render_backtest_report",
    "load_persisted_trades",
    "check_regime",
]
