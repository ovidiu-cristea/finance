"""Pull historical daily bars from Massive and compute per-stock price metrics.

For each ticker held in the DB (distinct open tax lots), fetches ~--days of daily
bars and stores into the `stock_metrics` table: 52-week high, drawdown from it,
200-day MA + slope, ATR%, and consecutive days closed below the 200-day MA.
Feeds the buy-side guardrails (drawdown-scaled sizing, 200-day-MA breaker) and the
terminal-risk signals.

Usage:
    python build_metrics.py <massive-api-key-file> [--db X] [--days 420]
                            [--delay 12] [--ticker QS]

--delay is seconds between API calls (rate limit). A full ~60-ticker run at the
default 12s takes ~12 min; lower it if your Massive plan allows.
"""
import argparse
import datetime
import sqlite3
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
BASE_URL = "https://api.massive.com"
TRADING_YEAR = 252   # ~trading days in 52 weeks
MA_WINDOW = 200
SLOPE_LOOKBACK = 20  # trading days back to measure MA slope
ATR_WINDOW = 14


def load_api_key(path):
    return Path(path).read_text(encoding="utf-8-sig").strip()


def bar_date(b):
    if "t" not in b:
        return None
    return datetime.datetime.fromtimestamp(
        b["t"] / 1000, datetime.timezone.utc).date().isoformat()


def held_tickers(db_path):
    rows = sqlite3.connect(db_path).execute(
        "SELECT DISTINCT symbol FROM tax_lots WHERE status = 'open'").fetchall()
    return sorted(r[0] for r in rows if r[0])


def fetch_bars(ticker, api_key, days):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days)
    url = (f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/"
           f"{start.isoformat()}/{today.isoformat()}")
    r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"},
                     params={"adjusted": "true", "sort": "asc", "limit": 50000}, timeout=30)
    r.raise_for_status()
    return r.json().get("results") or []


def rolling_sma(values, window):
    """SMA list, None until `window` values have been seen."""
    out, total = [], 0.0
    for i, v in enumerate(values):
        total += v
        if i >= window:
            total -= values[i - window]
        out.append(total / window if i >= window - 1 else None)
    return out


def compute_metrics(bars):
    """bars: list of dicts (o/h/l/c/t), ascending by date. Returns metrics dict or None."""
    bars = [b for b in bars if b.get("c") is not None]
    if not bars:
        return None
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    n = len(bars)
    last = closes[-1]

    high_52w = max(b["h"] for b in bars[-TRADING_YEAR:])
    drawdown_pct = (last / high_52w - 1) * 100 if high_52w else None

    ma_n = min(MA_WINDOW, n)
    ma_200 = sum(closes[-ma_n:]) / ma_n
    ma_slope = None
    if n >= ma_n + SLOPE_LOOKBACK:
        ma_prev = sum(closes[-ma_n - SLOPE_LOOKBACK:-SLOPE_LOOKBACK]) / ma_n
        ma_slope = ma_200 - ma_prev

    atr_pct = None
    if n >= ATR_WINDOW + 1 and last:
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                   abs(lows[i] - closes[i - 1])) for i in range(n - ATR_WINDOW, n)]
        atr_pct = (sum(trs) / ATR_WINDOW) / last * 100

    sma = rolling_sma(closes, ma_n)
    below = 0
    for i in range(n - 1, -1, -1):
        if sma[i] is None or closes[i] >= sma[i]:
            break
        below += 1

    return dict(as_of=bar_date(bars[-1]), last_close=last, high_52w=high_52w,
                drawdown_pct=drawdown_pct, ma_200=ma_200, ma_200_slope=ma_slope,
                atr_pct=atr_pct, below_ma_days=below, bars=n)


def upsert_metrics(conn, symbol, m):
    conn.execute(
        """
        INSERT INTO stock_metrics
            (symbol, as_of, last_close, high_52w, drawdown_pct, ma_200,
             ma_200_slope, atr_pct, below_ma_days, bars, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            as_of=excluded.as_of, last_close=excluded.last_close,
            high_52w=excluded.high_52w, drawdown_pct=excluded.drawdown_pct,
            ma_200=excluded.ma_200, ma_200_slope=excluded.ma_200_slope,
            atr_pct=excluded.atr_pct, below_ma_days=excluded.below_ma_days,
            bars=excluded.bars, computed_at=excluded.computed_at
        """,
        (symbol, m["as_of"], m["last_close"], m["high_52w"], m["drawdown_pct"],
         m["ma_200"], m["ma_200_slope"], m["atr_pct"], m["below_ma_days"], m["bars"],
         datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")),
    )


def main():
    ap = argparse.ArgumentParser(description="Build per-stock price metrics from Massive history.")
    ap.add_argument("key_file")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--days", type=int, default=420, help="Calendar days of history to pull")
    ap.add_argument("--delay", type=float, default=12.0, help="Seconds between API calls")
    ap.add_argument("--ticker", help="Only this ticker (for testing)")
    args = ap.parse_args()

    api_key = load_api_key(args.key_file)
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")

    tickers = [args.ticker.upper()] if args.ticker else held_tickers(args.db)
    print(f"Computing metrics for {len(tickers)} ticker(s)...\n")
    print(f"  {'ticker':<8} {'close':>9} {'52wk hi':>9} {'draw%':>7} "
          f"{'200dMA':>9} {'slope':>7} {'atr%':>6} {'<MA d':>6}")
    ok = 0
    for i, t in enumerate(tickers):
        if i:
            time.sleep(args.delay)
        try:
            bars = fetch_bars(t, api_key, args.days)
        except Exception as e:
            print(f"  {t:<8} fetch error: {e}")
            continue
        m = compute_metrics(bars)
        if not m:
            print(f"  {t:<8} no data")
            continue
        upsert_metrics(conn, t, m)
        conn.commit()
        ok += 1
        print(f"  {t:<8} {m['last_close']:>9,.2f} {m['high_52w']:>9,.2f} "
              f"{(m['drawdown_pct'] or 0):>7.1f} {m['ma_200']:>9,.2f} "
              f"{(m['ma_200_slope'] or 0):>+7.2f} {(m['atr_pct'] or 0):>6.1f} "
              f"{m['below_ma_days']:>6}")
    conn.close()
    print(f"\nStored metrics for {ok}/{len(tickers)} ticker(s) in stock_metrics.")


if __name__ == "__main__":
    main()
