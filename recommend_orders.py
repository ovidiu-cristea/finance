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

It also suggests BUYS with the buy-side guardrails, in two modes:
  * ADD (held full lots): price >= BUY_DIP_PCT% (10%) below the cheapest FULL lot
    (low-target/10% lot).
  * RE-ENTER (no full lot left - ran up and trimmed away): >= RE_ENTRY_DIP_PCT%
    (25%) below the 52-week high (from stock_metrics).
Size for both = the largest full lot's SHARE count (ever, for re-entry) x a
drawdown factor (full to -20%, tapering to 0 at -50%), valued at the current
price. Blocked by the 200-day-MA breaker (below a falling MA for >=
MA_BREAKER_DAYS), an optional --position-cap, and the DURABILITY GATE: names the
`durability` table marks HOLD_ONLY or TERMINAL are blocked from adds; ELIGIBLE,
ETFs, and unrated names fall through to the price guardrails. TERMINAL names
additionally surface an EXIT recommendation (the terminal-risk downside exit).
`--ignore-durability` turns the gate + exits off. Metrics from build_metrics.py;
fully-exited (zero-share) names are handled manually.

Price source: an end-of-day CSV (e.g. Massive VWAP via extract_tickers.py) with
--prices-csv, or live SnapTrade positions if a consumer-key file is given.

Read-only: prints suggestions, places nothing and writes nothing.

Usage:
    python recommend_orders.py --prices-csv stocksVWAP-2026-06-18.csv [--account X] [--all]
    python recommend_orders.py <consumer-key-file> [--account X] [--buffer-pct 3.5] [--all]
"""
import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from rebrands import canonical_symbol
from strategy_core import (
    BUY_DIP_PCT, DD_FULL_PCT, DD_STOP_PCT, DEFAULT_BUFFER_PCT, EPSILON,
    LOW_SELL_FRACTION, LOW_TARGET_PCT, MA_BREAKER_DAYS, RE_ENTRY_DIP_PCT,
    add_size_factor, evaluate_buy, evaluate_lot, sell_quantity,
)

HERE = Path(__file__).resolve().parent
CLIENT_ID = "PERS-97BRLMMWM55XNVEEORUA"


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


def load_metrics(db_path):
    """Return {symbol: {drawdown_pct, below_ma_days, ma_200_slope, ...}} from stock_metrics."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM stock_metrics")}
    except sqlite3.OperationalError:
        return {}  # table not built yet
    finally:
        conn.close()


def load_durability(db_path):
    """Return {symbol: {class, score, vetoes, ...}} from the durability table."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM durability")}
    except sqlite3.OperationalError:
        return {}  # table not built yet
    finally:
        conn.close()


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


def load_positions(db_path, account_filter):
    """Per (account, symbol): cheapest FULL-lot per-share cost and largest FULL-lot
    share count (low-target/10% lots only - the average-down anchor + base buy size;
    NULL if the position has no full lot), plus TOTAL held shares (all open lots,
    for the position cap) and lot count."""
    rows = sqlite3.connect(db_path).execute(
        """
        SELECT t.account_id, a.name, a.number, t.symbol,
               MIN(CASE WHEN ABS(t.target_min_profit_pct - ?) < 0.001
                        THEN t.cost_basis / t.original_quantity END) AS cheapest,
               MAX(CASE WHEN ABS(t.target_min_profit_pct - ?) < 0.001
                        THEN t.original_quantity END)                AS base_shares,
               SUM(t.remaining_quantity)                              AS shares,
               COUNT(*)                                               AS lots
        FROM tax_lots t JOIN accounts a ON a.id = t.account_id
        WHERE t.status = 'open' AND t.original_quantity > 0
        GROUP BY t.account_id, t.symbol
        ORDER BY a.name, t.symbol
        """,
        (LOW_TARGET_PCT, LOW_TARGET_PCT),
    ).fetchall()
    if account_filter:
        rows = [r for r in rows
                if r[0] == account_filter or str(r[2] or "").endswith(account_filter)]
    return rows


def largest_full_lot_ever(db_path, account_filter):
    """{(account_id, symbol): largest full-lot share count ever held}, over ALL
    low-target lots (open OR closed) - the re-entry base size for names whose
    open full lots have been trimmed away."""
    rows = sqlite3.connect(db_path).execute(
        """
        SELECT t.account_id, a.number, t.symbol, MAX(t.original_quantity)
        FROM tax_lots t JOIN accounts a ON a.id = t.account_id
        WHERE t.original_quantity > 0 AND ABS(t.target_min_profit_pct - ?) < 0.001
        GROUP BY t.account_id, t.symbol
        """,
        (LOW_TARGET_PCT,),
    ).fetchall()
    out = {}
    for acct_id, number, symbol, mx in rows:
        if account_filter and not (acct_id == account_filter
                                   or str(number or "").endswith(account_filter)):
            continue
        out[(acct_id, symbol)] = mx
    return out


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
    ap.add_argument("--position-cap", type=float,
                    help="Stop adding once a name's held value reaches this $ (off by default)")
    ap.add_argument("--ignore-durability", action="store_true",
                    help="Disable the durability gate + terminal-risk exits (show raw price signals)")
    args = ap.parse_args()

    lots = load_lots(args.db, args.account)
    positions = load_positions(args.db, args.account)
    ever_shares = largest_full_lot_ever(args.db, args.account)
    metrics = load_metrics(args.db)
    durability = load_durability(args.db)
    account_ids = {p[0] for p in positions} | {r[0] for r in lots}

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

    # ---- EXIT recommendations: terminal-risk names (durability) ----
    if not args.ignore_durability:
        print("\n=== EXIT recommendations (terminal-risk durability) ===")
        exits = [(acct, symbol, shares, prices.get(symbol), durability[symbol].get("vetoes"))
                 for (acct_id, acct, number, symbol, cheapest, base_shares, shares, nlots) in positions
                 if (shares or 0) > 0 and (durability.get(symbol) or {}).get("class") == "TERMINAL"]
        if not exits:
            print("  (none)")
        else:
            cur_acct = None
            for acct, symbol, shares, price, vetoes in exits:
                if acct != cur_acct:
                    print(f"\n{acct}")
                    cur_acct = acct
                pstr = f"${price:,.2f}" if price is not None else "n/a"
                val = f"~${shares * price:,.0f}" if price is not None else "n/a"
                print(f"  EXIT {shares:>7g} {symbol:<8} @ {pstr}  ({val})  "
                      f"[terminal: {vetoes or 'manual'}]")

    # ---- BUY recommendations: average-down (held full lots) + re-entry (trimmed away) ----
    cap = args.position_cap
    print("\n=== BUY recommendations ===")
    print(f"  add:      price <= cheapest full (10%) lot x {1 - BUY_DIP_PCT/100:.2f}")
    print(f"  re-entry: no full lot left, >= {RE_ENTRY_DIP_PCT:.0f}% off the 52-week high")
    print(f"  size = largest full lot's shares x drawdown factor (full to -{DD_FULL_PCT:.0f}%, "
          f"taper to -{DD_STOP_PCT:.0f}%); MA-breaker {MA_BREAKER_DAYS}d"
          + (f"; cap ${cap:,.0f}/name" if cap else ""))
    print("  durability gate: blocks HOLD_ONLY / TERMINAL (ETFs + unrated pass)"
          if not args.ignore_durability else "  durability gate: OFF (--ignore-durability)")
    buy_recs, blocked = [], []
    bcounts = {"buy": 0, "blocked": 0, "hold": 0, "noprice": 0}
    cur_acct = None
    for (acct_id, acct, number, symbol, cheapest, base_shares, shares, nlots) in positions:
        price = prices.get(symbol)
        m = metrics.get(symbol)
        reentry = cheapest is None  # no full lot left -> re-entry candidate
        base = ever_shares.get((acct_id, symbol)) if reentry else base_shares

        status, reason, factor, buy_shares, note = "hold", "", 0.0, 0.0, ""
        # --- trigger ---
        if price is None:
            status, armed = "NOPRICE", False
            bcounts["noprice"] += 1
        elif reentry:
            dd = (m or {}).get("drawdown_pct")
            armed = dd is not None and dd <= -RE_ENTRY_DIP_PCT
            note = f"re-entry, {-dd:.0f}% off high" if armed else ""
            if not armed:
                bcounts["hold"] += 1
        else:
            armed, _ = evaluate_buy(cheapest, price, BUY_DIP_PCT)
            note = f"{(cheapest - price) / cheapest * 100:.0f}% below ${cheapest:,.2f}" if armed else ""
            if not armed:
                bcounts["hold"] += 1
        # --- durability gate + guardrail sizing ---
        if armed:
            dclass = (durability.get(symbol) or {}).get("class")
            # Block only names explicitly judged non-durable. ETFs and unrated
            # names fall through to the price guardrails below.
            if not args.ignore_durability and dclass in ("HOLD_ONLY", "TERMINAL"):
                status, reason = "BLOCKED", f"durability={dclass}"
                bcounts["blocked"] += 1
                blocked.append((acct, symbol, reason, reentry))
            else:
                if not m:
                    factor, reason = 0.0, "no metrics (run build_metrics)"
                elif not base:
                    factor, reason = 0.0, "no full-lot size reference"
                else:
                    factor, reason = add_size_factor(
                        m.get("drawdown_pct"), m.get("below_ma_days"), m.get("ma_200_slope"))
                    if cap and (shares or 0) * price >= cap and factor > 0:
                        factor, reason = 0.0, f"at position cap (${(shares or 0) * price:,.0f})"
                buy_shares = round((base or 0) * factor)
                if factor > EPSILON and buy_shares >= 1:
                    status = "RE-ENTER" if reentry else "BUY"
                    bcounts["buy"] += 1
                    buy_recs.append((acct, symbol, price, factor, base, buy_shares, reason, note, reentry))
                else:
                    status = "BLOCKED"
                    if factor > EPSILON and buy_shares < 1:
                        reason += " (rounds to 0 sh)"
                    bcounts["blocked"] += 1
                    blocked.append((acct, symbol, reason, reentry))
        if args.all:
            if acct != cur_acct:
                print(acct)
                cur_acct = acct
            pstr = f"${price:,.2f}" if price is not None else "n/a"
            if reentry:
                ddv = (m or {}).get("drawdown_pct")
                dds = f"{ddv:+.0f}%" if ddv is not None else "n/a"
                print(f"  {symbol:<8} re-entry drawdown {dds} (need <=-{RE_ENTRY_DIP_PCT:.0f}%)  "
                      f"last {pstr}  [{status}] {reason}")
            else:
                print(f"  {symbol:<8} add cheapest ${cheapest:,.2f}  "
                      f"buy<=${cheapest * (1 - BUY_DIP_PCT/100):,.2f}  last {pstr}  [{status}] {reason}")

    if args.all:
        print()
    cur_acct = None
    if buy_recs:
        for acct, symbol, price, factor, base, buy_shares, reason, note, reentry in buy_recs:
            if acct != cur_acct:
                print(f"{acct}")
                cur_acct = acct
            tag = "RE-ENTER" if reentry else "BUY"
            print(f"  {tag:<8} {buy_shares:>6g} {symbol:<8}  ~${buy_shares * price:,.0f} "
                  f"({factor:.0%} of {base:g}-sh lot) @ ${price:,.2f}   [{reason}; {note}]")
    else:
        print("  (none triggered)")
    if blocked:
        print("\n  Armed but blocked by guardrails:")
        for acct, symbol, reason, reentry in blocked:
            print(f"    {symbol:<8} ({acct}){' (re-entry)' if reentry else ''} - {reason}")
    print(f"\nPositions: buy={bcounts['buy']}  blocked={bcounts['blocked']}  "
          f"hold={bcounts['hold']}  no_price={bcounts['noprice']}")


if __name__ == "__main__":
    main()
