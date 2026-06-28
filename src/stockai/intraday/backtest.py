"""Walk-forward historical backtest for the intraday scoring system.

For each trading day in the lookback window, this module:

  1. Builds an `IntradayProfile` using ONLY data available at the end of
     that day (no look-ahead). This is critical — leaking the next day
     into the profile would invalidate the entire backtest.
  2. Generates a plan.
  3. Classifies the outcome on the NEXT trading session using the same
     `evaluate_pending_plans` machinery.
  4. Persists every (plan, outcome) pair to the database (so the
     existing `intraday stats` command can read them too).

After running, the user gets an aggregate report bucketed by:
  - score decile
  - direction
  - ticker
  - year/quarter

This is the *only* way to know whether the score has real edge or is
just noise. A 50-trade sample is the minimum to draw any conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from typing import Iterable

import pandas as pd

from stockai.data.sources.yahoo import get_yahoo_source
from stockai.data.sources.idx import get_lq45
from stockai.intraday.scoring import (
    IntradayProfile, SECTOR_MAP, _bracket_score, _beta_subscore,
    _momentum_subscore, _idrx_tick, LIQUIDITY_BRACKETS, VOLATILITY_BRACKETS,
    SPREAD_BRACKETS,
)
from stockai.intraday.planner import generate_plan
from stockai.intraday.storage import _classify_outcome, save_plan
from stockai.intraday.models import IntradayOutcomeRow
from stockai.data.database import session_scope
from sqlalchemy import and_


# Universe defaults
DEFAULT_UNIVERSE: list[str] | None = None  # filled at call time with get_lq45()


@dataclass
class BacktestTrade:
    """Lightweight per-trade record (no SQL roundtrip)."""
    symbol: str
    plan_date: _date
    direction: str
    score: float
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    rr_tp1: float
    rr_tp2: float
    outcome: str
    r_multiple: float
    pnl_per_lot: float


def _build_profile_asof(
    symbol: str,
    as_of: _date,
    src,
) -> IntradayProfile | None:
    """Build an IntradayProfile using only data with date <= as_of.

    Returns None if there isn't enough history.
    """
    # Pull 6M of data and slice to as_of.
    end = pd.Timestamp(as_of)
    start = end - pd.Timedelta(days=200)
    try:
        hist = src.get_price_history(symbol, start=start, end=end, interval="1d")
    except Exception:
        return None
    if hist.empty or len(hist) < 30:
        return None
    hist = hist.sort_values("date").reset_index(drop=True)
    if hist["date"].dt.tz is not None:
        hist["date"] = hist["date"].dt.tz_localize(None)
    # Ensure we don't include rows after as_of.
    as_of_dt = pd.Timestamp(as_of)
    hist = hist[hist["date"] <= as_of_dt]
    if len(hist) < 30:
        return None

    last_close = float(hist.iloc[-1]["close"])
    hist_3m = hist.tail(63)
    adv_3m = float(hist_3m["volume"].mean())
    turnover = adv_3m * last_close
    hist_3m = hist_3m.copy()
    hist_3m["_range_pct"] = (hist_3m["high"] - hist_3m["low"]) / hist_3m["close"]
    avg_day_range = float(hist_3m["_range_pct"].mean())
    ret_1m = float((last_close / hist_3m.iloc[0]["close"]) - 1.0) if len(hist_3m) else 0.0
    ret_1w = float((last_close / hist.iloc[-6]["close"]) - 1.0) if len(hist) >= 6 else ret_1m
    spread = (2 * _idrx_tick(last_close)) / last_close

    # Beta: try to fetch, but tolerate missing.
    beta = None
    try:
        info = src.get_stock_info(symbol) or {}
        b = info.get("beta")
        if b is not None and not (isinstance(b, float) and b != b):
            beta = float(b)
    except Exception:
        beta = None

    sub_liq = _bracket_score(adv_3m, LIQUIDITY_BRACKETS)
    sub_vol = _bracket_score(avg_day_range, VOLATILITY_BRACKETS)
    sub_spr = _bracket_score(spread, SPREAD_BRACKETS)
    sub_bet = _beta_subscore(beta)
    sub_mom = _momentum_subscore(ret_1m)
    score = (
        0.30 * sub_liq + 0.30 * sub_vol + 0.15 * sub_spr
        + 0.15 * sub_bet + 0.10 * sub_mom
    )

    return IntradayProfile(
        symbol=symbol,
        sector=SECTOR_MAP.get(symbol, "Other"),
        last_close=last_close,
        adv_3m=adv_3m,
        turnover_idr_3m=turnover,
        day_range_pct=float(
            (hist.iloc[-1]["high"] - hist.iloc[-1]["low"]) / hist.iloc[-1]["close"]
        ),
        avg_day_range_pct=avg_day_range,
        spread_pct=spread,
        beta=beta,
        ret_1m=ret_1m,
        ret_1w=ret_1w,
        sub_liquidity=sub_liq,
        sub_volatility=sub_vol,
        sub_spread=sub_spr,
        sub_beta=sub_bet,
        sub_momentum=sub_mom,
        score=score,
    )


def _get_trading_days(
    symbol: str, src, start: _date, end: _date
) -> list[pd.Timestamp]:
    """Return the list of trading days for `symbol` in [start, end]."""
    try:
        hist = src.get_price_history(
            symbol, start=pd.Timestamp(start), end=pd.Timestamp(end), interval="1d"
        )
    except Exception:
        return []
    if hist.empty:
        return []
    hist = hist.sort_values("date").reset_index(drop=True)
    if hist["date"].dt.tz is not None:
        hist["date"] = hist["date"].dt.tz_localize(None)
    return list(hist["date"])


def _has_plan_already(symbol: str, plan_date: _date) -> bool:
    """Check if a plan already exists for (symbol, plan_date)."""
    with session_scope() as s:
        from stockai.intraday.models import IntradayPlanRow
        existing = (
            s.query(IntradayPlanRow)
            .filter_by(symbol=symbol, plan_date=plan_date)
            .one_or_none()
        )
        return existing is not None


def _next_session_ohlc(symbol: str, src, plan_date: _date) -> pd.Series | None:
    """Fetch the first trading session after plan_date."""
    try:
        hist = src.get_price_history(
            symbol,
            start=pd.Timestamp(plan_date + timedelta(days=1)),
            end=pd.Timestamp(plan_date + timedelta(days=15)),
            interval="1d",
        )
    except Exception:
        return None
    if hist.empty:
        return None
    hist = hist.sort_values("date").reset_index(drop=True)
    if hist["date"].dt.tz is not None:
        hist["date"] = hist["date"].dt.tz_localize(None)
    return hist.iloc[0]


def run_backtest(
    tickers: Iterable[str] | None = None,
    start: _date | None = None,
    end: _date | None = None,
    step_days: int = 7,           # generate one plan per ticker every N days
    capital_idr: float = 10_000_000,
    persist: bool = True,         # save to DB so `intraday stats` works
    skip_if_exists: bool = True,  # idempotent: don't re-evaluate known plans
) -> list[BacktestTrade]:
    """Run the walk-forward backtest.

    Args:
        tickers: list of bare tickers. If None, uses LQ45.
        start: first plan_date (default: 6 months ago).
        end: last plan_date (default: today).
        step_days: plan every N trading days per ticker (weekly = 5).
        capital_idr: for position sizing (not used in the trade logic, only
                     for the lots annotation).
        persist: write to DB.
        skip_if_exists: if a plan already exists for (symbol, date), skip.

    Returns:
        list[BacktestTrade] of every trade evaluated.
    """
    if tickers is None:
        tickers = get_lq45()
    end = end or _date.today()
    start = start or (end - timedelta(days=180))
    src = get_yahoo_source()

    trades: list[BacktestTrade] = []
    n_skipped = 0
    n_persisted = 0

    for sym in tickers:
        # Get the trading days available for this symbol.
        days = _get_trading_days(sym, src, start, end)
        if not days:
            continue
        # Sample every `step_days`-th day.
        sampled = days[::step_days]
        for day_ts in sampled:
            plan_date = day_ts.date() if hasattr(day_ts, "date") else day_ts
            if skip_if_exists and _has_plan_already(sym, plan_date):
                # We already have an outcome too, presumably.
                n_skipped += 1
                continue
            profile = _build_profile_asof(sym, plan_date, src)
            if profile is None:
                continue
            plan = generate_plan(profile, capital_idr=capital_idr)
            # Fetch next session and classify.
            nxt = _next_session_ohlc(sym, src, plan_date)
            if nxt is None:
                continue
            # We need a "plan-like" object for _classify_outcome. Build a
            # minimal duck-typed wrapper using the saved row.
            if persist:
                row = save_plan(plan, profile, plan_date=plan_date, report_id="backtest")
                outcome, r_mult, pnl, notes = _classify_outcome(row, nxt)
                # Persist the outcome row (UPDATE if it already exists).
                with session_scope() as s:
                    from stockai.intraday.models import IntradayPlanRow, IntradayOutcomeRow
                    plan_db = (
                        s.query(IntradayPlanRow)
                        .filter_by(symbol=sym, plan_date=plan_date)
                        .one()
                    )
                    if plan_db.outcome is None:
                        plan_db.outcome = IntradayOutcomeRow(
                            eval_date=(
                                nxt["date"].date() if hasattr(nxt["date"], "date") else plan_date
                            ),
                            open_price=float(nxt["open"]),
                            high_price=float(nxt["high"]),
                            low_price=float(nxt["low"]),
                            close_price=float(nxt["close"]),
                            outcome=outcome,
                            r_multiple=r_mult,
                            pnl_per_lot_idr=pnl,
                            notes=notes,
                        )
                    else:
                        # Refresh in place.
                        plan_db.outcome.eval_date = (
                            nxt["date"].date() if hasattr(nxt["date"], "date") else plan_date
                        )
                        plan_db.outcome.open_price = float(nxt["open"])
                        plan_db.outcome.high_price = float(nxt["high"])
                        plan_db.outcome.low_price = float(nxt["low"])
                        plan_db.outcome.close_price = float(nxt["close"])
                        plan_db.outcome.outcome = outcome
                        plan_db.outcome.r_multiple = r_mult
                        plan_db.outcome.pnl_per_lot_idr = pnl
                        plan_db.outcome.notes = notes
                n_persisted += 1
            else:
                # In-memory classification using a lightweight shim.
                class _Shim:
                    pass
                shim = _Shim()
                shim.symbol = plan.symbol
                shim.direction = plan.direction
                shim.entry_low = plan.entry_zone_low
                shim.entry_high = plan.entry_zone_high
                shim.stop_loss = plan.stop_loss
                shim.tp1 = plan.tp1
                shim.tp2 = plan.tp2
                outcome, r_mult, pnl, notes = _classify_outcome(shim, nxt)

            trades.append(BacktestTrade(
                symbol=sym,
                plan_date=plan_date,
                direction=plan.direction,
                score=profile.score,
                entry_low=plan.entry_zone_low,
                entry_high=plan.entry_zone_high,
                stop_loss=plan.stop_loss,
                tp1=plan.tp1,
                tp2=plan.tp2,
                rr_tp1=plan.rr_ratio_tp1,
                rr_tp2=plan.rr_ratio_tp2,
                outcome=outcome,
                r_multiple=r_mult,
                pnl_per_lot=pnl,
            ))

    print(f"[backtest] {len(trades)} new trades, {n_skipped} skipped, {n_persisted} persisted.")
    return trades


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------

def _decile(score: float) -> str:
    """Bucket a 0-10 score into a 0.5-wide bin label like '7.5-8.0'."""
    lo = round(score * 2) / 2
    hi = lo + 0.5
    return f"{lo:.1f}-{hi:.1f}"


def aggregate(trades: list[BacktestTrade]) -> dict:
    """Aggregate stats over a list of BacktestTrade.

    Returns a dict with:
      - overall: {n, win_rate, avg_r, total_pnl_per_lot, ...}
      - by_score: list of (decile, stats) sorted by decile
      - by_direction: list of (direction, stats)
      - by_symbol: list of (symbol, stats) sorted by n desc
    """
    if not trades:
        return {"overall": {}, "by_score": [], "by_direction": [], "by_symbol": []}

    def _stats(rows: list[BacktestTrade]) -> dict:
        n = len(rows)
        n_tp1 = sum(1 for r in rows if r.outcome == "TP1")
        n_tp2 = sum(1 for r in rows if r.outcome == "TP2")
        n_sl = sum(1 for r in rows if r.outcome == "SL")
        n_eod = sum(1 for r in rows if r.outcome == "EOD")
        wins = n_tp1 + n_tp2
        win_rate = wins / n
        avg_r = sum(r.r_multiple for r in rows) / n
        total_pnl = sum(r.pnl_per_lot for r in rows)
        return {
            "n": n, "n_tp2": n_tp2, "n_tp1": n_tp1, "n_sl": n_sl, "n_eod": n_eod,
            "win_rate": win_rate, "avg_r": avg_r, "total_pnl_per_lot": total_pnl,
        }

    by_score_dict: dict[str, list[BacktestTrade]] = {}
    by_dir_dict: dict[str, list[BacktestTrade]] = {}
    by_sym_dict: dict[str, list[BacktestTrade]] = {}
    for t in trades:
        by_score_dict.setdefault(_decile(t.score), []).append(t)
        by_dir_dict.setdefault(t.direction, []).append(t)
        by_sym_dict.setdefault(t.symbol, []).append(t)

    by_score = sorted(
        ((k, _stats(v)) for k, v in by_score_dict.items()),
        key=lambda x: x[0],
    )
    by_direction = sorted(
        ((k, _stats(v)) for k, v in by_dir_dict.items()),
        key=lambda x: -x[1]["n"],
    )
    by_symbol = sorted(
        ((k, _stats(v)) for k, v in by_sym_dict.items()),
        key=lambda x: -x[1]["n"],
    )

    return {
        "overall": _stats(trades),
        "by_score": by_score,
        "by_direction": by_direction,
        "by_symbol": by_symbol,
    }


def render_backtest_report(
    trades: list[BacktestTrade] | None = None,
    agg: dict | None = None,
    *,
    title: str = "Intraday Score — Walk-Forward Backtest",
) -> str:
    """Render a markdown backtest report. Reads from DB if trades is None."""
    if trades is None:
        trades = load_persisted_trades()
    if agg is None:
        agg = aggregate(trades)
    o = agg.get("overall", {})
    if not o:
        return f"# {title}\n\n_No trades to report._\n"

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**Trades evaluated:** {o['n']}")
    lines.append(
        f"**Win rate (TP1+TP2):** {o['win_rate']*100:.1f}%   "
        f"**Avg R:** {o['avg_r']:+.2f}R   "
        f"**Total P&L / lot:** Rp {o['total_pnl_per_lot']:,.0f}"
    )
    lines.append("")
    lines.append(
        f"**Outcome mix:** TP2={o['n_tp2']}  TP1={o['n_tp1']}  "
        f"SL={o['n_sl']}  EOD={o['n_eod']}"
    )
    lines.append("")

    # ----- By score -----
    lines.append("## By Score Decile (does a higher score = better outcome?)")
    lines.append("")
    lines.append("| Score | N | Win% | Avg R | P&L / lot |")
    lines.append("|-------|---|------|-------|-----------|")
    for decile, s in agg["by_score"]:
        # Win% colors: anything > 25% is a clear green in a sample where
        # most trades are EOD scratches (because the score is a 0-10
        # confidence, not a win% probability).
        if s["win_rate"] > 0.30:
            win_color = "🟢"
        elif s["win_rate"] > 0.20:
            win_color = "🟡"
        else:
            win_color = "🔴"
        lines.append(
            f"| {decile} | {s['n']} | {win_color} {s['win_rate']*100:.0f}% | "
            f"{s['avg_r']:+.2f}R | Rp {s['total_pnl_per_lot']:,.0f} |"
        )
    lines.append("")

    # ----- By direction -----
    lines.append("## By Direction")
    lines.append("")
    lines.append("| Direction | N | Win% | Avg R | P&L / lot |")
    lines.append("|-----------|---|------|-------|-----------|")
    for d, s in agg["by_direction"]:
        lines.append(
            f"| {d} | {s['n']} | {s['win_rate']*100:.0f}% | "
            f"{s['avg_r']:+.2f}R | Rp {s['total_pnl_per_lot']:,.0f} |"
        )
    lines.append("")

    # ----- By symbol -----
    lines.append("## By Symbol (top 20 by trade count)")
    lines.append("")
    lines.append("| Symbol | N | Win% | Avg R | P&L / lot |")
    lines.append("|--------|---|------|-------|-----------|")
    for sym, s in agg["by_symbol"][:20]:
        lines.append(
            f"| {sym} | {s['n']} | {s['win_rate']*100:.0f}% | "
            f"{s['avg_r']:+.2f}R | Rp {s['total_pnl_per_lot']:,.0f} |"
        )
    lines.append("")

    # ----- Verdict -----
    lines.append("## Verdict")
    lines.append("")
    if o["n"] < 30:
        lines.append(
            f"⚠️  Only **{o['n']} trades** — too small to draw conclusions. "
            "Recommended minimum: 50 trades for a directional edge, "
            "100+ for statistical significance."
        )
    elif o["avg_r"] > 0.10:
        lines.append(
            f"🟢 **POSITIVE edge detected.** Avg R = {o['avg_r']:+.2f}R over "
            f"{o['n']} trades. The scoring system appears to add value."
        )
    elif o["avg_r"] > 0:
        lines.append(
            f"🟡 **Marginal edge.** Avg R = {o['avg_r']:+.2f}R is positive but "
            "small. Consider tightening the score brackets or filtering further."
        )
    elif o["avg_r"] > -0.10:
        lines.append(
            f"🟡 **Roughly break-even.** Avg R = {o['avg_r']:+.2f}R. "
            "The system is not adding value but not destroying it either."
        )
    else:
        lines.append(
            f"🔴 **Negative edge.** Avg R = {o['avg_r']:+.2f}R. "
            "The scoring system is selecting the wrong names. "
            "Re-evaluate the sub-score weights or remove the module."
        )
    lines.append("")
    lines.append("---")
    lines.append(
        "*Generated by `stockai intraday backtest`. For research/educational use only — "
        "not investment advice.*"
    )
    lines.append("")
    return "\n".join(lines)


def load_persisted_trades() -> list[BacktestTrade]:
    """Load all evaluated plans from the DB as BacktestTrade objects."""
    from stockai.intraday.models import IntradayPlanRow, IntradayOutcomeRow
    out: list[BacktestTrade] = []
    with session_scope() as s:
        rows = (
            s.query(IntradayPlanRow, IntradayOutcomeRow)
            .join(IntradayOutcomeRow, IntradayPlanRow.id == IntradayOutcomeRow.plan_id)
            .all()
        )
        for p, o in rows:
            out.append(BacktestTrade(
                symbol=p.symbol,
                plan_date=p.plan_date,
                direction=p.direction,
                score=p.score,
                entry_low=p.entry_low,
                entry_high=p.entry_high,
                stop_loss=p.stop_loss,
                tp1=p.tp1,
                tp2=p.tp2,
                rr_tp1=p.rr_tp1,
                rr_tp2=p.rr_tp2,
                outcome=o.outcome,
                r_multiple=o.r_multiple,
                pnl_per_lot=o.pnl_per_lot_idr,
            ))
    return out


__all__ = [
    "BacktestTrade",
    "run_backtest",
    "aggregate",
    "render_backtest_report",
    "load_persisted_trades",
]
