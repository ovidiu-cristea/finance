"""Durability classification for names Massive doesn't cover (foreign ADRs), via yfinance.

Massive only parses US-GAAP filings, so European ADRs (RACE, STLA, LVMUY, BUD,
DASTY, ...) come back `no financials` from build_durability.py. This adapter pulls
the same line items from Yahoo Finance and feeds the *same* scoring core, so a
foreign name is scored and vetoed exactly like a US one (same `durability` table).

Can be run standalone, or invoked as the `--fallback-yf` pass of build_durability.py
(which calls `classify_tickers` directly). Standalone, it targets held tickers with
no `durability` row yet and skips anything Yahoo flags as an ETF/fund.

Statements report in the company's home currency (e.g. EUR). That's fine: every
metric used is a ratio so currency cancels; the only absolute threshold is the
$300M cap floor, and Yahoo's `marketCap` is in USD for these US-listed ADRs.

Requires: pip install yfinance

Usage:
    python build_durability_yf.py [--ticker RACE] [--years 4] [--delay 1] [--db X]
"""
import argparse
import sqlite3
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

from durability_core import CONFIG, HEADER, compute, fmt_row, load_flags, upsert

HERE = Path(__file__).resolve().parent
SKIP_QUOTE_TYPES = {"ETF", "MUTUALFUND", "INDEX", "CURRENCY"}

# Some held symbols are thinly-traded OTC lines whose Yahoo fundamentals are
# broken/empty; fetch from a cleaner listing instead, still stored under the held
# symbol. (Empty for now - add entries only after verifying the alias is the SAME
# company: e.g. a foreign primary listing for a sparse US ADR.)
SYMBOL_OVERRIDES = {}

# Yahoo line-item labels (first present wins) -> our canonical fields.
ASSETS      = ["Total Assets"]
LIABS       = ["Total Liabilities Net Minority Interest", "Total Liabilities"]
EQUITY      = ["Stockholders Equity", "Total Equity Gross Minority Interest", "Common Stock Equity"]
CUR_ASSETS  = ["Current Assets", "Total Current Assets"]
CUR_LIABS   = ["Current Liabilities", "Total Current Liabilities"]
REVENUES    = ["Total Revenue", "Operating Revenue"]
OPER_INCOME = ["Operating Income", "Operating Income Or Loss"]
NET_INCOME  = ["Net Income", "Net Income Common Stockholders",
               "Net Income From Continuing Operation Net Minority Interest"]
OCF         = ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities",
               "Total Cash From Operating Activities"]


def unclassified_tickers(db_path):
    rows = sqlite3.connect(db_path).execute(
        """SELECT DISTINCT symbol FROM tax_lots
           WHERE status = 'open'
             AND symbol NOT IN (SELECT symbol FROM durability)""").fetchall()
    return sorted(r[0] for r in rows if r[0])


def cell(df, label, col):
    if df is None or df.empty or col is None or label not in df.index or col not in df.columns:
        return None
    v = df.at[label, col]
    if isinstance(v, pd.Series):       # duplicate index label -> take first
        v = v.iloc[0]
    return float(v) if pd.notna(v) else None


def pick(df, labels, col):
    for lab in labels:
        v = cell(df, lab, col)
        if v is not None:
            return v
    return None


def col_for(df, date):
    """Find the column in df for `date` (exact, else same fiscal year)."""
    if df is None or df.empty:
        return None
    if date in df.columns:
        return date
    for c in df.columns:
        if getattr(c, "year", None) == getattr(date, "year", None):
            return c
    return None


def build_report(bs, inc, cf, date):
    w = lambda v: {"value": v}
    icol, ccol = col_for(inc, date), col_for(cf, date)
    fin = {
        "balance_sheet": {
            "assets": w(pick(bs, ASSETS, date)),
            "liabilities": w(pick(bs, LIABS, date)),
            "equity": w(pick(bs, EQUITY, date)),
            "current_assets": w(pick(bs, CUR_ASSETS, date)),
            "current_liabilities": w(pick(bs, CUR_LIABS, date)),
        },
        "income_statement": {
            "revenues": w(pick(inc, REVENUES, icol)),
            "operating_income_loss": w(pick(inc, OPER_INCOME, icol)),
            "net_income_loss": w(pick(inc, NET_INCOME, icol)),
        },
        "cash_flow_statement": {
            "net_cash_flow_from_operating_activities": w(pick(cf, OCF, ccol)),
        },
    }
    return {"fiscal_year": str(getattr(date, "year", "")),
            "end_date": date.date().isoformat() if hasattr(date, "date") else str(date),
            "financials": fin}


def fetch_reports(ticker, years):
    """Returns (details, reports) or (details, None) if no usable statements."""
    t = yf.Ticker(SYMBOL_OVERRIDES.get(ticker, ticker))
    info = t.info or {}
    details = {"market_cap": info.get("marketCap"),
               "quote_type": (info.get("quoteType") or "").upper(),
               "currency": info.get("financialCurrency")}
    bs = t.balance_sheet
    inc = getattr(t, "income_stmt", None)
    if inc is None or (hasattr(inc, "empty") and inc.empty):
        inc = t.financials
    cf = t.cashflow
    if bs is None or bs.empty:
        return details, None
    dates = sorted(bs.columns, reverse=True)[:years]
    return details, [build_report(bs, inc, cf, d) for d in dates]


def classify_tickers(conn, tickers, years=4, delay=1.0, flags=None):
    """Classify `tickers` via yfinance, upserting into `durability`. Prints one row
    each (no header - caller prints it). ETFs/funds are skipped (row deleted)."""
    if flags is None:
        flags = load_flags(conn)
    for i, t in enumerate(tickers):
        if i:
            time.sleep(delay)
        try:
            details, reports = fetch_reports(t, years)
        except Exception as e:
            print(f"  {t:<8} fetch error: {e}")
            continue
        if details.get("quote_type") in SKIP_QUOTE_TYPES:
            conn.execute("DELETE FROM durability WHERE symbol = ?", (t,))
            conn.commit()
            print(f"  {t:<8} skipped ({details['quote_type'].lower()})")
            continue
        if not reports:
            print(f"  {t:<8} no financials on Yahoo")
            continue
        m = compute(details, reports, CONFIG, flags.get(t))
        upsert(conn, t, m)
        conn.commit()
        print(fmt_row(t, m, extra=f"{(details.get('currency') or '?'):>4} "))


def main():
    ap = argparse.ArgumentParser(description="Durability classification for foreign ADRs via yfinance.")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--ticker", help="Only this ticker (else: all held names with no durability row)")
    ap.add_argument("--years", type=int, default=4, help="Annual reports to pull (for trends)")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between tickers")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")

    tickers = [args.ticker.upper()] if args.ticker else unclassified_tickers(args.db)
    print(f"Classifying {len(tickers)} ticker(s) via yfinance...\n")
    print(HEADER + f" {'cur':>4} vetoes")
    classify_tickers(conn, tickers, years=args.years, delay=args.delay)
    conn.close()
    print("\nStored in durability (same table as build_durability.py). "
          "Statement currency shown under 'cur'; ratios are currency-neutral.")


if __name__ == "__main__":
    main()
