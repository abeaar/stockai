"""Seed backdated plans for validation of the intraday evaluate/stats loop.

Generates plans for 5 IDX30 names across 3 historical dates (~1, 2, 3 weeks
ago), then exits. Use only for the dev/QA workflow, not in production.
"""
from datetime import date, datetime, timedelta

from stockai.data.sources.yahoo import get_yahoo_source
from stockai.intraday.scoring import (
    IntradayProfile, SECTOR_MAP, _bracket_score, _beta_subscore,
    _momentum_subscore, _idrx_tick, LIQUIDITY_BRACKETS, VOLATILITY_BRACKETS,
    SPREAD_BRACKETS,
)
from stockai.intraday.planner import generate_plan
from stockai.intraday.storage import save_plan


def main() -> None:
    src = get_yahoo_source()
    tickers = ["BRPT", "BMRI", "AMMN", "BBCA", "INCO"]
    today = date.today()
    n_saved = 0
    for weeks_ago in [3, 2, 1]:
        plan_date = today - timedelta(weeks=weeks_ago)
        for sym in tickers:
            try:
                hist = src.get_price_history(sym, period="6mo", interval="1d")
                if hist.empty or len(hist) < 20:
                    print(f"  skip {sym}: no history")
                    continue
                hist = hist.sort_values("date").reset_index(drop=True)
                if hist["date"].dt.tz is not None:
                    hist["date"] = hist["date"].dt.tz_localize(None)
                target = datetime.combine(plan_date, datetime.min.time())
                closest_idx = (hist["date"] - target).abs().idxmin()
                hist = hist.iloc[: closest_idx + 1].copy()
                if len(hist) < 20:
                    print(f"  skip {sym} @ {plan_date}: not enough history")
                    continue
                last_close = float(hist.iloc[-1]["close"])
                hist_3m = hist.tail(63)
                adv_3m = float(hist_3m["volume"].mean())
                turnover = adv_3m * last_close
                hist_3m = hist_3m.copy()
                hist_3m["_range_pct"] = (
                    (hist_3m["high"] - hist_3m["low"]) / hist_3m["close"]
                )
                avg_day_range = float(hist_3m["_range_pct"].mean())
                ret_1m = float(
                    (last_close / hist_3m.iloc[0]["close"]) - 1.0
                ) if len(hist_3m) else 0.0
                ret_1w = float(
                    (last_close / hist.iloc[-6]["close"]) - 1.0
                ) if len(hist) >= 6 else ret_1m
                spread = (2 * _idrx_tick(last_close)) / last_close
                beta = None
                try:
                    info = src.get_stock_info(sym) or {}
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
                profile = IntradayProfile(
                    symbol=sym,
                    sector=SECTOR_MAP.get(sym, "Other"),
                    last_close=last_close,
                    adv_3m=adv_3m,
                    turnover_idr_3m=turnover,
                    day_range_pct=float(
                        (hist.iloc[-1]["high"] - hist.iloc[-1]["low"])
                        / hist.iloc[-1]["close"]
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
                plan = generate_plan(profile, capital_idr=10_000_000)
                save_plan(plan, profile, plan_date=plan_date, report_id="seed-test")
                n_saved += 1
                print(
                    f"  saved {sym} @ {plan_date} "
                    f"(close=Rp {last_close:,.0f}, score={score:.2f})"
                )
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL {sym} @ {plan_date}: {e}")
    print(f"\nTotal saved: {n_saved} plan(s)")


if __name__ == "__main__":
    main()
