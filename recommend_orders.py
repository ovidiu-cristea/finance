"""Recommend trailing stop-loss sell orders for targeted tax lots.

Strategy (per open lot that has a target_min_profit_pct), run end-of-day:
  per_share_cost = cost_basis / original_quantity
  target_price   = per_share_cost * (1 + target/100)      # minimum acceptable sell
  trailing_stop  = current_price * (1 - buffer/100)        # buffer default 3.5%

Recommend a SELL STOP at `trailing_stop`, but only once
`trailing_stop >= target_price` (i.e. the price has risen far enough that a
buffer% pullback still clears your target profit). Re-run daily: as the price
climbs, the trailing stop climbs with it - keep raising the placed stop, never
lower it, so you capture the upside and sell when the stock turns down.

Order size: lots at the low target (10%) sell 90% of their shares (rounded down
to a whole share); lots at the high target sell the full remaining quantity.

Price source: an end-of-day CSV (e.g. Massive VWAP via extract_tickers.py) with
--prices-csv, or live SnapTrade positions if a consumer-key file is given.

Read-only: prints suggestions, places nothing and writes nothing.

Usage:
    python recommend_orders.py --prices-csv stocksVWAP-2026-06-18.csv [--account X] [--all]
    python recommend_orders.py <consumer-key-file> [--account X] [--buffer-pct 3.5] [--all]
"""
import argparse
import csv
import math
import sqlite3
import sys
from pathlib import Path

from rebrands import canonical_symbol

HERE = Path(__file__).resolve().parent
CLIENT_ID = "PERS-97BRLMMWM55XNVEEORUA"
DEFAULT_BUFFER_PCT = 3.5

# Lots at the low target sell only part of the position; high-target lots sell all.
LOW_TARGET_PCT = 10.0
LOW_SELL_FRACTION = 0.90


def sell_quantity(remaining, target_pct):
    """Shares to sell for a lot: 90% (rounded down to a whole share) at the low
    target, otherwise the full remaining quantity. Rounding down means a small
    low-target lot always keeps at least one share."""
    if abs(target_pct - LOW_TARGET_PCT) < 1e-9:
        return math.floor(remaining * LOW_SELL_FRACTION)
    return remaining


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_secret(path):
    secret = Path(path).read_text(encoding="utf-8-sig").strip()
    if not secret:
        sys.exit(f"Error: {path} is empty")
    return secret


def evaluate_lot(per_share, target_pct, price, buffer_pct):
    """Return (status, target_price, arm_price, stop).

    status is one of armed / watch / below / noprice. `arm_price` is the price at
    which a buffer% trailing stop first reaches the target. `stop` is set only
    when armed (= current price minus buffer%, which by construction >= target).
    """
    target_price = per_share * (1 + target_pct / 100)
    factor = 1 - buffer_pct / 100
    arm_price = target_price / factor if factor > 0 else None
    if price is None:
        return "noprice", target_price, arm_price, None
    stop = price * factor
    if stop >= target_price:
        return "armed", target_price, arm_price, stop
    if price >= target_price:
        return "watch", target_price, arm_price, None
    return "below", target_price, arm_price, None


def read_prices_csv(path):
    """Read {canonical_symbol: price} from a CSV with a ticker and price column.

    Accepts header names ticker/symbol for the symbol and price/vwap/close for
    the value - matches the output of extract_tickers.py (ticker, vwap, ...).
    """
    prices = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sym = (row.get("ticker") or row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            raw = next((row[c] for c in ("price", "vwap", "close")
                        if row.get(c) not in (None, "")), None)
            price = to_float(raw)
            if price and price > 0:
                prices[canonical_symbol(sym)] = price
    return prices


def snaptrade_prices(consumer_key, account_ids):
    """Return ({canonical_symbol: price}, as_of) from live SnapTrade positions."""
    from snaptrade_client import SnapTrade

    def position_symbol(pos):
        instrument = pos.get("instrument") or {}
        return instrument.get("symbol") or instrument.get("raw_symbol") or "?"

    snaptrade = SnapTrade(client_id=CLIENT_ID, consumer_key=consumer_key)
    prices, as_of = {}, None
    for account_id in account_ids:
        body = snaptrade.account_information.get_all_account_positions(
            user_id=CLIENT_ID, user_secret=consumer_key, account_id=account_id,
        ).body
        as_of = as_of or (body.get("data_freshness") or {}).get("as_of")
        for pos in body.get("results") or []:
            price = to_float(pos.get("price"))
            if price and price > 0:
                prices[canonical_symbol(position_symbol(pos))] = price
    return prices, as_of


def load_lots(db_path, account_filter):
    rows = sqlite3.connect(db_path).execute(
        """
        SELECT t.account_id, a.name, a.number, t.symbol, t.open_date,
               t.remaining_quantity, t.original_quantity, t.cost_basis,
               t.target_min_profit_pct
        FROM tax_lots t JOIN accounts a ON a.id = t.account_id
        WHERE t.status = 'open' AND t.target_min_profit_pct IS NOT NULL
        ORDER BY a.name, t.symbol, t.open_date
        """
    ).fetchall()
    if account_filter:
        rows = [r for r in rows
                if r[0] == account_filter or str(r[2] or "").endswith(account_filter)]
    return rows


def main():
    ap = argparse.ArgumentParser(description="Recommend trailing stop-loss orders.")
    ap.add_argument("key_file", nargs="?",
                    help="SnapTrade consumer-key file (omit if using --prices-csv)")
    ap.add_argument("--prices-csv", help="End-of-day price CSV (ticker, vwap/price/close)")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--account", help="Filter by account id or last-4 digits")
    ap.add_argument("--buffer-pct", type=float, default=DEFAULT_BUFFER_PCT,
                    help="Trailing-stop distance below price, also the arming margin (default 3.5)")
    ap.add_argument("--all", action="store_true",
                    help="Show every targeted lot with its status, not just armed ones")
    args = ap.parse_args()

    lots = load_lots(args.db, args.account)
    account_ids = {r[0] for r in lots}

    if args.prices_csv:
        prices, source = read_prices_csv(args.prices_csv), f"csv {Path(args.prices_csv).name}"
    elif args.key_file:
        prices, as_of = snaptrade_prices(read_secret(args.key_file), account_ids)
        source = f"SnapTrade, as of {as_of}"
    else:
        ap.error("provide a consumer-key file or --prices-csv")

    buffer = args.buffer_pct
    print(f"Trailing stop recommendations  (stop = price x {1 - buffer/100:.3f}; "
          f"prices: {source})\n")

    recs = []
    counts = {"armed": 0, "watch": 0, "below": 0, "noprice": 0}
    cur_acct = None
    for (acct_id, acct, number, symbol, date, qty, orig_qty, basis, target) in lots:
        if not basis or not orig_qty:
            counts["noprice"] += 1
            continue
        per_share = basis / orig_qty
        price = prices.get(symbol)
        status, target_price, arm_price, stop = evaluate_lot(per_share, target, price, buffer)
        counts[status] += 1
        if status == "armed":
            sell_qty = sell_quantity(qty, target)
            if sell_qty > 0:
                recs.append((acct, symbol, date, qty, sell_qty, stop, target, target_price, price))

        if args.all:
            if acct != cur_acct:
                print(acct)
                cur_acct = acct
            pstr = f"${price:,.2f}" if price is not None else "n/a"
            arm = f"${arm_price:,.2f}" if arm_price else "n/a"
            print(f"  {symbol:<8} {date}  {qty:>7g} sh  cost/sh ${per_share:,.2f}  "
                  f"target +{target:g}%=${target_price:,.2f}  arm>={arm}  "
                  f"last {pstr}  [{status.upper()}]")

    if args.all:
        print()
    print("=== SELL STOP orders to place / raise ===")
    if not recs:
        print("  (none armed)")
    cur_acct = None
    total = 0.0
    for acct, symbol, date, held, sell_qty, stop, target, target_price, price in recs:
        if acct != cur_acct:
            print(f"\n{acct}")
            cur_acct = acct
        proceeds = sell_qty * stop
        total += proceeds
        partial = f" ({LOW_SELL_FRACTION:.0%} of {held:g})" if sell_qty != held else ""
        print(f"  SELL STOP {sell_qty:>7g} {symbol:<8} lot {date}{partial}  @ stop ${stop:,.2f}   "
              f"(last ${price:,.2f}, target +{target:g}%=${target_price:,.2f}, ~${proceeds:,.2f})")

    if recs:
        print(f"\n  {len(recs)} order(s), ~${total:,.2f} proceeds at stop.")
    print(f"\nLots: armed={counts['armed']}  watching={counts['watch']}  "
          f"below_target={counts['below']}  no_price={counts['noprice']}")


if __name__ == "__main__":
    main()
