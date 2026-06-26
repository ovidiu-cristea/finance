"""Build the durability whitelist from Massive fundamentals.

For each held ticker, pulls company details (market cap) and the last few annual
financial statements from Massive, then classifies the name's ability to survive
long enough for the mean-reversion strategy to pay off:

    ELIGIBLE   - buying / re-entry open
    HOLD_ONLY  - hold existing lots, do not add, do not force-exit
    TERMINAL   - a hard veto tripped: stop buying + recommend exit

Scoring/veto logic and CONFIG live in durability_core.py (shared with the yfinance
fetcher). This module only does the Massive HTTP calls. Results are upserted into
`durability`. The `manual_flag` column (set by you in DB Browser) is read as a
hard veto and PRESERVED across rebuilds.

Massive only parses US-GAAP filings, so foreign ADRs come back `no financials`.
Pass `--fallback-yf` to fill those gaps from Yahoo Finance after the Massive pass
(requires `pip install yfinance`; Massive stays authoritative - yfinance only
classifies names Massive couldn't).

Usage:
    python build_durability.py <massive-api-key-file> [--db X] [--ticker QS]
                               [--years 4] [--delay 12] [--fallback-yf]
"""
import argparse
import sqlite3
import time
from pathlib import Path

import requests

from durability_core import (
    CONFIG, HEADER, compute, fmt_row, held_tickers, load_flags, mark_etf, upsert)

HERE = Path(__file__).resolve().parent
BASE_URL = "https://api.massive.com"
ETF_TYPES = {"ETF", "ETN", "ETV", "FUND"}   # not businesses; outside the durability gate


def load_api_key(path):
    return Path(path).read_text(encoding="utf-8-sig").strip()


def fetch_details(ticker, api_key):
    url = f"{BASE_URL}/v3/reference/tickers/{ticker}"
    r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("results") or {}


def fetch_financials(ticker, api_key, years):
    url = f"{BASE_URL}/vX/reference/financials"
    params = {"ticker": ticker, "limit": years, "timeframe": "annual",
              "order": "desc", "sort": "period_of_report_date"}
    r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("results") or []


def main():
    ap = argparse.ArgumentParser(description="Build the durability whitelist from Massive fundamentals.")
    ap.add_argument("key_file")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--ticker", help="Only this ticker (for testing)")
    ap.add_argument("--years", type=int, default=4, help="Annual reports to pull (for trends)")
    ap.add_argument("--delay", type=float, default=12.0, help="Seconds between API calls")
    ap.add_argument("--fallback-yf", action="store_true",
                    help="After the Massive pass, classify the 'no financials' gaps via yfinance")
    args = ap.parse_args()

    api_key = load_api_key(args.key_file)
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")
    flags = load_flags(conn)

    tickers = [args.ticker.upper()] if args.ticker else held_tickers(args.db)
    print(f"Classifying {len(tickers)} ticker(s) via Massive...\n")
    print(HEADER + " vetoes")
    gaps = []
    first = True
    for t in tickers:
        try:
            if not first:
                time.sleep(args.delay)
            details = fetch_details(t, api_key)
            first = False
            if (details.get("type") or "").upper() in ETF_TYPES:
                mark_etf(conn, t)
                conn.commit()
                print(f"  {t:<8} ETF/fund (durability n/a)")
                continue
            time.sleep(args.delay)
            reports = fetch_financials(t, api_key, args.years)
        except Exception as e:
            print(f"  {t:<8} fetch error: {e}")
            gaps.append(t)
            continue
        if not reports:
            print(f"  {t:<8} no financials")
            gaps.append(t)
            continue
        m = compute(details, reports, CONFIG, flags.get(t))
        upsert(conn, t, m)
        conn.commit()
        print(fmt_row(t, m))

    if args.fallback_yf and gaps:
        print(f"\nFalling back to yfinance for {len(gaps)} name(s) Massive couldn't cover...\n")
        print(HEADER + f" {'cur':>4} vetoes")
        from build_durability_yf import classify_tickers   # lazy: keep yfinance optional
        classify_tickers(conn, gaps, years=args.years, flags=flags)

    conn.close()
    print("\nStored in durability. Set manual_flag in DB Browser to force TERMINAL "
          "(preserved across rebuilds).")


if __name__ == "__main__":
    main()
