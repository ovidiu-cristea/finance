"""Probe Massive for fundamentals needed by the durability whitelist.

Checks two Polygon-style endpoints:
  * /v3/reference/tickers/{ticker}   -> market cap, shares, sector (size veto)
  * /vX/reference/financials         -> income / balance / cash-flow statements
                                        (profitability, balance sheet, Altman Z inputs)
Dumps the available line-item field names + values so we can map them to the
durability metrics. Read-only.

Usage:
    python probe_fundamentals.py <api-key-file> [--ticker AAPL]
"""
import argparse
from pathlib import Path

import requests

BASE_URL = "https://api.massive.com"


def load_api_key(path):
    return Path(path).read_text(encoding="utf-8-sig").strip()


def get(url, api_key, params=None):
    return requests.get(url, headers={"Authorization": f"Bearer {api_key}"},
                        params=params or {}, timeout=30)


def main():
    ap = argparse.ArgumentParser(description="Probe Massive fundamentals endpoints.")
    ap.add_argument("key_file")
    ap.add_argument("--ticker", default="AAPL")
    args = ap.parse_args()
    api_key = load_api_key(args.key_file)
    t = args.ticker.upper()

    # 1) Ticker details: market cap / shares / classification (for the size veto)
    print("=" * 64)
    url = f"{BASE_URL}/v3/reference/tickers/{t}"
    print(f"GET {url}")
    r = get(url, api_key)
    print(f"status: {r.status_code}")
    if r.status_code == 200:
        res = r.json().get("results") or {}
        for k in ("name", "market_cap", "share_class_shares_outstanding",
                  "weighted_shares_outstanding", "total_employees", "sic_description",
                  "type", "primary_exchange", "currency_name", "list_date"):
            print(f"  {k}: {res.get(k)}")
    else:
        print("body:", r.text[:600])

    # 2) Financial statements
    print("=" * 64)
    url = f"{BASE_URL}/vX/reference/financials"
    params = {"ticker": t, "limit": 4, "timeframe": "annual",
              "order": "desc", "sort": "period_of_report_date"}
    print(f"GET {url}  params={params}")
    r = get(url, api_key, params)
    print(f"status: {r.status_code}")
    if r.status_code != 200:
        print("body:", r.text[:800])
        return

    data = r.json()
    print("top-level keys:", list(data.keys()), " count:", data.get("count"))
    results = data.get("results") or []
    print(f"reports returned: {len(results)}")
    if not results:
        print("raw:", str(data)[:600])
        return

    rep = results[0]
    print("report keys:", list(rep.keys()))
    print(f"period: {rep.get('fiscal_period')} {rep.get('fiscal_year')}  "
          f"{rep.get('start_date')} -> {rep.get('end_date')}")

    fin = rep.get("financials") or {}
    print("statements:", list(fin.keys()))
    for stmt, items in fin.items():
        print(f"\n  --- {stmt} ({len(items)} items) ---")
        for key, v in items.items():
            val = v.get("value") if isinstance(v, dict) else v
            unit = v.get("unit") if isinstance(v, dict) else ""
            print(f"    {key:<48} {val!s:>18}  {unit}")


if __name__ == "__main__":
    main()
