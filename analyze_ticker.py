"""Screen a candidate ticker: durability gate + strategy backtest + peer comparison.

For one ticker, this:
  1. classifies its durability (the fundamentals gate, via Massive or yfinance),
  2. backtests the volatility-harvesting strategy on its price history,
  3. drops its row into the cached portfolio reference set (strategy_backtest) and
     names its closest behavioral analogs ("behaves like QS" vs "like INTC").

Run backtest_strategy.py first to populate the reference set you compare against.

Usage:
    python analyze_ticker.py <massive-api-key-file> <TICKER> [--years 3] [--db X]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from backtest_strategy import (
    TABLE_HEADER, durability_classes, fetch_window_bars, fmt_table_row,
    load_api_key, load_backtest, run_backtest,
)
from durability_core import CONFIG, compute
from strategy_sim import SimParams

HERE = Path(__file__).resolve().parent

# Behavioral fingerprint axes for the nearest-analog match.
FINGERPRINT = ("atr_pct", "max_drawdown", "harvest_per_year", "bh_return", "edge_vs_hold")


def classify_durability(ticker, api_key, years):
    """Return (metrics_dict, source) using Massive, then yfinance as fallback."""
    import build_durability as bd
    try:
        details = bd.fetch_details(ticker, api_key)
        if (details.get("type") or "").upper() not in bd.ETF_TYPES:
            reports = bd.fetch_financials(ticker, api_key, years=4)
            if reports:
                return compute(details, reports, CONFIG, None), "Massive"
    except Exception as e:
        print(f"  (Massive durability fetch failed: {e})")
    try:
        import build_durability_yf as yf
        details, reports = yf.fetch_reports(ticker, 4)
        if details.get("quote_type") in yf.SKIP_QUOTE_TYPES:
            return None, "ETF/fund"
        if reports:
            return compute(details, reports, CONFIG, None), "yfinance"
    except Exception as e:
        print(f"  (yfinance durability fetch failed: {e})")
    return None, "none"


def nearest_analogs(candidate, ref, k=3):
    """Top-k held names closest to the candidate by fingerprint distance.

    Uses robust (median / MAD) scaling so a single 50-bagger like HGRAF doesn't
    dominate the normalization. `ref` should already exclude ETFs/unclassified."""
    import statistics
    if not ref:
        return []
    scalers = {}
    for ax in FINGERPRINT:
        vals = [row.get(ax) for row in ref.values() if row.get(ax) is not None]
        if not vals:
            scalers[ax] = (0.0, 1.0)
            continue
        med = statistics.median(vals)
        scale = 1.4826 * statistics.median([abs(v - med) for v in vals])   # MAD -> ~stdev
        if scale < 1e-9:
            scale = statistics.pstdev(vals) or 1.0
        scalers[ax] = (med, scale)

    def z(row):
        return [((row.get(ax) or 0.0) - scalers[ax][0]) / scalers[ax][1] for ax in FINGERPRINT]

    cz = z(candidate)
    dists = [(sum((a - b) ** 2 for a, b in zip(cz, z(row))) ** 0.5, sym)
             for sym, row in ref.items()]
    return sorted(dists)[:k]


def main():
    ap = argparse.ArgumentParser(description="Screen a candidate ticker against the strategy + portfolio.")
    ap.add_argument("key_file")
    ap.add_argument("ticker")
    ap.add_argument("--years", type=float, default=3.0, help="Backtest window in years")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    args = ap.parse_args()

    api_key = load_api_key(args.key_file)
    t = args.ticker.upper()
    conn = sqlite3.connect(args.db)

    # 1) durability
    dur, dsource = classify_durability(t, api_key, args.years)
    print(f"\n=== {t} ===\n")
    if dur:
        print(f"Durability ({dsource}): {dur['class']}  score {dur['score']:.1f}"
              + (f"  vetoes: {dur['vetoes']}" if dur["vetoes"] else "")
              + (f"  [market cap ${dur['market_cap']/1e9:.2f}B]" if dur.get("market_cap") else ""))
    else:
        print(f"Durability: unclassified ({dsource})")

    # 2) strategy backtest
    try:
        bars = fetch_window_bars(t, api_key, args.years)
    except Exception as e:
        sys.exit(f"price fetch failed: {e}")
    if not bars:
        sys.exit("no price data")
    r = run_backtest(t, bars, args.years, SimParams())
    print(f"\nStrategy backtest ({r.start_date} -> {r.end_date}, {r.years:g}y):")
    print(f"  return on capital {r.return_on_capital*100:+.1f}%   buy-and-hold {r.bh_return*100:+.1f}%"
          f"   edge {r.edge_vs_hold*100:+.1f}%")
    print(f"  {r.buys} buys / {r.sells} sells ({r.harvest_per_year:.1f}/yr)   "
          f"max drawdown {r.max_drawdown*100:.1f}%   "
          + (f"risk-adjusted {r.risk_adjusted:.2f}" if r.risk_adjusted is not None else "risk-adjusted -"))
    print(f"  max capital deployed {r.max_capital:,.0f}   "
          f"realized {r.realized_pnl:,.0f}   unrealized {r.unrealized_pnl:,.0f}")

    # 3) portfolio comparison + analogs
    ref = load_backtest(conn)
    ref.pop(t, None)                       # don't compare a held candidate to itself
    dclasses = durability_classes(conn)
    print("\nPortfolio comparison (candidate marked '>'):\n")
    print(TABLE_HEADER)
    print(fmt_table_row(t, r, dur["class"] if dur else None, marker=">"))
    for sym in sorted(ref, key=lambda s: ref[s].get("return_on_capital") or 0, reverse=True):
        print(fmt_table_row(sym, ref[sym], dclasses.get(sym)))

    analog_ref = {s: ref[s] for s in ref if s in dclasses}   # durability-classified only (drops ETFs)
    analogs = nearest_analogs({k: getattr(r, k) for k in FINGERPRINT}, analog_ref)
    if analogs:
        print("\nClosest behavioral analogs: "
              + ", ".join(f"{sym} (dist {d:.2f})" for d, sym in analogs))
    else:
        print("\n(no reference set yet - run backtest_strategy.py to populate it)")
    conn.close()


if __name__ == "__main__":
    main()
