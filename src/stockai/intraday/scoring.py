"""Intraday scoring engine.

Replicates the methodology of the May 2026 intraday report (see
`reports/idx_intraday_202605.md`):

    intraday_score (0-10) =
        0.30 * liquidity_score     (3M average daily volume, 0-10)
      + 0.30 * volatility_score    (avg daily range %, 0-10)
      + 0.15 * spread_score        (proxy: inverse of close price tier, 0-10)
      + 0.15 * beta_score          (|beta to IHSG|, 0-10)
      + 0.10 * momentum_score      (last 1M return magnitude, 0-10)

All sub-scores are normalized to 0-10 using static, well-defined brackets so
the ranking is deterministic and easy to explain in the generated report.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np
import pandas as pd

from stockai.data.sources.yahoo import get_yahoo_source
from stockai.data.sources.idx import get_lq45


# ---------------------------------------------------------------------------
# Static reference data
# ---------------------------------------------------------------------------

# Sector map for the LQ45 universe. Used to compute "sector activity" bonus
# and to label the generated report. Kept intentionally small (subset of LQ45
# is enough to cover ~95% of trading volume).
SECTOR_MAP: dict[str, str] = {
    "BBCA": "Financials / Banks",
    "BBRI": "Financials / Banks",
    "BMRI": "Financials / Banks",
    "BBNI": "Financials / Banks",
    "BRIS": "Financials / Banks",
    "BBTN": "Financials / Banks",
    "ARTO": "Financials / Digital Bank",
    "BREN": "Energy / Renewables",
    "BRPT": "Basic Materials / Petrochem",
    "AMMN": "Basic Materials / Mining (copper-gold)",
    "PTRO": "Basic Materials / Mining services",
    "TINS": "Basic Materials / Mining (tin)",
    "INCO": "Basic Materials / Mining (nickel)",
    "ANTM": "Basic Materials / Mining (gold)",
    "MDKA": "Basic Materials / Mining (gold-copper)",
    "ASII": "Industrials / Conglomerate",
    "UNTR": "Industrials / Heavy equipment",
    "TLKM": "Communication Services / Telecom",
    "ISAT": "Communication Services / Telecom",
    "EXCL": "Communication Services / Telecom",
    "TOWR": "Infrastructure / Tower",
    "TBIG": "Infrastructure / Tower",
    "AMRT": "Consumer / Retail",
    "ICBP": "Consumer / FMCG",
    "INDF": "Consumer / FMCG",
    "UNVR": "Consumer / FMCG",
    "KLBF": "Healthcare / Pharma",
    "GGRM": "Consumer / Tobacco",
    "HMSP": "Consumer / Tobacco",
    "ACES": "Consumer / Retail (building)",
    "MAPI": "Consumer / Retail",
    "JPFA": "Consumer / Poultry",
    "CPIN": "Consumer / Poultry",
    "SMGR": "Basic Materials / Cement",
    "INTP": "Basic Materials / Cement",
    "ESSA": "Energy / Gas",
    "PGAS": "Energy / Gas distribution",
    "AKRA": "Energy / Distribution",
    "JSMR": "Infrastructure / Toll roads",
    "MNCN": "Communication / Media",
    "MTEL": "Infrastructure / Tower",
    "SIDO": "Healthcare / Pharma",
    "ERAA": "Consumer / Electronics retail",
    "MYOR": "Consumer / FMCG",
    "CMRY": "Consumer / Tobacco",
    "HRUM": "Basic Materials / Mining (coal)",
    "ITMG": "Basic Materials / Mining (coal)",
    "ADRO": "Basic Materials / Mining (coal)",
    "PTBA": "Basic Materials / Mining (coal)",
}

# IDX tick size table (IDX regulation, Rp per share). Used to compute
# spread-cost score. For prices < 200 IDR the tick is 1; for higher prices
# the tick is per the schedule below. We use a single representative tick
# of 1 IDR for <200, 5 for 200-499, 10 for 500-1999, 25 for 2000-4999,
# 50 for >= 5000. This is a simplification but accurate enough for the
# relative spread score.
def _idrx_tick(price: float) -> int:
    if price < 200:
        return 1
    if price < 500:
        return 5
    if price < 2000:
        return 10
    if price < 5000:
        return 25
    return 50


# ---------------------------------------------------------------------------
# Bracket / normalization tables (each maps raw value to a 0-10 sub-score)
# ---------------------------------------------------------------------------

# Liquidity: average daily volume in shares (3M window). Brackets chosen so
# BREN/AMMN/BRPT land in 8-10 and small caps land in 0-2.
LIQUIDITY_BRACKETS: list[tuple[float, float]] = [
    (0,      5_000_000,   0.0),
    (5e6,    20_000_000,  3.0),
    (20e6,   50_000_000,  5.5),
    (50e6,   100_000_000, 7.0),
    (100e6,  200_000_000, 8.5),
    (200e6,  float("inf"), 10.0),
]

# Volatility: average daily range (High-Low) as % of close. The May report
# shows BREN/AMMN at ~8-10% and BBCA at ~2-3%. We anchor on those.
VOLATILITY_BRACKETS: list[tuple[float, float]] = [
    (0.0,  0.015, 0.0),
    (0.015, 0.025, 3.0),
    (0.025, 0.04,  5.0),
    (0.04,  0.06,  7.0),
    (0.06,  0.08,  8.5),
    (0.08,  float("inf"), 10.0),
]

# Spread: bid-ask spread as fraction of mid price. yfinance doesn't reliably
# return the live IDX bid/ask, so we approximate using the IDX tick-size
# table: spread_pct ~= 2 * tick / price. Lower is better.
SPREAD_BRACKETS: list[tuple[float, float]] = [
    (0.0,    0.001,  10.0),  # < 0.1%  (mega caps, tick 50 on 10k price)
    (0.001,  0.002,   9.0),
    (0.002,  0.004,   7.5),
    (0.004,  0.008,   6.0),
    (0.008,  0.015,   4.0),
    (0.015,  float("inf"), 2.0),
]

# Beta: |beta to IHSG| over a 5Y monthly window. yfinance returns
# `beta` from the .info dict. Higher absolute beta = more "with the tape".
# We treat low-beta names (mega-cap banks) as a 6.0 baseline because they
# are useful as mean-reversion vehicles even without directional beta.
def _beta_subscore(beta: float | None) -> float:
    if beta is None or np.isnan(beta):
        return 5.0  # unknown -> middle
    b = abs(beta)
    if b < 0.10:
        return 6.0  # mean-reversion / VWAP vehicle
    if b < 0.25:
        return 6.5
    if b < 0.50:
        return 7.5
    if b < 0.80:
        return 8.5
    if b < 1.20:
        return 9.5
    return 8.0  # > 1.2 = jumpy, still good but more dangerous


# Momentum: magnitude of the 1M (21-trading-day) return. We reward both
# directions (trending names are intraday-friendly), but penalize names
# that moved too much (>25% in 1M = exhaustion / risk).
def _momentum_subscore(ret_1m: float) -> float:
    r = abs(ret_1m)
    if r < 0.02:
        return 2.0
    if r < 0.05:
        return 5.0
    if r < 0.10:
        return 7.0
    if r < 0.20:
        return 9.0
    if r < 0.30:
        return 8.0  # hot but risky
    return 6.0  # > 30% = exhaustion


def _bracket_score(value: float, brackets: list[tuple[float, float, float]]) -> float:
    """value -> 0-10 score from a list of (low, high, score) brackets."""
    for low, high, score in brackets:
        if low <= value < high:
            return score
    return 0.0


# ---------------------------------------------------------------------------
# Profile dataclass
# ---------------------------------------------------------------------------

@dataclass
class IntradayProfile:
    """Per-ticker intraday snapshot used by the screen, plan, and report."""

    symbol: str
    sector: str
    last_close: float
    adv_3m: float               # shares, 3M average daily volume
    turnover_idr_3m: float      # IDR, 3M average daily turnover
    day_range_pct: float        # last session (H-L)/C
    avg_day_range_pct: float    # 3M average (H-L)/C
    spread_pct: float           # 2*tick/close
    beta: float | None
    ret_1m: float
    ret_1w: float
    sub_liquidity: float
    sub_volatility: float
    sub_spread: float
    sub_beta: float
    sub_momentum: float
    score: float
    notes: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core data fetcher
# ---------------------------------------------------------------------------

def _fetch_profile(symbol: str) -> IntradayProfile | None:
    """Fetch 3M daily data + 1M / 1W / last close for one ticker."""
    src = get_yahoo_source()
    sym = symbol.upper().replace(".JK", "")

    # Use a 6M window so the 3M average is stable.
    hist = src.get_price_history(sym, period="6mo", interval="1d")
    if hist.empty or len(hist) < 20:
        return None

    hist = hist.sort_values("date").reset_index(drop=True)
    last = hist.iloc[-1]
    last_close = float(last["close"])

    # 3M window (we use last ~63 trading days as a robust proxy).
    hist_3m = hist.tail(63)
    adv_3m = float(hist_3m["volume"].mean())
    turnover_idr_3m = adv_3m * last_close

    # Day range: last session + 3M average.
    day_range_pct = float((last["high"] - last["low"]) / last["close"]) if last["close"] else 0.0
    hist_3m = hist_3m.copy()
    hist_3m["_range_pct"] = (hist_3m["high"] - hist_3m["low"]) / hist_3m["close"]
    avg_day_range_pct = float(hist_3m["_range_pct"].mean())

    # Returns
    ret_1m = float((last["close"] / hist_3m.iloc[0]["close"]) - 1.0) if len(hist_3m) > 0 else 0.0
    ret_1w = float((last["close"] / hist.iloc[-6]["close"]) - 1.0) if len(hist) >= 6 else ret_1m

    # Spread proxy from IDX tick size.
    spread_pct = (2 * _idrx_tick(last_close)) / last_close if last_close else 1.0

    # Beta from yfinance .info (5Y monthly). May be missing for some tickers.
    beta: float | None = None
    try:
        info = src.get_stock_info(sym) or {}
        b = info.get("beta")
        if b is not None and not (isinstance(b, float) and np.isnan(b)):
            beta = float(b)
    except Exception:
        beta = None

    sub_liq = _bracket_score(adv_3m, LIQUIDITY_BRACKETS)
    sub_vol = _bracket_score(avg_day_range_pct, VOLATILITY_BRACKETS)
    sub_spr = _bracket_score(spread_pct, SPREAD_BRACKETS)
    sub_bet = _beta_subscore(beta)
    sub_mom = _momentum_subscore(ret_1m)

    score = (
        0.30 * sub_liq
        + 0.30 * sub_vol
        + 0.15 * sub_spr
        + 0.15 * sub_bet
        + 0.10 * sub_mom
    )

    sector = SECTOR_MAP.get(sym, "Other / Unclassified")

    return IntradayProfile(
        symbol=sym,
        sector=sector,
        last_close=last_close,
        adv_3m=adv_3m,
        turnover_idr_3m=turnover_idr_3m,
        day_range_pct=day_range_pct,
        avg_day_range_pct=avg_day_range_pct,
        spread_pct=spread_pct,
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def score_universe(
    universe: list[str] | None = None,
    top_n: int = 5,
    min_score: float = 0.0,
) -> list[IntradayProfile]:
    """Score a list of tickers and return the top_n by intraday score.

    Args:
        universe: list of bare tickers (e.g. ["BBCA", "BREN"]). If None, uses
                 the LQ45 universe from the existing data layer.
        top_n:    how many top profiles to return.
        min_score: drop profiles below this composite score.

    Returns:
        list[IntradayProfile] sorted descending by score.

    Notes:
        Network calls (yfinance). Cached at the source layer; expect 30-90s
        for the full LQ45 universe on a typical residential connection.
    """
    if universe is None:
        universe = get_lq45()

    profiles: list[IntradayProfile] = []
    for sym in universe:
        try:
            p = _fetch_profile(sym)
        except Exception as e:  # noqa: BLE001
            # One bad ticker should not blow up the whole screen.
            p = None
            print(f"[intraday] {sym} skipped: {e}")
        if p is not None and p.score >= min_score:
            profiles.append(p)

    profiles.sort(key=lambda p: p.score, reverse=True)
    return profiles[:top_n]


__all__ = [
    "IntradayProfile",
    "score_universe",
    "SECTOR_MAP",
]
