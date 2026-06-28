"""Render the backtest report from current DB state (no new fetches)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path("E:/project/stockai").resolve()))

from datetime import date
from stockai.intraday.backtest import (
    load_persisted_trades, aggregate, render_backtest_report
)

trades = load_persisted_trades()
print(f"Loaded {len(trades)} trades from DB")
agg = aggregate(trades)
md = render_backtest_report(trades=trades, agg=agg)
out = Path(f"E:/project/stockai/reports/intraday_backtest_{date.today().isoformat()}.md")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(md, encoding="utf-8")
print(f"Wrote {out}  ({len(md):,} chars)")
print()
print("=== Summary ===")
o = agg["overall"]
print(f"  N trades       : {o['n']}")
print(f"  Win rate       : {o['win_rate']*100:.1f}%")
print(f"  Avg R          : {o['avg_r']:+.2f}R")
print(f"  Total P&L/lot  : Rp {o['total_pnl_per_lot']:,.0f}")
print(f"  TP2/TP1/SL/EOD : {o['n_tp2']}/{o['n_tp1']}/{o['n_sl']}/{o['n_eod']}")
