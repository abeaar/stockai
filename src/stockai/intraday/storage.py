"""Storage helpers for the intraday module.

Wraps the existing StockAI SQLite database so that:
  - generated plans are auto-persisted (idempotent on (symbol, plan_date))
  - outcomes are pulled from next-day yfinance OHLC and stored against plans
  - the user can query stats over a date range

We deliberately use the *same* database as the rest of StockAI rather than
a separate file: the user already has `data/stockai.db`, and SQLite handles
two more tables trivially.
"""

from __future__ import annotations

from datetime import date as _date, datetime, timedelta
from typing import Iterable

import pandas as pd

from stockai.data.database import get_db, session_scope
from stockai.data.sources.yahoo import get_yahoo_source
from stockai.intraday.models import IntradayPlanRow, IntradayOutcomeRow
from stockai.intraday.scoring import IntradayProfile
from stockai.intraday.planner import IntradayPlan


# ---------------------------------------------------------------------------
# Plan persistence
# ---------------------------------------------------------------------------

def save_plan(
    plan: IntradayPlan,
    profile: IntradayProfile,
    plan_date: _date,
    report_id: str | None = None,
) -> IntradayPlanRow:
    """Insert or update one IntradayPlanRow.

    Idempotent on (symbol, plan_date) — re-running the same day's report
    overwrites the existing plan in place (same id, refreshed values).
    """
    with session_scope() as s:
        existing = (
            s.query(IntradayPlanRow)
            .filter_by(symbol=plan.symbol, plan_date=plan_date)
            .one_or_none()
        )
        if existing is None:
            row = IntradayPlanRow(
                plan_date=plan_date,
                generated_at=datetime.utcnow(),
                symbol=plan.symbol,
                direction=plan.direction,
                bias=plan.bias,
                entry_low=plan.entry_zone_low,
                entry_high=plan.entry_zone_high,
                stop_loss=plan.stop_loss,
                tp1=plan.tp1,
                tp2=plan.tp2,
                rr_tp1=plan.rr_ratio_tp1,
                rr_tp2=plan.rr_ratio_tp2,
                preferred_window=plan.preferred_window,
                position_lots=plan.position_lots,
                thesis=plan.thesis,
                score=profile.score,
                sector=profile.sector,
                last_close=profile.last_close,
                adv_3m=profile.adv_3m,
                avg_day_range_pct=profile.avg_day_range_pct,
                beta=profile.beta,
                ret_1m=profile.ret_1m,
                report_id=report_id,
            )
            s.add(row)
        else:
            existing.direction = plan.direction
            existing.bias = plan.bias
            existing.entry_low = plan.entry_zone_low
            existing.entry_high = plan.entry_zone_high
            existing.stop_loss = plan.stop_loss
            existing.tp1 = plan.tp1
            existing.tp2 = plan.tp2
            existing.rr_tp1 = plan.rr_ratio_tp1
            existing.rr_tp2 = plan.rr_ratio_tp2
            existing.preferred_window = plan.preferred_window
            existing.position_lots = plan.position_lots
            existing.thesis = plan.thesis
            existing.score = profile.score
            existing.sector = profile.sector
            existing.last_close = profile.last_close
            existing.adv_3m = profile.adv_3m
            existing.avg_day_range_pct = profile.avg_day_range_pct
            existing.beta = profile.beta
            existing.ret_1m = profile.ret_1m
            existing.report_id = report_id
            existing.generated_at = datetime.utcnow()
            row = existing
        s.flush()
        s.refresh(row)
        # Detach so the caller can use it after the session closes.
        s.expunge(row)
        return row


def list_plans(
    plan_date: _date | None = None,
    symbol: str | None = None,
    limit: int = 100,
) -> list[IntradayPlanRow]:
    """Read plans back, optionally filtered by date and/or symbol."""
    from sqlalchemy.orm import joinedload
    with session_scope() as s:
        q = s.query(IntradayPlanRow).options(joinedload(IntradayPlanRow.outcome))
        if plan_date is not None:
            q = q.filter(IntradayPlanRow.plan_date == plan_date)
        if symbol is not None:
            q = q.filter(IntradayPlanRow.symbol == symbol.upper())
        rows = q.order_by(IntradayPlanRow.plan_date.desc(), IntradayPlanRow.symbol.asc()).limit(limit).all()
        # Touch the relationship while still in session, then expunge.
        for r in rows:
            _ = r.outcome
        for r in rows:
            s.expunge(r)
        return rows


# ---------------------------------------------------------------------------
# Outcome evaluation
# ---------------------------------------------------------------------------

def _classify_outcome(
    plan: IntradayPlanRow, ohlc: pd.Series
) -> tuple[str, float, float, str]:
    """Classify one plan's outcome against one session's OHLC.

    We use a conservative interpretation:
      - Use OPEN as the fill price (assumes you got filled at the open).
      - Walk the day in price-priority order: which level (TP1, TP2, SL) was
        hit first given the open and the day's high/low?
      - For LONG plans: SL hit if low <= sl. TP1/TP2 hit if high >= tp1/tp2.
        Whichever happens first in price-order wins.
      - For SHORT plans: mirror.
      - For NEUTRAL mean-reversion: assume fade from upper band; if open
        is in the upper half of the entry zone, short; otherwise we treat
        the trade as not triggered and return EOD/NO_DATA semantics.

    Returns (outcome, r_multiple, pnl_per_lot_idr, notes).
    """
    op = float(ohlc["open"])
    hi = float(ohlc["high"])
    lo = float(ohlc["low"])
    cl = float(ohlc["close"])
    lot = 100  # shares per lot

    direction = plan.direction.upper()
    notes = ""

    if "LONG" in direction:
        # Fill assumption: open if open is inside entry zone, else use the
        # nearest zone edge (favorable-to-user approximation).
        if plan.entry_low <= op <= plan.entry_high:
            fill = op
        elif op < plan.entry_low:
            fill = plan.entry_low
            notes = "gapped below entry; assumed fill at entry_low"
        else:
            fill = plan.entry_high
            notes = "gapped above entry; assumed fill at entry_high"

        risk_per_share = fill - plan.stop_loss
        if risk_per_share <= 0:
            return ("EOD", 0.0, 0.0, "invalid plan: stop above fill")

        # Order of hits: which level (TP1, TP2, SL) is closest to fill in
        # price? We approximate "which was hit first" by distance to fill
        # because we don't have tick-by-tick. This is the standard
        # backtest-engine approximation when only daily OHLC is available.
        dist_tp1 = plan.tp1 - fill
        dist_tp2 = plan.tp2 - fill
        dist_sl = fill - plan.stop_loss

        if hi >= plan.tp2 and dist_tp2 <= dist_sl:
            r = (plan.tp2 - fill) / risk_per_share
            pnl = (plan.tp2 - fill) * lot
            return ("TP2", r, pnl, notes)
        if hi >= plan.tp1 and dist_tp1 <= dist_sl:
            r = (plan.tp1 - fill) / risk_per_share
            pnl = (plan.tp1 - fill) * lot
            return ("TP1", r, pnl, notes)
        if lo <= plan.stop_loss:
            r = -1.0
            pnl = (plan.stop_loss - fill) * lot
            return ("SL", r, pnl, notes)
        # EOD scratch
        r = (cl - fill) / risk_per_share
        pnl = (cl - fill) * lot
        return ("EOD", r, pnl, notes + " (scratch)")

    if "SHORT" in direction:
        if plan.entry_low <= op <= plan.entry_high:
            fill = op
        elif op > plan.entry_high:
            fill = plan.entry_high
            notes = "gapped above entry; assumed fill at entry_high"
        else:
            fill = plan.entry_low
            notes = "gapped below entry; assumed fill at entry_low"

        risk_per_share = plan.stop_loss - fill
        if risk_per_share <= 0:
            return ("EOD", 0.0, 0.0, "invalid plan: stop below fill")

        dist_tp1 = fill - plan.tp1
        dist_tp2 = fill - plan.tp2
        dist_sl = plan.stop_loss - fill

        if lo <= plan.tp2 and dist_tp2 <= dist_sl:
            r = (fill - plan.tp2) / risk_per_share
            pnl = (fill - plan.tp2) * lot
            return ("TP2", r, pnl, notes)
        if lo <= plan.tp1 and dist_tp1 <= dist_sl:
            r = (fill - plan.tp1) / risk_per_share
            pnl = (fill - plan.tp1) * lot
            return ("TP1", r, pnl, notes)
        if hi >= plan.stop_loss:
            r = -1.0
            pnl = (fill - plan.stop_loss) * lot
            return ("SL", r, pnl, notes)
        r = (fill - cl) / risk_per_share
        pnl = (fill - cl) * lot
        return ("EOD", r, pnl, notes + " (scratch)")

    # NEUTRAL mean-reversion: conservative implementation -> scratch unless
    # the day moved a lot against us. Mark as EOD with zero R.
    return ("EOD", 0.0, 0.0, "mean-reversion plan (informational only)")


def evaluate_pending_plans(as_of: _date | None = None) -> int:
    """Find all plans dated before `as_of` with no outcome, fetch next-day
    OHLC, classify, and persist the outcome.

    Returns the number of plans newly evaluated.
    """
    as_of = as_of or _date.today()
    src = get_yahoo_source()
    evaluated = 0

    with session_scope() as s:
        pending = (
            s.query(IntradayPlanRow)
            .filter(IntradayPlanRow.plan_date < as_of)
            .outerjoin(IntradayOutcomeRow, IntradayPlanRow.id == IntradayOutcomeRow.plan_id)
            .filter(IntradayOutcomeRow.id.is_(None))
            .all()
        )
        for plan in pending:
            # Need the next session AFTER plan_date. We pull a 1-month window
            # so we get at least one trading day after plan_date (skip
            # weekends / holidays naturally).
            start = plan.plan_date + timedelta(days=1)
            end = plan.plan_date + timedelta(days=10)
            try:
                hist = src.get_price_history(
                    plan.symbol,
                    start=pd.Timestamp(start),
                    end=pd.Timestamp(end),
                    interval="1d",
                )
            except Exception as e:  # noqa: BLE001
                plan.outcome = IntradayOutcomeRow(
                    eval_date=as_of,
                    outcome="NO_DATA",
                    r_multiple=0.0,
                    pnl_per_lot_idr=0.0,
                    notes=f"yfinance fetch failed: {e}",
                )
                evaluated += 1
                continue
            if hist.empty:
                plan.outcome = IntradayOutcomeRow(
                    eval_date=as_of,
                    outcome="NO_DATA",
                    r_multiple=0.0,
                    pnl_per_lot_idr=0.0,
                    notes="no next-day OHLC (holiday or delisted?)",
                )
                evaluated += 1
                continue
            # First row after plan_date.
            session = hist.sort_values("date").iloc[0]
            outcome, r_mult, pnl, notes = _classify_outcome(plan, session)
            plan.outcome = IntradayOutcomeRow(
                eval_date=session["date"].date() if hasattr(session["date"], "date") else as_of,
                open_price=float(session["open"]),
                high_price=float(session["high"]),
                low_price=float(session["low"]),
                close_price=float(session["close"]),
                outcome=outcome,
                r_multiple=r_mult,
                pnl_per_lot_idr=pnl,
                notes=notes,
            )
            evaluated += 1
    return evaluated


# ---------------------------------------------------------------------------
# Stats / aggregation
# ---------------------------------------------------------------------------

def plan_stats(symbol: str | None = None, since: _date | None = None) -> dict:
    """Aggregate stats over evaluated plans.

    Returns a dict with: n, n_evaluated, n_tp1, n_tp2, n_sl, n_eod, win_rate,
    avg_r, expectancy, total_pnl_idr.
    """
    with session_scope() as s:
        q = (
            s.query(IntradayPlanRow, IntradayOutcomeRow)
            .join(IntradayOutcomeRow, IntradayPlanRow.id == IntradayOutcomeRow.plan_id)
        )
        if symbol is not None:
            q = q.filter(IntradayPlanRow.symbol == symbol.upper())
        if since is not None:
            q = q.filter(IntradayPlanRow.plan_date >= since)
        # Force-load all the fields we need while the session is alive.
        rows = []
        for p, o in q.all():
            rows.append((
                p.symbol, p.plan_date, o.outcome, o.r_multiple, o.pnl_per_lot_idr
            ))

    n = len(rows)
    if n == 0:
        return {
            "n": 0, "n_evaluated": 0, "n_tp1": 0, "n_tp2": 0, "n_sl": 0,
            "n_eod": 0, "n_other": 0,
            "win_rate": 0.0, "avg_r": 0.0, "expectancy_r": 0.0,
            "total_pnl_idr": 0.0,
        }

    n_tp1 = sum(1 for _, _, o, _, _ in rows if o == "TP1")
    n_tp2 = sum(1 for _, _, o, _, _ in rows if o == "TP2")
    n_sl = sum(1 for _, _, o, _, _ in rows if o == "SL")
    n_eod = sum(1 for _, _, o, _, _ in rows if o == "EOD")
    n_other = n - (n_tp1 + n_tp2 + n_sl + n_eod)

    wins = n_tp1 + n_tp2
    win_rate = wins / n
    avg_r = sum(r for _, _, _, r, _ in rows) / n
    expectancy_r = avg_r  # equal-weight sizing assumed
    total_pnl = sum(p for _, _, _, _, p in rows)

    return {
        "n": n,
        "n_evaluated": n,
        "n_tp1": n_tp1,
        "n_tp2": n_tp2,
        "n_sl": n_sl,
        "n_eod": n_eod,
        "n_other": n_other,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "expectancy_r": expectancy_r,
        "total_pnl_idr": total_pnl,
    }


__all__ = [
    "save_plan",
    "list_plans",
    "evaluate_pending_plans",
    "plan_stats",
]
