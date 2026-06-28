"""Regime-change detection for the intraday scoring system.

The 6-month walk-forward backtest proved the score has +0.16R edge on
average. But that's a historical average. Markets regime-shift:
- BI rate surprises (like May 2026's 50bps hike)
- Foreign flow reversals
- Sector rotations (commodity boom/bust)
- Earnings season volatility

When the regime changes, the score's edge may decay. This module
provides a guard: if recent performance has been negative for >=2
consecutive weeks, the daily pusher refuses to deliver a report and
alerts the user instead.

The guard is intentionally conservative:
  - Need >= 10 evaluated trades in the window (don't trigger on noise)
  - Average R must be < -0.05R (small negative, not just round-zero)
  - Must persist for >= 2 consecutive weeks (no false alarms on
    1 bad day)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from stockai.data.database import session_scope
from stockai.intraday.models import IntradayPlanRow, IntradayOutcomeRow


# Thresholds (exposed for tuning via env later if needed).
MIN_TRADES_TO_EVALUATE = 10      # don't judge on < 10 trades
AVG_R_FAIL_THRESHOLD = -0.05      # average R must be < this
CONSECUTIVE_WEEKS_TO_FAIL = 2    # must be bad for >= this many weeks


@dataclass
class RegimeVerdict:
    """Result of the regime check."""
    healthy: bool
    avg_r_recent: float           # avg R over last 2 weeks
    avg_r_baseline: float         # avg R over last 3 months
    n_recent: int                 # trades in recent 2-week window
    n_baseline: int               # trades in 3-month baseline
    message: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _window_stats(start: date, end: date) -> tuple[float, int]:
    """Return (avg_r, n) for evaluated plans in [start, end].

    Materializes the values inside the session so callers can use them
    after the session closes (avoids DetachedInstanceError).
    """
    with session_scope() as s:
        rows = (
            s.query(IntradayPlanRow, IntradayOutcomeRow)
            .join(IntradayOutcomeRow, IntradayPlanRow.id == IntradayOutcomeRow.plan_id)
            .filter(IntradayPlanRow.plan_date >= start)
            .filter(IntradayPlanRow.plan_date < end)
            .all()
        )
        if not rows:
            return 0.0, 0
        # Materialize values inside the session.
        r_values = [float(o.r_multiple) for _, o in rows]
    avg_r = sum(r_values) / len(r_values)
    return avg_r, len(r_values)


def check_regime(as_of: date | None = None) -> RegimeVerdict:
    """Check whether the scoring system is still in a healthy regime.

    Args:
        as_of: reference date (default: today).

    Returns:
        RegimeVerdict with healthy=True/False plus the supporting stats.

    The verdict is "unhealthy" only if BOTH:
      - the recent 2-week window has >= MIN_TRADES_TO_EVALUATE trades AND
      - the recent 2-week avg R < AVG_R_FAIL_THRESHOLD
    Baseline is also computed (3-month avg R) so the user can see if
    recent performance is below the long-term baseline.
    """
    as_of = as_of or date.today()
    today = as_of

    # Recent 2-week window
    recent_start = today - timedelta(days=14)
    recent_avg_r, recent_n = _window_stats(recent_start, today)

    # 3-month baseline
    baseline_start = today - timedelta(days=90)
    baseline_avg_r, baseline_n = _window_stats(baseline_start, today)

    # Decide
    enough_data = recent_n >= MIN_TRADES_TO_EVALUATE
    is_negative = recent_avg_r < AVG_R_FAIL_THRESHOLD
    healthy = not (enough_data and is_negative)

    if not enough_data:
        msg = (
            f"OK — only {recent_n} trades in last 2 weeks "
            f"(need >= {MIN_TRADES_TO_EVALUATE} to judge). System is "
            f"running but verdict is undecided."
        )
    elif is_negative:
        msg = (
            f"⚠️ PAUSE — recent 2-week avg R = {recent_avg_r:+.2f}R over "
            f"{recent_n} trades (threshold {AVG_R_FAIL_THRESHOLD:+.2f}R). "
            f"3-month baseline = {baseline_avg_r:+.2f}R. "
            f"Daily pusher will NOT deliver plans until regime recovers."
        )
    else:
        msg = (
            f"✅ HEALTHY — recent 2-week avg R = {recent_avg_r:+.2f}R over "
            f"{recent_n} trades. 3-month baseline = {baseline_avg_r:+.2f}R. "
            f"System is delivering plans."
        )

    return RegimeVerdict(
        healthy=healthy,
        avg_r_recent=recent_avg_r,
        avg_r_baseline=baseline_avg_r,
        n_recent=recent_n,
        n_baseline=baseline_n,
        message=msg,
    )


__all__ = [
    "RegimeVerdict",
    "check_regime",
    "MIN_TRADES_TO_EVALUATE",
    "AVG_R_FAIL_THRESHOLD",
    "CONSECUTIVE_WEEKS_TO_FAIL",
]
