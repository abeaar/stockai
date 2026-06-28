"""Per-ticker next-day intraday plan generator.

Given an `IntradayProfile`, build a structured trade plan:
  - Direction bias (long / short / neutral)
  - Entry zone (around VWAP / opening range)
  - Stop-loss
  - TP1 / TP2
  - Preferred time window (morning momentum vs mean-reversion)
  - Position size hint (in lots; lot = 100 shares on IDX)
  - One-line thesis

This is intentionally rule-based and explainable. The "15 minutes/day"
passive-trader workflow is the target user; we want a plan they can read
in 30 seconds and decide whether to act.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stockai.intraday.scoring import IntradayProfile


# IDX default lot size (most names). 100 shares per lot.
LOT_SIZE = 100

# Capital assumption for the position-size hint. The user can override
# at the CLI layer; this is the default for the report.
DEFAULT_CAPITAL_IDR = 10_000_000  # Rp 10 juta

# Per-trade risk cap (the user's risk rule, matching the existing README).
RISK_PER_TRADE = 0.02  # 2% of capital


@dataclass
class IntradayPlan:
    """Structured next-day intraday trade plan for one ticker."""

    symbol: str
    direction: str               # "LONG", "SHORT", or "NEUTRAL (mean-reversion)"
    bias: str                    # "momentum" or "mean-reversion"
    entry_zone_low: float        # IDR
    entry_zone_high: float       # IDR
    stop_loss: float             # IDR
    tp1: float                   # IDR
    tp2: float                   # IDR
    risk_per_share: float        # IDR
    reward_per_share_tp1: float  # IDR
    reward_per_share_tp2: float  # IDR
    rr_ratio_tp1: float
    rr_ratio_tp2: float
    preferred_window: str        # human-readable, e.g. "09:00-10:30 WIB"
    position_lots: int           # round lots
    thesis: str                  # one-liner
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _round_to_tick(price: float) -> float:
    """Round a price to the nearest IDX tick."""
    if price < 200:
        return round(price)
    if price < 500:
        return round(price / 5) * 5
    if price < 2000:
        return round(price / 10) * 10
    if price < 5000:
        return round(price / 25) * 25
    return round(price / 50) * 50


def _direction(profile: IntradayProfile) -> tuple[str, str]:
    """Decide direction bias and trading style from the profile."""
    # Mega-cap / low-beta names (BBCA-class) -> mean-reversion.
    if profile.beta is not None and abs(profile.beta) < 0.20 and profile.avg_day_range_pct < 0.04:
        return "NEUTRAL (mean-reversion)", "mean-reversion"

    # Strong 1M move up -> momentum long into continuation.
    if profile.ret_1m > 0.05 and profile.sub_momentum >= 7.0:
        return "LONG", "momentum"

    # Strong 1M move down -> momentum short (IDX allows intraday short on
    # liquid names).
    if profile.ret_1m < -0.05 and profile.sub_momentum >= 7.0:
        return "SHORT", "momentum"

    # Otherwise, mild bias in the direction of the 1W move (shorter term).
    if profile.ret_1w > 0.02:
        return "LONG (mild)", "momentum"
    if profile.ret_1w < -0.02:
        return "SHORT (mild)", "momentum"

    return "NEUTRAL (mean-reversion)", "mean-reversion"


def _window(bias: str) -> str:
    if bias == "momentum":
        return "09:00 - 10:30 WIB (opening drive) / 13:30 - 14:30 WIB (post-lunch continuation)"
    return "10:00 - 11:30 WIB / 14:00 - 15:00 WIB (mid-session fade window)"


def _thesis(profile: IntradayProfile, direction: str, bias: str) -> str:
    if bias == "momentum":
        if "LONG" in direction:
            return (
                f"Trend follower: 1M return {profile.ret_1m*100:+.1f}%, "
                f"avg day range {profile.avg_day_range_pct*100:.1f}%, "
                f"ADV {profile.adv_3m/1e6:.0f} M sh — trade with the morning flow."
            )
        if "SHORT" in direction:
            return (
                f"Trend follower (short side): 1M return {profile.ret_1m*100:+.1f}%, "
                f"avg day range {profile.avg_day_range_pct*100:.1f}%, "
                f"ADV {profile.adv_3m/1e6:.0f} M sh — fade rallies into VWAP."
            )
    return (
        f"Mean-reversion: low beta {profile.beta if profile.beta is not None else 'n/a'}, "
        f"deep liquidity (Rp {profile.turnover_idr_3m/1e9:.1f} bn ADV), "
        f"tight spread {profile.spread_pct*100:.2f}% — fade deviations from VWAP."
    )


def generate_plan(
    profile: IntradayProfile,
    capital_idr: float = DEFAULT_CAPITAL_IDR,
) -> IntradayPlan:
    """Build a next-day intraday plan from an `IntradayProfile`.

    Entry zone = close +/- 0.5 * avg_day_range. SL = beyond the zone by
    another 0.5 * range. TP1 = +1.0 * range, TP2 = +2.0 * range (in the
    direction of the bias).
    """
    direction, bias = _direction(profile)
    p = profile.last_close
    rng_pct = max(profile.avg_day_range_pct, 0.01)  # floor 1% so low-vol names still have a plan
    half_range = p * (rng_pct / 2.0)

    if "LONG" in direction:
        entry_low = p - half_range
        entry_high = p
        sl = p - 2 * half_range
        tp1 = p + 1 * (p - sl)               # symmetric R-multiple of 1R up
        tp2 = p + 2 * (p - sl)
    elif "SHORT" in direction:
        entry_low = p
        entry_high = p + half_range
        sl = p + 2 * half_range
        tp1 = p - 1 * (sl - p)
        tp2 = p - 2 * (sl - p)
    else:
        # Mean-reversion: fade either side back to VWAP (= close).
        entry_low = p - half_range
        entry_high = p + half_range
        sl_up = p + 2 * half_range
        sl_dn = p - 2 * half_range
        # Default plan is a fade from the upper band (short) since that's
        # the more common intraday setup.
        sl = sl_up
        tp1 = p
        tp2 = p - 1 * (sl - p)

    # Snap to ticks.
    entry_low = _round_to_tick(entry_low)
    entry_high = _round_to_tick(entry_high)
    sl = _round_to_tick(sl)
    tp1 = _round_to_tick(tp1)
    tp2 = _round_to_tick(tp2)

    # Per-share risk and reward.
    if "LONG" in direction or direction.startswith("NEUTRAL"):
        risk_per_share = abs(entry_high - sl)
    else:
        risk_per_share = abs(sl - entry_low)

    if "LONG" in direction:
        reward_tp1 = tp1 - entry_low
        reward_tp2 = tp2 - entry_low
    elif "SHORT" in direction:
        reward_tp1 = entry_high - tp1
        reward_tp2 = entry_high - tp2
    else:  # neutral
        reward_tp1 = entry_high - tp1   # if we fade from the upper band
        reward_tp2 = entry_high - tp2

    rr_tp1 = reward_tp1 / risk_per_share if risk_per_share > 0 else 0.0
    rr_tp2 = reward_tp2 / risk_per_share if risk_per_share > 0 else 0.0

    # Position sizing: 2% risk rule, integer lots.
    risk_budget = capital_idr * RISK_PER_TRADE
    risk_per_lot = risk_per_share * LOT_SIZE
    lots = int(risk_budget // risk_per_lot) if risk_per_lot > 0 else 0
    # Cap to 1 lot for any name with R/R < 1.0 (don't risk capital on bad plans).
    if rr_tp1 < 1.0 and lots > 0:
        notes = [f"R/R to TP1 = {rr_tp1:.2f} (< 1.0) — plan is informational only."]
        lots = 0
    else:
        notes = []
    if profile.avg_day_range_pct < 0.015:
        notes.append("Avg day range < 1.5% — low payoff, prefer to skip.")
        lots = 0

    thesis = _thesis(profile, direction, bias)

    return IntradayPlan(
        symbol=profile.symbol,
        direction=direction,
        bias=bias,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        risk_per_share=risk_per_share,
        reward_per_share_tp1=reward_tp1,
        reward_per_share_tp2=reward_tp2,
        rr_ratio_tp1=rr_tp1,
        rr_ratio_tp2=rr_tp2,
        preferred_window=_window(bias),
        position_lots=lots,
        thesis=thesis,
        notes=notes,
    )


__all__ = ["IntradayPlan", "generate_plan", "LOT_SIZE", "DEFAULT_CAPITAL_IDR"]
