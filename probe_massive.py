"""Probe Massive's historical daily-bars (aggregates/range) endpoint.

Verifies we can pull the OHLCV history needed for the price-history metrics
(52-week high, 200-day MA, drawdown, ATR). Read-only - one API call.

Usage:
    python probe_massive.py <api-key-file> [--ticker QS] [--days 730]
"""
import argparse
import datetime
from pathlib import Path

import requests

BASE_URL = "https://api.massive.com"


def load_api_key(path):
    return Path(path).read_text(encoding="utf-8-sig").strip()


def bar_date(b):
    return datetime.datetime.utcfromtimestamp(b["t"] / 1000).date() if "t" in b else None


def main():
    ap = argparse.ArgumentParser(description="Probe Massive historical daily bars.")
    ap.add_argument("key_file")
    ap.add_argument("--ticker", default="QS")
    ap.add_argument("--days", type=int, default=730, help="Calendar days of history to request")
    args = ap.parse_args()

    api_key = load_api_key(args.key_file)
    today = datetime.date.today()
    start = today - datetime.timedelta(days=args.days)
    url = (f"{BASE_URL}/v2/aggs/ticker/{args.ticker}/range/1/day/"
           f"{start.isoformat()}/{today.isoformat()}")
    params = {"adjusted": "true", "sort": "asc", "limit": 50000}

    print(f"GET {url}")
    print(f"    params={params}\n")
    r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"},
                     params=params, timeout=30)
    print(f"status: {r.status_code}")
    if r.status_code != 200:
        print("body:", r.text[:800])
        return

    data = r.json()
    print("top-level keys:", list(data.keys()))
    print(f"status={data.get('status')}  resultsCount={data.get('resultsCount')}  "
          f"queryCount={data.get('queryCount')}  adjusted={data.get('adjusted')}  "
          f"next_url={'yes' if data.get('next_url') else 'no'}")

    results = data.get("results") or []
    print(f"\nbars returned: {len(results)}")
    if not results:
        print("No results. Raw:", str(data)[:600])
        return

    print("first-bar fields:", list(results[0].keys()))

    def fmt(b):
        return (f"{bar_date(b)}  o={b.get('o')} h={b.get('h')} l={b.get('l')} "
                f"c={b.get('c')} v={b.get('v')} vw={b.get('vw')}")
    print("earliest:", fmt(results[0]))
    print("latest:  ", fmt(results[-1]))

    closes = [b["c"] for b in results if b.get("c") is not None]
    highs = [b["h"] for b in results if b.get("h") is not None]
    if closes and highs:
        hi = max(highs)
        last = closes[-1]
        ma_n = min(200, len(closes))
        print(f"\nsanity metrics for {args.ticker} ({len(closes)} closes):")
        print(f"  52wk-ish high (max high) : {hi:,.2f}")
        print(f"  last close               : {last:,.2f}")
        print(f"  drawdown from high       : {(last / hi - 1) * 100:+.1f}%")
        print(f"  {ma_n}-day MA              : {sum(closes[-ma_n:]) / ma_n:,.2f}")


if __name__ == "__main__":
    main()
