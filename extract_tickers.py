import csv
import datetime
import re
import sqlite3
import sys
import time

import requests

TICKER_RE = re.compile(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$')


def is_ticker(value: str) -> bool:
    return bool(TICKER_RE.match(value.strip().upper()))


def extract_tickers(db_path: str) -> list[str]:
    """Distinct tickers from open tax lots in the holdings database."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM tax_lots WHERE status = 'open'"
        ).fetchall()
    finally:
        conn.close()
    return sorted({r[0].strip().upper() for r in rows if is_ticker(r[0])})

BASE_URL = "https://api.massive.com"


def load_api_key(key_file: str) -> str:
    with open(key_file) as f:
        return f.read().strip()


def get_previous_day_price(ticker: str, api_key: str) -> dict | None:
    url = f"{BASE_URL}/v2/aggs/ticker/{ticker}/prev"
    # If Massive uses a different auth scheme (e.g. X-API-Key or ?apiKey=), update this header
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()
    results = data.get("results")
    if not results:
        return None
    bar = results[0]
    return {
        "ticker": ticker,
        "vwap": bar.get("vw"),
        "open": bar.get("o"),
        "close": bar.get("c"),
        "high": bar.get("h"),
        "low": bar.get("l"),
        "volume": bar.get("v"),
    }


if __name__ == "__main__":
    if len(sys.argv) == 2:
        tickers = extract_tickers(sys.argv[1])
        print(f"Found {len(tickers)} unique ticker(s):")
        for ticker in tickers:
            print(ticker)
    elif len(sys.argv) == 3:
        tickers = extract_tickers(sys.argv[1])
        if not tickers:
            print("No valid tickers found.")
            sys.exit(1)
        api_key = load_api_key(sys.argv[2])
        print(f"Fetching previous-day VWAP for {len(tickers)} ticker(s) (5/min rate limit)...\n")
        RATE_LIMIT_DELAY = 60 / 5  # 12 seconds between calls
        today = datetime.date.today().strftime("%Y-%m-%d")
        csv_out = f"stocksVWAP-{today}.csv"
        results = []
        for i, ticker in enumerate(tickers):
            if i > 0:
                time.sleep(RATE_LIMIT_DELAY)
            result = get_previous_day_price(ticker, api_key)
            if result is None:
                print(f"{ticker:<8}  no data")
            else:
                vwap = result["vwap"]
                print(f"{ticker:<8}  VWAP: ${vwap:.2f}")
                results.append(result)
        with open(csv_out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["ticker", "vwap", "open", "close", "high", "low", "volume"])
            writer.writeheader()
            writer.writerows(results)
        print(f"\nSaved {len(results)} record(s) to {csv_out}")
    else:
        print("Usage:")
        print("  python extract_tickers.py <holdings.db>")
        print("  python extract_tickers.py <holdings.db> <api_key_file>")
        sys.exit(1)
