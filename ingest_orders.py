"""Ingest recent executed BUY orders from SnapTrade into the holdings DB.

Reads executed orders (last N days) per account:

  * BUY  -> creates a new tax lot (target 10%), deduped by buy_order_id.
  * SELL -> listed only, not applied here. Sell lot-tracking is done by
            disambiguate_sells.py, using Fidelity's post-sale lot view as ground
            truth (handles partial sales and same-size-lot ambiguity that this
            script can't). The sell line is shown so you know which tickers to
            run disambiguate_sells.py on.

Read-only by default. With --apply, each BUY is confirmed interactively
(y applies, n or Enter skips); confirmed writes are committed per order.

Usage:
    python ingest_orders.py <consumer-key-file> [--db X] [--account X] [--days N] [--apply]
"""
import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

from snaptrade_client import SnapTrade

from rebrands import canonical_symbol

HERE = Path(__file__).resolve().parent
CLIENT_ID = "PERS-97BRLMMWM55XNVEEORUA"
EPSILON = 1e-6

# Money-market cash sweep symbols - skip; these are cash, not tradable lots.
CASH_SYMBOLS = {"SPAXX", "FDRXX"}

# New lots created from buy orders default to the low sell target.
LOW_TARGET_PCT = 10.0


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


def order_symbol(order):
    uni = order.get("universal_symbol") or {}
    opt = order.get("option_symbol") or {}
    return canonical_symbol(uni.get("symbol") or opt.get("ticker") or order.get("symbol") or "?")


def order_description(order):
    uni = order.get("universal_symbol") or {}
    return uni.get("description") or uni.get("raw_symbol")


def is_buy(action):
    return (action or "").upper().startswith("BUY")


def is_sell(action):
    return (action or "").upper().startswith("SELL")


def fetch_executed_orders(snaptrade, consumer_key, account_id, days):
    return snaptrade.account_information.get_user_account_orders(
        user_id=CLIENT_ID, user_secret=consumer_key,
        account_id=account_id, state="executed", days=days,
    ).body


def existing_buy_lot(conn, order_id):
    """Return the id of a tax lot already created from this buy order, else None."""
    row = conn.execute(
        "SELECT id FROM tax_lots WHERE buy_order_id = ?", (order_id,)
    ).fetchone()
    return row[0] if row else None


def ensure_security(conn, symbol, description):
    conn.execute(
        """
        INSERT INTO securities (symbol, description) VALUES (?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            description = COALESCE(securities.description, excluded.description)
        """,
        (symbol, description),
    )


def open_lots(conn, account_id, symbol):
    """Open tax lots for one (account, symbol). Used by disambiguate_sells.py."""
    return conn.execute(
        """
        SELECT id, open_date, original_quantity, remaining_quantity, cost_basis
        FROM tax_lots
        WHERE account_id = ? AND symbol = ? AND status = 'open'
        ORDER BY open_date, id
        """,
        (account_id, symbol),
    ).fetchall()


def record_order(conn, order, account_id, run_id, applied, needs_review, note, already):
    """Insert a new executed order, or update applied/needs_review on an existing one."""
    oid = order.get("brokerage_order_id")
    if already:
        conn.execute(
            "UPDATE executed_orders SET applied = ?, needs_review = ?, notes = ? "
            "WHERE brokerage_order_id = ?",
            (applied, needs_review, note, oid),
        )
        return
    conn.execute(
        """
        INSERT INTO executed_orders
            (brokerage_order_id, account_id, symbol, action,
             total_quantity, filled_quantity, execution_price, order_type,
             time_placed, time_executed, status, first_seen_run_id,
             planned_order_id, applied, needs_review, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        ON CONFLICT(brokerage_order_id) DO NOTHING
        """,
        (oid, account_id, order_symbol(order), order.get("action"),
         to_float(order.get("total_quantity")),
         to_float(order.get("filled_quantity")) or to_float(order.get("total_quantity")),
         to_float(order.get("execution_price")), order.get("order_type"),
         str(order.get("time_placed") or "")[:19] or None,
         str(order.get("time_executed") or "")[:19] or None,
         str(order.get("status") or "") or None,
         run_id, applied, needs_review, note),
    )


def confirm(prompt="      apply this order? [y/N] "):
    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        return False


def ingest_account(conn, snaptrade, consumer_key, account_id, name, days, run_id, apply):
    orders = fetch_executed_orders(snaptrade, consumer_key, account_id, days)
    print(f"\n{name}  [{account_id[:8]}]  ({len(orders)} executed orders, last {days}d)")
    if orders:
        print(f"  {'executed':<20} {'action':<5} {'symbol':<10} {'qty':>8} {'price':>10}")

    existing = {r[0]: r[1] for r in conn.execute(
        "SELECT brokerage_order_id, applied FROM executed_orders")}
    new = 0
    for order in sorted(orders, key=lambda o: str(o.get("time_executed") or "")):
        oid = order.get("brokerage_order_id")
        symbol = order_symbol(order)
        if symbol in CASH_SYMBOLS:
            continue  # money-market cash sweep, not a tradable lot
        action = order.get("action")
        filled = to_float(order.get("filled_quantity")) or to_float(order.get("total_quantity"))
        price = to_float(order.get("execution_price"))
        executed_at = str(order.get("time_executed") or "")[:19]
        already = oid in existing

        print(f"  {executed_at:<20} {(action or '?'):<5} {symbol:<10} "
              f"{(filled or 0):>8g} {(price or 0):>10.2f}")

        # ---------- BUY: create a new lot ----------
        if is_buy(action):
            open_date = executed_at[:10]
            cost_basis = (filled or 0) * (price or 0)
            dup_lot = existing_buy_lot(conn, oid)
            if dup_lot is not None:
                print("      DUPLICATE ORDER, will NOT be inserted in the database again")
            else:
                print(f"      -> create new lot {symbol}: date={open_date}, qty={filled or 0:g}, "
                      f"price=${price or 0:,.2f}, cost_basis=${cost_basis:,.2f}, "
                      f"target={LOW_TARGET_PCT:g}%")
            if not apply:
                continue
            will_order, will_lot = not already, dup_lot is None
            if not (will_order or will_lot):
                print("      already recorded, nothing to apply")
                continue
            if not confirm():
                print("      SKIPPED")
                continue
            ensure_security(conn, symbol, order_description(order))
            done = []
            if will_order:
                record_order(conn, order, account_id, run_id, applied=1, needs_review=0,
                             note=f"buy - new lot (target {LOW_TARGET_PCT:g}%)", already=False)
                new += 1
                done.append("order recorded")
            if will_lot:
                conn.execute(
                    """
                    INSERT INTO tax_lots
                        (account_id, symbol, open_date, original_quantity,
                         remaining_quantity, price_per_share, cost_basis,
                         source, buy_order_id, status, target_min_profit_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'sync', ?, 'open', ?)
                    """,
                    (account_id, symbol, open_date, filled, filled, price, cost_basis,
                     oid, LOW_TARGET_PCT),
                )
                done.append(f"lot created (target {LOW_TARGET_PCT:g}%)")
            conn.commit()
            print(f"      INSERTED: {', '.join(done)}")
            continue

        # ---------- SELL: listed only; applied by disambiguate_sells.py ----------
        if is_sell(action):
            if existing.get(oid) == 1:
                print("      already applied (via disambiguate_sells)")
            else:
                print(f"      -> sell: run disambiguate_sells.py {symbol} <fidelity-paste>")
            continue

        # ---------- other actions ----------
        print(f"      REVIEW: unhandled action {action!r}")
    return new


def main():
    ap = argparse.ArgumentParser(description="Ingest executed SnapTrade orders into the DB.")
    ap.add_argument("key_file", help="File containing the SnapTrade consumer key")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--account", help="Filter by account id or last-4 digits")
    ap.add_argument("--days", type=int, default=10, help="Look-back window (max 90)")
    ap.add_argument("--apply", action="store_true", help="Write to the DB (default: dry run)")
    args = ap.parse_args()

    consumer_key = read_secret(args.key_file)
    snaptrade = SnapTrade(client_id=CLIENT_ID, consumer_key=consumer_key)

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")

    accounts = snaptrade.account_information.list_user_accounts(
        user_id=CLIENT_ID, user_secret=consumer_key,
    ).body
    if args.account:
        accounts = [a for a in accounts
                    if a["id"] == args.account
                    or str(a.get("number", "")).endswith(args.account)]
        if not accounts:
            sys.exit(f"No SnapTrade account matched {args.account!r}")

    run_id = None
    if args.apply:
        cur = conn.execute(
            "INSERT INTO sync_runs (run_at, source) VALUES (?, 'orders-ingest')",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),),
        )
        run_id = cur.lastrowid

    total_new = 0
    try:
        for acct in accounts:
            name = acct.get("name") or acct.get("institution_name") or "(unnamed)"
            total_new += ingest_account(conn, snaptrade, consumer_key, acct["id"],
                                        name, args.days, run_id, args.apply)
        conn.commit()
    finally:
        conn.close()

    if args.apply:
        print(f"\nRecorded {total_new} new buy order(s). "
              f"Apply any sells listed above with disambiguate_sells.py.")
    else:
        print("\nDry run - re-run with --apply to record buy orders.")


if __name__ == "__main__":
    main()
