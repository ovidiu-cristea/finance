"""Backtest the strategy across held names -> the `strategy_backtest` reference set.

For each held ticker, pulls price history from Massive, replays the strategy
(strategy_sim.simulate) over the last --years, and stores the metrics. This is
both the portfolio backtest (how the strategy did on what you own) and the
reference set the candidate screener (analyze_ticker.py) compares against.

Exposes the shared helpers (fetch_window_bars, run_backtest, upsert_backtest,
load_backtest, table formatting) that analyze_ticker.py reuses.

Usage:
    python backtest_strategy.py <massive-api-key-file> [--ticker QS] [--years 3]
                                [--delay 12] [--base-shares 100] [--db X]
"""
import argparse
import datetime
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from build_metrics import fetch_bars
from strategy_sim import SimParams, simulate, window_start_index

HERE = Path(__file__).resolve().parent

# strategy_backtest columns, in SimResult attribute order (excludes events).
BT_COLS = ("start_date", "end_date", "years", "buys", "sells", "total_invested",
           "max_capital", "realized_pnl", "unrealized_pnl", "total_pnl",
           "return_on_capital", "bh_return", "edge_vs_hold", "max_drawdown",
           "risk_adjusted", "harvest_per_year", "atr_pct", "final_drawdown")

TABLE_HEADER = (f"  {'ticker':<8} {'dur':<10} {'ret%':>7} {'BH%':>7} {'edge%':>7} "
                f"{'maxDD%':>7} {'riskAdj':>7} {'harv/yr':>7} {'atr%':>6} {'b/s':>7}")


def load_api_key(path):
    return Path(path).read_text(encoding="utf-8-sig").strip()


def held_tickers(db_path):
    rows = sqlite3.connect(db_path).execute(
        "SELECT DISTINCT symbol FROM tax_lots WHERE status = 'open'").fetchall()
    return sorted(r[0] for r in rows if r[0])


def fetch_window_bars(ticker, api_key, years):
    """~years of window + ~1y lookback so the 52wk-high/200d-MA are valid on day 1."""
    days = round(years * 365.25) + 420
    return fetch_bars(ticker, api_key, days)


def run_backtest(ticker, bars, years, params):
    """Replay the strategy over the last `years` of `bars`. Returns a SimResult."""
    ws = window_start_index(bars, years)
    return simulate(bars, ws, params, ticker=ticker)


def upsert_backtest(conn, r):
    d = asdict(r)
    vals = [d[c] for c in BT_COLS]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in BT_COLS)
    conn.execute(
        f"""INSERT INTO strategy_backtest (symbol, {", ".join(BT_COLS)}, computed_at)
            VALUES ({", ".join("?" * (len(BT_COLS) + 2))})
            ON CONFLICT(symbol) DO UPDATE SET {set_clause}, computed_at=excluded.computed_at""",
        (r.ticker, *vals, now),
    )


def load_backtest(conn):
    conn.row_factory = sqlite3.Row
    try:
        return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM strategy_backtest")}
    except sqlite3.OperationalError:
        return {}


def durability_classes(conn):
    try:
        return dict(conn.execute("SELECT symbol, class FROM durability"))
    except sqlite3.OperationalError:
        return {}


def fmt_table_row(symbol, m, dur_class, marker=" "):
    """One comparison-table line. `m` is a SimResult or a dict of its fields."""
    g = (lambda k: getattr(m, k)) if hasattr(m, "ticker") else (lambda k: m.get(k))
    ra = g("risk_adjusted")
    ras = f"{ra:.2f}" if ra is not None else "-"
    return (f"{marker} {symbol:<8} {(dur_class or '?'):<10} "
            f"{g('return_on_capital')*100:>7.1f} {g('bh_return')*100:>7.1f} "
            f"{g('edge_vs_hold')*100:>7.1f} {g('max_drawdown')*100:>7.1f} {ras:>7} "
            f"{g('harvest_per_year'):>7.1f} {g('atr_pct'):>6.1f} "
            f"{g('buys'):>3.0f}/{g('sells'):<3.0f}")


def main():
    ap = argparse.ArgumentParser(description="Backtest the strategy across held names.")
    ap.add_argument("key_file")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--ticker", help="Only this ticker (for testing)")
    ap.add_argument("--years", type=float, default=3.0, help="Window length in years")
    ap.add_argument("--base-shares", type=float, default=100.0, help="Normalized base lot size")
    ap.add_argument("--delay", type=float, default=12.0, help="Seconds between API calls")
    args = ap.parse_args()

    api_key = load_api_key(args.key_file)
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")
    dclasses = durability_classes(conn)
    params = SimParams(base_shares=args.base_shares)

    tickers = [args.ticker.upper()] if args.ticker else held_tickers(args.db)
    print(f"Backtesting {len(tickers)} ticker(s) over {args.years:g}y...\n")
    print(TABLE_HEADER)
    for i, t in enumerate(tickers):
        if i:
            time.sleep(args.delay)
        try:
            bars = fetch_window_bars(t, api_key, args.years)
        except Exception as e:
            print(f"  {t:<8} fetch error: {e}")
            continue
        if not bars:
            print(f"  {t:<8} no price data")
            continue
        r = run_backtest(t, bars, args.years, params)
        upsert_backtest(conn, r)
        conn.commit()
        print(fmt_table_row(t, r, dclasses.get(t)))
    conn.close()
    print("\nStored in strategy_backtest (the reference set for analyze_ticker.py).")


if __name__ == "__main__":
    main()
